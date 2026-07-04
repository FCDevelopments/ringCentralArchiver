"""
Backfill historical recordings WITHOUT re-downloading from RingCentral.

The built-in RingCentral Archiver already pushed recordings into Box under
  RingCentral Archiver/<Line Name>_<ext>/<YYYY-M-D>/Call Recording/*.mp3
with filenames ending in the RingCentral recording id, e.g.
  20260510-100212_15550100567_15550100234#135_Automatic_3636328599023.mp3

This script:
  1. Fetches the call log for the date range (cheap; no media downloads).
  2. Builds/merges the monthly CSV(s) into the "<Month> <Year>" folder(s).
  3. Indexes the old archiver tree and matches files to call-log records by
     recording id (index is cached locally so re-runs after a token refresh
     skip straight to copying).
  4. COPIES each matched file Box-side into its month folder under the new
     <stamp>_<asset>_<number>_<extension>.mp3 name. Box-to-Box copies do not
     touch RingCentral's media rate limit. Originals are left untouched.

Already-copied files are skipped, so the script can be re-run any number of
times (e.g. after a developer-token refresh) and just continues.

Recordings with no matching file in the old archive are reported at the end;
a normal `archiver.py --from ... --to ...` run will download just those.

Usage:
  python backfill_from_box.py --from 2026-05-01 --to 2026-05-31 --dry-run
  python backfill_from_box.py --from 2026-05-01 --to 2026-05-31
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from archiver import (Box, Config, RingCentral, csv_name_for, load_state,
                      log, merge_csv, month_folder_name, record_to_row,
                      recording_filename, save_state)

# The Box folder ID of your OLD/built-in RingCentral Archiver output -- this
# script recovers and reorganizes recordings RingCentral's native archiver
# had already placed in Box before this tool existed. Find it in the Box
# folder URL: https://app.box.com/folder/123456789 -> 123456789
SOURCE_FOLDER_ID = os.getenv("SOURCE_FOLDER_ID", "YOUR_LEGACY_ARCHIVE_FOLDER_ID")
# TODO: set to the Box folder ID of your OLD RingCentral archiver output
INDEX_CACHE = Path(__file__).resolve().parent / "box_index_cache.json"
RECORDS_CACHE = Path(__file__).resolve().parent / "call_log_cache.json"

REC_ID_RE = re.compile(r"_(\d{8,})\.mp3$", re.IGNORECASE)
DAY_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def index_old_archive(box: Box, date_from: datetime, date_to: datetime) -> dict:
    """{recording_id: box_file_id} for every old-archiver MP3 in range.
    Cached to disk because building it costs thousands of Box list calls."""
    cache_key = f"{SOURCE_FOLDER_ID}:{date_from.date()}:{date_to.date()}"
    if INDEX_CACHE.exists():
        try:
            cached = json.loads(INDEX_CACHE.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key:
                log.info("Using cached old-archive index (%d files). "
                         "Delete %s to force a rescan.",
                         len(cached["files"]), INDEX_CACHE.name)
                return cached["files"]
        except Exception:
            pass

    index = {}
    lines = box.list_folder(SOURCE_FOLDER_ID, force=True)
    log.info("Indexing old archive: %d line folders ...", len(lines))
    for li, (line_name, (line_id, line_type)) in enumerate(sorted(lines.items()), 1):
        if line_type != "folder":
            continue
        for day_name, (day_id, day_type) in box.list_folder(line_id).items():
            if day_type != "folder":
                continue
            m = DAY_RE.match(day_name)
            if not m:
                continue
            day = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                           tzinfo=timezone.utc)
            # day folders are line-local dates; allow 1 day of slack around
            # the UTC range so boundary calls aren't missed
            if not (date_from - timedelta(days=1) <= day <= date_to + timedelta(days=1)):
                continue
            for sub_name, (sub_id, sub_type) in box.list_folder(day_id).items():
                if sub_type == "folder":
                    for fname, (fid, ftype) in box.list_folder(sub_id).items():
                        m2 = REC_ID_RE.search(fname)
                        if ftype == "file" and m2:
                            index[m2.group(1)] = fid
                elif REC_ID_RE.search(sub_name):
                    index[sub_name and REC_ID_RE.search(sub_name).group(1)] = sub_id
        if li % 20 == 0:
            log.info("  ... %d/%d line folders indexed (%d files so far)",
                     li, len(lines), len(index))

    INDEX_CACHE.write_text(json.dumps({"key": cache_key, "files": index}),
                           encoding="utf-8")
    log.info("Old archive index complete: %d recordings (cached to %s)",
             len(index), INDEX_CACHE.name)
    return index


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="date_from", required=True)
    p.add_argument("--to", dest="date_to", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-csv", action="store_true",
                   help="Skip the CSV build (if it's already up to date)")
    args = p.parse_args()

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    date_to = (datetime.strptime(args.date_to, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) + timedelta(days=1))

    # ---- RingCentral: call log + asset names (cached -- historical data
    # can't change, and re-fetching costs ~10 min per token session) ----
    cache_key = f"{args.date_from}:{args.date_to}"
    records = ext_map = None
    if RECORDS_CACHE.exists():
        try:
            cached = json.loads(RECORDS_CACHE.read_text(encoding="utf-8"))
            if cached.get("key") == cache_key:
                records, ext_map = cached["records"], cached["ext_map"]
                log.info("Using cached call log (%d records). Delete %s to "
                         "force a refetch.", len(records), RECORDS_CACHE.name)
        except Exception:
            pass
    if records is None:
        rc = RingCentral()
        rc.login()
        records = rc.fetch_call_log(date_from, date_to)
        ext_map = rc.fetch_extension_map()
        RECORDS_CACHE.write_text(json.dumps(
            {"key": cache_key, "records": records, "ext_map": ext_map}),
            encoding="utf-8")
    with_rec = [r for r in records if (r.get("recording") or {}).get("id")]
    log.info("Call log: %d records, %d with recordings", len(records), len(with_rec))

    box = Box()
    box.whoami()

    # ---- Month folders ----
    months = {}     # month name -> folder id

    def month_id(month: str) -> str:
        if month not in months:
            months[month] = box.ensure_subfolder(Config.BOX_FOLDER_ID, month)
        return months[month]

    def month_of(record) -> str:
        try:
            return month_folder_name(datetime.fromisoformat(
                record.get("startTime", "").replace("Z", "+00:00")))
        except ValueError:
            return month_folder_name(date_from)

    # ---- CSVs ----
    if not args.skip_csv:
        rows_by_dest = {}
        for record in records:
            try:
                dt = datetime.fromisoformat(
                    record.get("startTime", "").replace("Z", "+00:00"))
            except ValueError:
                dt = date_from
            rows_by_dest.setdefault(
                (month_folder_name(dt), csv_name_for(dt)), []).append(
                    record_to_row(record, ext_map))
        for (month, csv_name), rows in sorted(rows_by_dest.items()):
            if args.dry_run:
                log.info("DRY RUN CSV: %s/%s with %d rows", month, csv_name, len(rows))
                continue
            mid = month_id(month)
            items = box.list_folder(mid, force=True)
            if csv_name in items and items[csv_name][1] == "file":
                file_id = items[csv_name][0]
                merged, added = merge_csv(box.download_file(file_id), rows)
                if added:
                    box.upload_new_version(file_id, csv_name, merged)
                log.info("%s/%s: +%d rows (now complete)", month, csv_name, added)
            else:
                merged, _ = merge_csv(b"", rows)
                box.upload_new_file(mid, csv_name, merged)
                log.info("%s/%s: created with %d rows", month, csv_name, len(rows))

    # ---- Index old archive + copy ----
    index = index_old_archive(box, date_from, date_to)

    existing = {}   # month -> names already in dest folder
    copied = skipped = unmatched = failed = consecutive_failures = 0
    new_names_used = set()
    state = load_state()
    uploaded = set(state.get("uploaded_recordings", []))

    for i, record in enumerate(with_rec, 1):
        rec_id = str(record["recording"]["id"])
        month = month_of(record)
        new_name = recording_filename(record, ext_map)
        if new_name in new_names_used:
            new_name = f"{new_name[:-4]}_{rec_id}.mp3"
        new_names_used.add(new_name)

        if rec_id not in index:
            unmatched += 1
            continue
        if args.dry_run:
            if copied < 10:
                log.info("DRY RUN copy -> %s/%s", month, new_name)
            copied += 1
            continue

        if month not in existing:
            existing[month] = box.list_folder(month_id(month), force=True)
        if new_name in existing[month]:
            skipped += 1
            uploaded.add(rec_id)
            continue
        try:
            box.copy_file(index[rec_id], month_id(month), new_name)
            copied += 1
            consecutive_failures = 0
            uploaded.add(rec_id)
            if copied % 100 == 0:
                log.info("[%d/%d] %d copied so far ...", i, len(with_rec), copied)
        except Exception as e:
            failed += 1
            consecutive_failures += 1
            log.error("copy failed for %s (%s): %s", new_name, rec_id,
                      str(e)[:200])
            if consecutive_failures >= 15:
                log.error("15 copy failures in a row -- the dev token has "
                          "probably expired. Stopping here; paste a fresh "
                          "token and re-run the same command to resume.")
                break

    if not args.dry_run:
        state["uploaded_recordings"] = sorted(uploaded)
        save_state(state)

    log.info("Backfill %s: %d copied, %d already there, %d not in old "
             "archive, %d failed",
             "DRY RUN" if args.dry_run else "complete",
             copied, skipped, unmatched, failed)
    if unmatched:
        log.info("The %d unmatched recordings can be fetched the slow way "
                 "with:  python archiver.py --from %s --to %s",
                 unmatched, args.date_from, args.date_to)


if __name__ == "__main__":
    main()
