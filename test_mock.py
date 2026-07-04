"""
Mock end-to-end test for archiver.py -- no real credentials needed.
Simulates RingCentral (call log + extensions + recordings) and Box (folders,
files, versions) in memory, then runs the real pipeline twice to prove:

  1. Each call lands in its "<Month> <Year>" Box subfolder (e.g. "June 2026").
  2. Combined CSV per month lives inside that month folder, sorted by time.
  3. MP3s are named <stamp>_<asset/VAN name>_<asset number>_<extension>.mp3.
  4. A second overlapping run adds only NEW rows/files (no duplicates).

Run:  python test_mock.py
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.update({
    "RC_CLIENT_ID": "test", "RC_CLIENT_SECRET": "test", "RC_JWT": "test",
    "BOX_CLIENT_ID": "test", "BOX_CLIENT_SECRET": "test",
    "BOX_ENTERPRISE_ID": "test", "BOX_FOLDER_ID": "ROOT",
    "CSV_ROTATION": "monthly", "RECORDINGS_SUBFOLDER": "false",
    "MEDIA_THROTTLE_SECONDS": "0",
})

import archiver  # noqa: E402

# Never touch the real state.json -- the live archiver depends on it.
archiver.STATE_FILE = Path(__file__).resolve().parent / "state.test.json"

# ---------------------------------------------------------------------------
# Fake data: 5 calls spanning May 31 -> June 12, three with recordings.
# All calls belong to extension 301 = "VAN 109" x194.
# ---------------------------------------------------------------------------

EXT_MAP = {"301": ("VAN 109", "194"),
           "num:15550100234": ("VAN 109", "194")}


def make_record(i, start, with_recording):
    rec = {"id": f"REC{i}", "type": "RCC", "contentUri": "x"} if with_recording else None
    return {
        "id": f"CALL{i}",
        "startTime": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "type": "Voice",
        "direction": "Outbound" if i % 2 else "Inbound",
        "from": {"phoneNumber": f"+1503555000{i}", "name": f"Caller {i}"},
        "to": {"phoneNumber": "+15550100234", "name": "Support"},
        # CALL4 has no extension id on the record (it happens on real inbound
        # calls) -- the asset must resolve via the "num:" phone-number fallback
        "extension": None if i == 4 else {"id": "301"},
        "action": "VoIP Call",
        "result": "Call connected",
        "duration": 60 + i * 13,
        "recording": rec,
    }


BASE = datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc)
ALL_RECORDS = [
    make_record(1, BASE, True),                          # May 31  (Outbound)
    make_record(2, BASE + timedelta(days=2), False),     # Jun 2
    make_record(3, BASE + timedelta(days=5), True),      # Jun 5   (Outbound)
    make_record(4, BASE + timedelta(days=10), True),     # Jun 10  (Inbound)
    make_record(5, BASE + timedelta(days=12), False),    # Jun 12
]

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeRC:
    def login(self):
        print("  [RC] login OK (mock)")

    def fetch_extension_map(self):
        return dict(EXT_MAP)

    def fetch_call_log(self, date_from, date_to):
        return [r for r in ALL_RECORDS
                if date_from <= datetime.fromisoformat(
                    r["startTime"].replace("Z", "+00:00")) <= date_to]

    def download_recording(self, rec_id, dest: Path):
        dest.write_bytes(b"ID3 fake mp3 bytes for " + rec_id.encode())
        return True


class FakeBox:
    """In-memory Box: folders -> {name: (id, type)}, files -> bytes, versions."""
    storage = {"ROOT": {}}
    blobs = {}
    versions = {}
    _next = [100]

    def whoami(self):
        print("  [Box] auth OK (mock service account)")
        return "0"

    def list_folder(self, folder_id, force=False):
        return dict(self.storage[folder_id])

    def ensure_subfolder(self, parent_id, name):
        for n, (fid, t) in self.storage[parent_id].items():
            if n == name and t == "folder":
                return fid
        fid = f"FOLDER{self._next[0]}"; self._next[0] += 1
        self.storage[parent_id][name] = (fid, "folder")
        self.storage[fid] = {}
        return fid

    def upload_new_file(self, folder_id, name, content):
        if name in self.storage[folder_id]:
            raise RuntimeError(f"409 conflict: {name} already exists")
        fid = f"FILE{self._next[0]}"; self._next[0] += 1
        self.storage[folder_id][name] = (fid, "file")
        self.blobs[fid] = content
        self.versions[fid] = 1
        return fid

    def upload_new_version(self, file_id, name, content):
        self.blobs[file_id] = content
        self.versions[file_id] += 1
        return file_id

    def download_file(self, file_id):
        return self.blobs[file_id]


archiver.RingCentral = FakeRC
archiver.Box = FakeBox


def folder(name):
    fid = FakeBox.storage["ROOT"][name][0]
    return FakeBox.storage[fid]


def check(label, cond):
    print(("  PASS  " if cond else "  FAIL  ") + label)
    if not cond:
        sys.exit(1)


# ---------------------------------------------------------------------------
# RUN 1: May 30 -> Jun 6  (calls 1, 2, 3)
# ---------------------------------------------------------------------------

print("\n--- RUN 1: May 30 -> Jun 6 ---")
archiver.STATE_FILE.unlink(missing_ok=True)
rc1 = archiver.run(datetime(2026, 5, 30, tzinfo=timezone.utc),
                   datetime(2026, 6, 6, tzinfo=timezone.utc), dry_run=False)
root = FakeBox.storage["ROOT"]
print("  Box root after run 1:", sorted(root))

check("exit code 0", rc1 == 0)
check("root holds ONLY month folders (no loose files)",
      sorted(root) == ["June 2026", "May 2026"]
      and all(t == "folder" for _, t in root.values()))
check("May 2026 = May CSV + REC1 mp3",
      sorted(folder("May 2026")) == [
          "20260531-090000_VAN 109_15035550001_194.mp3",
          "Call Log - 2026-05.csv"])
check("June 2026 = June CSV + REC3 mp3",
      sorted(folder("June 2026")) == [
          "20260605-090000_VAN 109_15035550003_194.mp3",
          "Call Log - 2026-06.csv"])

# ---------------------------------------------------------------------------
# RUN 2: Jun 1 -> Jun 12 (overlaps run 1; calls 2, 3, 4, 5)
# ---------------------------------------------------------------------------

print("\n--- RUN 2: Jun 1 -> Jun 12 (overlaps run 1) ---")
rc2 = archiver.run(datetime(2026, 6, 1, tzinfo=timezone.utc),
                   datetime(2026, 6, 12, 23, tzinfo=timezone.utc), dry_run=False)
june = folder("June 2026")
print("  June 2026 after run 2:", sorted(june))

june_id = june["Call Log - 2026-06.csv"][0]
june_csv = FakeBox.blobs[june_id].decode("utf-8-sig").strip().splitlines()
print("\n  Combined June CSV contents:")
for line in june_csv:
    print("   |", line[:120])

check("exit code 0", rc2 == 0)
check("June CSV has header + 4 calls, no duplicates", len(june_csv) == 5)
check("June CSV got a new Box VERSION (not a duplicate file)",
      FakeBox.versions[june_id] == 2)
check("REC4 (Inbound) named with VAN + receiving number + extension",
      "20260610-090000_VAN 109_15550100234_194.mp3" in june)
check("3 MP3s total, REC1/REC3 not re-uploaded",
      sum(1 for f in (folder("May 2026"), folder("June 2026"))
          for n in f if n.endswith(".mp3")) == 3)

rows = [l.split(",") for l in june_csv[1:]]
times = [r[1] for r in rows]
check("CSV rows sorted chronologically", times == sorted(times))
check("CSV Extension column filled from extension map",
      all(r[9] == "194" for r in rows))

mp3s = sorted(n for f in (folder("May 2026"), folder("June 2026"))
              for n in f if n.endswith(".mp3"))
print("\n  MP3 names:")
for m in mp3s:
    print("   |", m)
check("MP3 names = stamp_asset_number_extension",
      all(len(m[:-4].split("_")) == 4
          and m[:8].isdigit()
          and m[:-4].split("_")[1] == "VAN 109"
          and m[:-4].split("_")[3] == "194"
          for m in mp3s))

state = json.loads(archiver.STATE_FILE.read_text())
check("state.json tracks 3 uploaded recordings",
      len(state["uploaded_recordings"]) == 3)

print("\nALL TESTS PASSED")
