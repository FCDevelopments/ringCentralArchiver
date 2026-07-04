"""
RingCentral Archiver -> Box (single-folder edition)
====================================================
Replaces the built-in RingCentral Archiver, which creates one folder per
line per day per media type. This script instead:

  1. Pulls the company call log from the RingCentral API for a date range.
  2. Appends every call to ONE combined CSV in Box (monthly file by default),
     sorted by call start time. Re-runs never duplicate rows.
  3. Downloads every call recording (MP3) and uploads them flat into the
     SAME single Box folder -- no per-day / per-line folder sprawl.

Auth:
  - RingCentral: JWT flow (server app, no UI).
  - Box: Client Credentials Grant (CCG) custom app.

Usage:
  python archiver.py                  # archive since last run (or yesterday)
  python archiver.py --from 2026-06-01 --to 2026-06-12
  python archiver.py --dry-run        # fetch + build locally, no Box uploads

State is kept in state.json next to this script so scheduled runs pick up
exactly where the last one left off.
"""

import argparse
import csv
import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

# ----------------------------------------------------------------------------
# Setup & configuration
# ----------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
load_dotenv(SCRIPT_DIR / ".env")

LOG_FILE = SCRIPT_DIR / "archiver.log"
STATE_FILE = SCRIPT_DIR / "state.json"
LOCAL_OUTPUT = SCRIPT_DIR / "output"          # used in dry-run mode
TEMP_DIR = SCRIPT_DIR / "temp"                # recording downloads before upload

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("archiver")


class Config:
    """All configuration comes from .env (see .env.example)."""

    # --- RingCentral (JWT auth) ---
    RC_CLIENT_ID = os.getenv("RC_CLIENT_ID", "")
    RC_CLIENT_SECRET = os.getenv("RC_CLIENT_SECRET", "")
    RC_JWT = os.getenv("RC_JWT", "")
    RC_SERVER_URL = os.getenv("RC_SERVER_URL", "https://platform.ringcentral.com")

    # --- Box (JWT auth) ---
    # Path to the JSON config downloaded from the Box Dev Console
    # (App > Configuration > App Settings > Download Config.JSON after generating a keypair).
    BOX_JWT_CONFIG = os.getenv("BOX_JWT_CONFIG", "")
    # Optional: a 60-minute developer token. If set, it overrides JWT (for quick testing only).
    BOX_DEV_TOKEN = os.getenv("BOX_DEV_TOKEN", "")
    # The ONE folder everything goes into. Grab the ID from the Box URL:
    # https://app.box.com/folder/123456789  ->  123456789
    BOX_FOLDER_ID = os.getenv("BOX_FOLDER_ID", "")

    # --- Behavior ---
    # monthly  -> one CSV per month:  "Call Log - 2026-06.csv"  (recommended)
    # never    -> one CSV forever:    "Call Log - Master.csv"
    CSV_ROTATION = os.getenv("CSV_ROTATION", "monthly").lower()
    # true  -> MP3s go into a single "Recordings" subfolder inside the folder
    # false -> MP3s sit flat next to the CSV (default; truly one folder)
    RECORDINGS_SUBFOLDER = os.getenv("RECORDINGS_SUBFOLDER", "false").lower() == "true"
    # Seconds to wait between recording downloads. RC media endpoints are on
    # the 'Heavy' usage plan (~10 requests/minute), so >= 6s is required to
    # stay under the limit on long runs.
    MEDIA_THROTTLE_SECONDS = float(os.getenv("MEDIA_THROTTLE_SECONDS", "6.5"))
    # Skip recordings entirely (CSV only) if you ever need to.
    DOWNLOAD_RECORDINGS = os.getenv("DOWNLOAD_RECORDINGS", "true").lower() == "true"

    @classmethod
    def validate(cls, dry_run: bool) -> list:
        missing = []
        for key in ("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT"):
            if not getattr(cls, key):
                missing.append(key)
        if not dry_run:
            box_keys = (("BOX_FOLDER_ID",) if cls.BOX_DEV_TOKEN else
                        ("BOX_JWT_CONFIG", "BOX_FOLDER_ID"))
            for key in box_keys:
                if not getattr(cls, key):
                    missing.append(key)
        return missing


