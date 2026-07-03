"""
One-time migration to the new Box layout.

The first live run uploaded everything FLAT into the Box root folder with
MP3s named  <stamp>_<from>_<to>_<recordingId>.mp3.  This script reorganizes
those existing files in place (no re-downloading from RingCentral):

  1. Re-fetches the call log for the date range to know which file is which.
  2. MOVES each old MP3 into its "<Month> <Year>" subfolder and RENAMES it
     to the new  <stamp>_<asset/VAN name>_<asset number>_<extension>.mp3.
  3. Rebuilds the monthly CSV (the Recording File column changed) inside the
     month folder, then deletes the old root-level CSV.

Usage:
  python migrate_existing.py --from 2026-06-11 --to 2026-06-11 --dry-run
  python migrate_existing.py --from 2026-06-11 --to 2026-06-11
"""

import argparse
import sys
from datetime import datetime, timedelta, timezone

from archiver import (Box, Config, RingCentral, csv_name_for, log,
                      merge_csv, month_folder_name, record_to_row,
                      recording_filename)


def old_recording_filename(record: dict) -> str:
    """The naming scheme the first live run used -- needed to find files."""
    start = record.get("startTime", "")
    try:
        dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        stamp = dt.strftime("%Y%m%d-%H%M%S")
    except ValueError:
        stamp = "00000000-000000"
    from_leg = record.get("from") or {}
    to_leg = record.get("to") or {}
    from_num = from_leg.get("phoneNumber") or from_leg.get("extensionNumber") or ""
    to_num = to_leg.get("phoneNumber") or to_leg.get("extensionNumber") or ""
    rec_id = (record.get("recording") or {}).get("id", "unknown")
    clean = lambda s: "".join(c for c in str(s) if c.isalnum())
    return (f"{stamp}_{clean(from_num) or 'unknown'}_"
            f"{clean(to_num) or 'unknown'}_{rec_id}.mp3")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--from", dest="date_from", required=True,
                   help="Start date of the already-archived range (YYYY-MM-DD)")
    p.add_argument("--to", dest="date_to", required=True,
                   help="End date of the already-archived range (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true",
                   help="Only print what would be moved/renamed")
    args = p.parse_args()

    date_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(
        tzinfo=timezone.utc)
    date_to = (datetime.strptime(args.date_to, "%Y-%m-%d").replace(
        tzinfo=timezone.utc) + timedelta(days=1))

    missing = [k for k in ("RC_CLIENT_ID", "RC_CLIENT_SECRET", "RC_JWT",
                           "BOX_FOLDER_ID") if not getattr(Config, k)]
    if missing:
        log.error("Missing .env settings: %s", ", ".join(missing))
        sys.exit(2)

    # ---- RingCentral: what SHOULD everything be called? ----
    rc = RingCentral()
    rc.login()
    records = rc.fetch_call_log(date_from, date_to)
    ext_map = rc.fetch_extension_map()
    with_rec = [r for r in records if (r.get("recording") or {}).get("id")]
    log.info("Call log: %d records, %d with recordings", len(records), len(with_rec))

    # ---- Box: what IS in the root folder? ----
    box = Box()
    box.whoami()
    root_items = box.list_folder(Config.BOX_FOLDER_ID, force=True)

    # ---- Plan MP3 moves ----
    moves, missing_in_box, new_names_used = [], 0, set()
    for record in with_rec:
        old = old_recording_filename(record)
        if old not in root_items or root_items[old][1] != "file":
            missing_in_box += 1
            continue
        new = recording_filename(record, ext_map)
        if new in new_names_used:
            new = f"{new[:-4]}_{record['recording']['id']}.mp3"
        new_names_used.add(new)
        try:
            dt = datetime.fromisoformat(
                record.get("startTime", "").replace("Z", "+00:00"))
        except ValueError:
            dt = date_from
        moves.append((root_items[old][0], old, new, month_folder_name(dt)))

    log.info("MP3s to move+rename: %d (%d expected files not found in root)",
             len(moves), missing_in_box)

    # ---- Plan CSV rebuilds ----
    rows_by_month = {}
    for record in records:
        try:
            dt = datetime.fromisoformat(
                record.get("startTime", "").replace("Z", "+00:00"))
        except ValueError:
            dt = date_from
        rows_by_month.setdefault(
            (month_folder_name(dt), csv_name_for(dt)), []).append(
                record_to_row(record, ext_map))

    if args.dry_run:
        for _, old, new, month in moves[:10]:
            log.info("DRY RUN move: %s  ->  %s/%s", old, month, new)
        if len(moves) > 10:
            log.info("DRY RUN ... and %d more", len(moves) - 10)
        for (month, csv_name), rows in sorted(rows_by_month.items()):
            log.info("DRY RUN CSV: rebuild %s/%s with %d rows%s",
                     month, csv_name, len(rows),
                     "; delete old root copy" if csv_name in root_items else "")
        log.info("DRY RUN complete. Nothing changed.")
        return

    # ---- Execute MP3 moves ----
    month_ids = {}
    for i, (file_id, old, new, month) in enumerate(moves, 1):
        if month not in month_ids:
            month_ids[month] = box.ensure_subfolder(Config.BOX_FOLDER_ID, month)
        try:
            box.move_and_rename_file(file_id, new, month_ids[month])
            log.info("[%d/%d] %s  ->  %s/%s", i, len(moves), old, month, new)
        except Exception as e:
            log.error("[%d/%d] FAILED %s: %s", i, len(moves), old, e)

    # ---- Rebuild CSVs in month folders, remove old root copies ----
    for (month, csv_name), rows in sorted(rows_by_month.items()):
        month_id = month_ids.get(month) or box.ensure_subfolder(
            Config.BOX_FOLDER_ID, month)
        month_items = box.list_folder(month_id, force=True)
        if csv_name in month_items and month_items[csv_name][1] == "file":
            file_id = month_items[csv_name][0]
            merged, added = merge_csv(box.download_file(file_id), rows)
            box.upload_new_version(file_id, csv_name, merged)
            log.info("%s/%s: merged %d rows into existing CSV", month, csv_name, added)
        else:
            merged, _ = merge_csv(b"", rows)
            box.upload_new_file(month_id, csv_name, merged)
            log.info("%s/%s: created with %d rows", month, csv_name, len(rows))

        if csv_name in root_items and root_items[csv_name][1] == "file":
            box.delete_file(root_items[csv_name][0])
            log.info("deleted old root-level %s", csv_name)

    log.info("Migration complete.")


if __name__ == "__main__":
    main()