# CSV schema -- mirrors RingCentral's own call-log export, plus recording info.
CSV_COLUMNS = [
    "Record ID", "Start Time (UTC)", "Start Time (Local)", "Type", "Direction",
    "From", "From Name", "To", "To Name", "Extension", "Action", "Result",
    "Duration (sec)", "Duration (hh:mm:ss)", "Recording", "Recording File",
]

# ----------------------------------------------------------------------------
# State (so scheduled runs resume and never duplicate)
# ----------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("state.json unreadable; starting fresh")
    return {"last_run_utc": None, "uploaded_recordings": []}


def save_state(state: dict) -> None:
    # Keep the recording-id list from growing forever. (Box-side existence
    # checks still dedupe anything that falls off the end of this list.)
    state["uploaded_recordings"] = state.get("uploaded_recordings", [])[-50000:]
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# RingCentral
# ----------------------------------------------------------------------------

class RingCentral:
    def __init__(self):
        from ringcentral import SDK
        self.sdk = SDK(Config.RC_CLIENT_ID, Config.RC_CLIENT_SECRET,
                       Config.RC_SERVER_URL)
        self.platform = self.sdk.platform()

    def login(self):
        self.platform.login(jwt=Config.RC_JWT)
        log.info("RingCentral: authenticated via JWT")

    # -- call log ------------------------------------------------------------

    def fetch_call_log(self, date_from: datetime, date_to: datetime) -> list:
        """Page through the company-level call log. Returns raw records.
        NOTE: the call-log response often omits paging.totalPages, so we
        keep going as long as navigation.nextPage exists or the page came
        back full -- otherwise large ranges get silently truncated."""
        records, page = [], 1
        per_page = 250
        params = {
            "view": "Detailed",
            "dateFrom": date_from.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "dateTo": date_to.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            "perPage": per_page,
        }
        while True:
            params["page"] = page
            resp = self._get_with_retry("/restapi/v1.0/account/~/call-log", params)
            if resp is None:
                raise RuntimeError(
                    f"Call-log fetch failed after retries (page {page}); "
                    "aborting run so the date range can be retried next time.")
            data = resp.json_dict()
            batch = data.get("records", [])
            records.extend(batch)
            log.info("RingCentral: call-log page %d (%d records so far)",
                     page, len(records))
            nav_next = (data.get("navigation", {}) or {}).get("nextPage")
            has_next = bool(nav_next and nav_next.get("uri"))
            if not has_next and len(batch) < per_page:
                break
            page += 1
            time.sleep(0.5)   # call-log GETs are 'Heavy' tier too
        return records

    # -- extensions ----------------------------------------------------------

    def fetch_extension_map(self) -> dict:
        """{extension id (str): (name, extensionNumber)} for every extension
        on the account -- used to put the VAN/asset name into filenames."""
        ext_map, page, total_pages = {}, 1, 1
        while page <= total_pages:
            resp = self._get_with_retry("/restapi/v1.0/account/~/extension",
                                        {"perPage": 1000, "page": page})
            if resp is None:
                log.warning("Could not fetch extension list; filenames will "
                            "fall back to call-log leg names")
                return ext_map
            data = resp.json_dict()
            for ext in data.get("records", []):
                ext_map[str(ext.get("id", ""))] = (
                    ext.get("name", ""), ext.get("extensionNumber", ""))
            total_pages = data.get("paging", {}).get("totalPages", 1)
            page += 1
        n_ext = len(ext_map)

        # Also key the same info by direct phone number ("num:<digits>") --
        # some inbound call-log records carry no extension id, only the
        # number that was dialed.
        page, total_pages = 1, 1
        while page <= total_pages:
            resp = self._get_with_retry("/restapi/v1.0/account/~/phone-number",
                                        {"perPage": 1000, "page": page})
            if resp is None:
                break
            data = resp.json_dict()
            for pn in data.get("records", []):
                ext_id = str((pn.get("extension") or {}).get("id", ""))
                digits = re.sub(r"\D", "", pn.get("phoneNumber", ""))
                if digits and ext_id in ext_map:
                    ext_map[f"num:{digits}"] = ext_map[ext_id]
            total_pages = data.get("paging", {}).get("totalPages", 1)
            page += 1
        log.info("RingCentral: loaded %d extensions / %d direct numbers "
                 "for asset naming", n_ext, len(ext_map) - n_ext)
        return ext_map

    # -- recordings ----------------------------------------------------------

    def download_recording(self, recording_id: str, dest: Path) -> bool:
        """Download one recording MP3 to dest. Returns True on success."""
        uri = f"/restapi/v1.0/account/~/recording/{recording_id}/content"
        resp = self._get_with_retry(uri)
        if resp is None:
            return False
        dest.write_bytes(resp.response().content)
        return True

    # -- plumbing ------------------------------------------------------------

    @staticmethod
    def _sanitize(err) -> str:
        """Exception text minus the SDK's request-details blob, which includes
        the Authorization bearer token and must never reach the log file."""
        return str(err).split("(request details:")[0].strip()

    def _get_with_retry(self, url: str, params: dict = None, max_attempts: int = 5):
        """GET with 429 (rate limit) handling. RC media endpoints are 'Heavy':
        ~10 requests/minute, so on 429 we wait out the full window."""
        for attempt in range(1, max_attempts + 1):
            try:
                return self.platform.get(url, params)
            except Exception as e:
                status, retry_after = None, 60
                try:
                    # ApiException.api_response is a METHOD on the RC SDK
                    r = e.api_response().response()
                    status = r.status_code
                    retry_after = int(r.headers.get("Retry-After", "60"))
                except Exception:
                    pass
                if status == 429 and attempt < max_attempts:
                    wait = max(retry_after, 30)
                    log.warning("RingCentral rate limit hit; sleeping %ds "
                                "(attempt %d/%d)", wait, attempt, max_attempts)
                    time.sleep(wait)
                    continue
                if attempt < max_attempts:
                    log.warning("RingCentral request failed (%s); retrying in 10s "
                                "(attempt %d/%d)", self._sanitize(e), attempt, max_attempts)
                    time.sleep(10)
                    continue
                log.error("RingCentral request permanently failed: %s | url=%s",
                          self._sanitize(e), url)
                return None
        return None


# ----------------------------------------------------------------------------
# Box
# ----------------------------------------------------------------------------

class Box:
    def __init__(self):
        from box_sdk_gen import BoxClient, BoxDeveloperTokenAuth
        if Config.BOX_DEV_TOKEN:
            log.info("Box: using developer token (testing only)")
            auth = BoxDeveloperTokenAuth(token=Config.BOX_DEV_TOKEN)
        else:
            from box_sdk_gen import BoxJWTAuth, JWTConfig
            import json as _json
            with open(Config.BOX_JWT_CONFIG) as f:
                cfg = _json.load(f)
            app = cfg["boxAppSettings"]
            jwt_cfg = JWTConfig(
                client_id=app["clientID"],
                client_secret=app["clientSecret"],
                jwt_key_id=app["appAuth"]["publicKeyID"],
                private_key=app["appAuth"]["privateKey"],
                private_key_passphrase=app["appAuth"]["passphrase"],
                enterprise_id=cfg["enterpriseID"],
            )
            auth = BoxJWTAuth(config=jwt_cfg)
            log.info("Box: using JWT auth (rcToBoxArchiver)")
        self.client = BoxClient(auth=auth)
        self._folder_listing_cache = {}

    def whoami(self) -> str:
        me = self.client.users.get_user_me()
        log.info("Box: authenticated as %s (%s)", me.name, me.login)
        return me.id

    # -- folder helpers --------------------------------------------------------

    def list_folder(self, folder_id: str, force: bool = False) -> dict:
        """Return {item_name: (item_id, item_type)} for a folder (cached)."""
        if not force and folder_id in self._folder_listing_cache:
            return self._folder_listing_cache[folder_id]
        items, offset = {}, 0
        while True:
            page = self.client.folders.get_folder_items(
                folder_id, limit=1000, offset=offset)
            for entry in page.entries:
                items[entry.name] = (entry.id, entry.type.value
                                     if hasattr(entry.type, "value") else str(entry.type))
            if offset + 1000 >= (page.total_count or 0):
                break
            offset += 1000
        self._folder_listing_cache[folder_id] = items
        return items

    def ensure_subfolder(self, parent_id: str, name: str) -> str:
        from box_sdk_gen.managers.folders import CreateFolderParent
        existing = self.list_folder(parent_id)
        if name in existing and existing[name][1] == "folder":
            return existing[name][0]
        folder = self.client.folders.create_folder(
            name=name, parent=CreateFolderParent(id=parent_id))
        self._folder_listing_cache.pop(parent_id, None)
        log.info("Box: created subfolder '%s' (%s)", name, folder.id)
        return folder.id

    # -- file ops ----------------------------------------------------------------

    def upload_new_file(self, folder_id: str, name: str, content: bytes) -> str:
        from box_sdk_gen.managers.uploads import (
            UploadFileAttributes, UploadFileAttributesParentField)
        attrs = UploadFileAttributes(
            name=name, parent=UploadFileAttributesParentField(id=folder_id))
        result = self.client.uploads.upload_file(attrs, io.BytesIO(content))
        self._folder_listing_cache.pop(folder_id, None)
        return result.entries[0].id

    def upload_new_version(self, file_id: str, name: str, content: bytes) -> str:
        from box_sdk_gen.managers.uploads import UploadFileVersionAttributes
        attrs = UploadFileVersionAttributes(name=name)
        result = self.client.uploads.upload_file_version(
            file_id, attrs, io.BytesIO(content))
        return result.entries[0].id

    def download_file(self, file_id: str) -> bytes:
        stream = self.client.downloads.download_file(file_id)
        return stream.read()

    def move_and_rename_file(self, file_id: str, new_name: str,
                             new_parent_id: str) -> None:
        from box_sdk_gen.managers.files import UpdateFileByIdParent
        self.client.files.update_file_by_id(
            file_id, name=new_name, parent=UpdateFileByIdParent(id=new_parent_id))

    def delete_file(self, file_id: str) -> None:
        self.client.files.delete_file_by_id(file_id)

    def copy_file(self, file_id: str, dest_folder_id: str, new_name: str) -> str:
        from box_sdk_gen.managers.files import CopyFileParent
        result = self.client.files.copy_file(
            file_id, CopyFileParent(id=dest_folder_id), name=new_name)
        return result.id


# ----------------------------------------------------------------------------
# Transform: RC call-log records -> CSV rows / recording jobs
# ----------------------------------------------------------------------------

def _fmt_duration(seconds) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{s:02d}"


def _leg_phone(leg: dict) -> tuple:
    """(number, name) from a call-log 'from'/'to' block."""
    if not leg:
        return "", ""
    return (leg.get("phoneNumber") or leg.get("extensionNumber") or "",
            leg.get("name") or "")


def month_folder_name(dt: datetime) -> str:
    """Box subfolder per call month: 'June 2026'."""
    return dt.strftime("%B %Y")


def _clean_component(s) -> str:
    """Make one filename field safe: no underscores (they separate fields),
    no characters Windows/Box reject, collapsed whitespace."""
    s = re.sub(r'[\\/:*?"<>|_]', " ", str(s or ""))
    return re.sub(r"\s+", " ", s).strip()


def _asset_info(record: dict, ext_map: dict = None) -> tuple:
    """(name, number, extension) of the company line (VAN/asset) on a call:
    the 'to' leg on inbound calls, the 'from' leg on outbound. Resolved via
    the extension id on the record, falling back to the line's direct
    phone number, falling back to whatever name is on the call leg."""
    leg = (record.get("from") if record.get("direction") == "Outbound"
           else record.get("to")) or {}
    number = re.sub(r"\D", "", str(leg.get("phoneNumber")
                                   or leg.get("extensionNumber") or ""))
    ext_id = str((record.get("extension") or {}).get("id", ""))
    ext_name, ext_number = ((ext_map or {}).get(ext_id)
                            or (ext_map or {}).get(f"num:{number}")
                            or ("", ""))
    name = _clean_component(ext_name or leg.get("name")) or "Unknown"
    extension = _clean_component(ext_number or leg.get("extensionNumber")
                                 or "") or "noext"
    return name, number or "unknown", extension


def recording_filename(record: dict, ext_map: dict = None) -> str:
    """20260611-194559_VAN 109_15550100890_109.mp3
    = call time _ asset (VAN) name _ asset phone number _ extension."""
    start = record.get("startTime", "")
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        stamp = dt.strftime("%Y%m%d-%H%M%S")
    except ValueError:
        stamp = "00000000-000000"
    asset, number, extension = _asset_info(record, ext_map)
    return f"{stamp}_{asset}_{number}_{extension}.mp3"


def record_to_row(record: dict, ext_map: dict = None) -> dict:
    from_num, from_name = _leg_phone(record.get("from", {}))
    to_num, to_name = _leg_phone(record.get("to", {}))
    rec = record.get("recording") or {}
    start_utc = record.get("startTime", "")
    local_str = ""
    try:
        dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        local_str = dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    ext = record.get("extension", {}) or {}
    _, _, ext_number = _asset_info(record, ext_map)
    return {
        "Record ID": record.get("id", ""),
        "Start Time (UTC)": start_utc,
        "Start Time (Local)": local_str,
        "Type": record.get("type", ""),
        "Direction": record.get("direction", ""),
        "From": from_num,
        "From Name": from_name,
        "To": to_num,
        "To Name": to_name,
        "Extension": ext.get("extensionNumber", "")
                     or (ext_number if ext_number != "noext" else "")
                     or ext.get("id", ""),
        "Action": record.get("action", ""),
        "Result": record.get("result", ""),
        "Duration (sec)": record.get("duration", ""),
        "Duration (hh:mm:ss)": _fmt_duration(record.get("duration")),
        "Recording": "Yes" if rec.get("id") else "No",
        "Recording File": recording_filename(record, ext_map) if rec.get("id") else "",
    }


def csv_name_for(dt: datetime) -> str:
    if Config.CSV_ROTATION == "never":
        return "Call Log - Master.csv"
    return f"Call Log - {dt.strftime('%Y-%m')}.csv"


def merge_csv(existing_bytes: bytes, new_rows: list) -> tuple:
    """Merge new rows into an existing CSV, dedupe on Record ID,
    keep everything sorted by start time. Returns (bytes, added_count)."""
    rows_by_id = {}
    if existing_bytes:
        reader = csv.DictReader(io.StringIO(existing_bytes.decode("utf-8-sig")))
        for row in reader:
            if row.get("Record ID"):
                rows_by_id[row["Record ID"]] = {c: row.get(c, "") for c in CSV_COLUMNS}
    before = len(rows_by_id)
    for row in new_rows:
        rows_by_id[row["Record ID"]] = row
    added = len(rows_by_id) - before

    merged = sorted(rows_by_id.values(),
                    key=lambda r: r.get("Start Time (UTC)", ""))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(merged)
    return buf.getvalue().encode("utf-8-sig"), added


# ----------------------------------------------------------------------------
# Main archive run
# ----------------------------------------------------------------------------

def run(date_from: datetime, date_to: datetime, dry_run: bool) -> int:
    state = load_state()
    uploaded_recordings = set(state.get("uploaded_recordings", []))

    # ---- 1. Pull call log from RingCentral ----
    rc = RingCentral()
    rc.login()
    records = rc.fetch_call_log(date_from, date_to)
    log.info("Fetched %d call-log records (%s -> %s)",
             len(records), date_from.isoformat(), date_to.isoformat())
    if not records:
        log.info("Nothing to archive. Done.")
        if not dry_run:
            state["last_run_utc"] = date_to.isoformat()
            save_state(state)
        return 0

    ext_map = rc.fetch_extension_map()

    # ---- 2. Group rows by month folder + destination CSV ----
    # Everything for a call lands in one "<Month> <Year>" subfolder, e.g.
    # "June 2026". A master CSV (CSV_ROTATION=never) stays at the top level.
    rows_by_dest = {}        # (month_folder | None, csv_name) -> [rows]
    month_of_record = {}     # call record id -> month folder name
    for record in records:
        try:
            dt = datetime.fromisoformat(
                record.get("startTime", "").replace("Z", "+00:00"))
        except ValueError:
            dt = date_from
        month = month_folder_name(dt)
        month_of_record[record.get("id")] = month
        dest_month = None if Config.CSV_ROTATION == "never" else month
        rows_by_dest.setdefault((dest_month, csv_name_for(dt)), []).append(
            record_to_row(record, ext_map))

    # ---- 3. Connect to Box (unless dry run) ----
    box = None
    month_folder_ids = {}        # month name -> Box folder id
    recordings_folder_ids = {}   # month name -> folder id MP3s go into

    def folder_for_month(month: str) -> str:
        if month not in month_folder_ids:
            month_folder_ids[month] = box.ensure_subfolder(
                Config.BOX_FOLDER_ID, month)
        return month_folder_ids[month]

    def recordings_folder_for_month(month: str) -> str:
        if month not in recordings_folder_ids:
            parent = folder_for_month(month)
            recordings_folder_ids[month] = (
                box.ensure_subfolder(parent, "Recordings")
                if Config.RECORDINGS_SUBFOLDER else parent)
        return recordings_folder_ids[month]

    if not dry_run:
        box = Box()
        box.whoami()
    else:
        LOCAL_OUTPUT.mkdir(exist_ok=True)
        log.info("DRY RUN: writing everything to %s instead of Box", LOCAL_OUTPUT)

    # ---- 4. Merge + upload the combined CSV(s) into their month folder ----
    for (month, csv_name), new_rows in sorted(
            rows_by_dest.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])):
        label = f"{month + '/' if month else ''}{csv_name}"
        if dry_run:
            local_dir = LOCAL_OUTPUT / month if month else LOCAL_OUTPUT
            local_dir.mkdir(parents=True, exist_ok=True)
            local = local_dir / csv_name
            existing = local.read_bytes() if local.exists() else b""
            merged, added = merge_csv(existing, new_rows)
            local.write_bytes(merged)
            log.info("DRY RUN: %s -> +%d new rows (local)", label, added)
            continue

        parent_id = folder_for_month(month) if month else Config.BOX_FOLDER_ID
        folder_items = box.list_folder(parent_id, force=True)
        if csv_name in folder_items and folder_items[csv_name][1] == "file":
            file_id = folder_items[csv_name][0]
            existing = box.download_file(file_id)
            merged, added = merge_csv(existing, new_rows)
            if added == 0:
                log.info("%s: no new calls, skipping version upload", label)
                continue
            box.upload_new_version(file_id, csv_name, merged)
            log.info("%s: appended %d new calls (new Box version)", label, added)
        else:
            merged, added = merge_csv(b"", new_rows)
            box.upload_new_file(parent_id, csv_name, merged)
            log.info("%s: created with %d calls", label, added)

    # ---- 5. Download + upload recordings ----
    failures = 0
    if Config.DOWNLOAD_RECORDINGS:
        TEMP_DIR.mkdir(exist_ok=True)
        to_fetch = [r for r in records
                    if (r.get("recording") or {}).get("id")
                    and (r["recording"]["id"] not in uploaded_recordings)]
        log.info("Recordings to archive: %d (skipping %d already uploaded)",
                 len(to_fetch),
                 sum(1 for r in records
                     if (r.get("recording") or {}).get("id") in uploaded_recordings))

        existing_by_month = {}   # month -> {name: (id, type)} in its Box folder
        used_names = set()       # collision guard within this run

        for i, record in enumerate(to_fetch, 1):
            rec_id = record["recording"]["id"]
            fname = recording_filename(record, ext_map)
            month = (month_of_record.get(record.get("id"))
                     or month_folder_name(date_from))
            # Same second + same asset = same name; disambiguate with the
            # recording id rather than silently skipping one of the calls.
            if fname in used_names:
                fname = f"{fname[:-4]}_{rec_id}.mp3"
            used_names.add(fname)
            try:
                if not dry_run:
                    if month not in existing_by_month:
                        existing_by_month[month] = box.list_folder(
                            recordings_folder_for_month(month), force=True)
                    if fname in existing_by_month[month]:
                        log.info("[%d/%d] %s already in Box, skipping",
                                 i, len(to_fetch), fname)
                        uploaded_recordings.add(rec_id)
                        continue

                tmp = TEMP_DIR / fname
                if not rc.download_recording(rec_id, tmp):
                    failures += 1
                    continue

                if dry_run:
                    local_dir = LOCAL_OUTPUT / month
                    local_dir.mkdir(parents=True, exist_ok=True)
                    (local_dir / fname).write_bytes(tmp.read_bytes())
                    log.info("[%d/%d] DRY RUN: saved %s/%s locally (%.1f KB)",
                             i, len(to_fetch), month, fname,
                             tmp.stat().st_size / 1024)
                else:
                    box.upload_new_file(recordings_folder_for_month(month),
                                        fname, tmp.read_bytes())
                    log.info("[%d/%d] uploaded %s/%s (%.1f KB)",
                             i, len(to_fetch), month, fname,
                             tmp.stat().st_size / 1024)
                uploaded_recordings.add(rec_id)
                tmp.unlink(missing_ok=True)
            except Exception as e:
                failures += 1
                log.error("[%d/%d] failed on %s: %s", i, len(to_fetch), fname, e)
            finally:
                time.sleep(Config.MEDIA_THROTTLE_SECONDS)

    # ---- 6. Save state (never during a dry run, so the next real run
    # doesn't think these recordings are already in Box) ----
    if not dry_run:
        state["uploaded_recordings"] = sorted(uploaded_recordings)
        # A historical backfill must not rewind the resume pointer.
        prev = state.get("last_run_utc")
        if not prev or date_to.isoformat() > prev:
            state["last_run_utc"] = date_to.isoformat()
        save_state(state)

    log.info("Run complete. %d failures.", failures)
    return 1 if failures else 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Archive RingCentral call logs + "
                                            "recordings into one Box folder.")
    p.add_argument("--from", dest="date_from",
                   help="Start date (YYYY-MM-DD). Default: last run / yesterday.")
    p.add_argument("--to", dest="date_to",
                   help="End date (YYYY-MM-DD, exclusive of future). Default: now.")
    p.add_argument("--dry-run", action="store_true",
                   help="Fetch from RingCentral but write locally, not to Box.")
    return p.parse_args()


def main():
    args = parse_args()
    dry_run = args.dry_run or os.getenv("DRY_RUN", "").lower() == "true"

    missing = Config.validate(dry_run)
    if missing:
        log.error("Missing required .env settings: %s", ", ".join(missing))
        log.error("Copy .env.example to .env and fill these in.")
        sys.exit(2)

    now = datetime.now(timezone.utc)
    state = load_state()

    if args.date_from:
        date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(
            tzinfo=timezone.utc)
    elif state.get("last_run_utc"):
        # small overlap so nothing falls through the cracks; dedupe handles it
        date_from = datetime.fromisoformat(state["last_run_utc"]) - timedelta(hours=1)
    else:
        date_from = (now - timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)

    if args.date_to:
        date_to = (datetime.strptime(args.date_to, "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc) + timedelta(days=1))
        date_to = min(date_to, now)
    else:
        date_to = now

    log.info("=== RingCentral Archiver run: %s -> %s%s ===",
             date_from.isoformat(), date_to.isoformat(),
             " (DRY RUN)" if dry_run else "")
    sys.exit(run(date_from, date_to, dry_run))


if __name__ == "__main__":
    main()
