# RingCentral Archiver → Box

Custom replacement for RingCentral's built-in Archiver. Instead of one folder
per line, per day, per media type, this script archives everything into
**one Box folder**:

```
Old (built-in archiver):                 New (this script):
RingCentral Archiver/                    Call Archive/
├─ Line One Dallas_101/                  ├─ May 2026/
â”‚  â”œâ”€ 2026-6-11/                         â”‚  â”œâ”€ Call Log - 2026-05.csv  ← every call, sorted by time
│  │  └─ Call Recording/                 │  └─ 20260531-090012_VAN 109_15550100890_109.mp3
│  │     └─ *.mp3                        └─ June 2026/
│  ├─ 2026-6-10/ ...                        ├─ Call Log - 2026-06.csv
├─ Line Two Denver_102/ ...             ├─ 20260601-101414_VAN 109_15550100890_109.mp3
└─ (×16 lines × 30 days...)                 └─ 20260602-130924_VAN 214_15550100891_214.mp3
```

- **One subfolder per month** (`June 2026`), holding that month's CSV and
  every recording from that month.
- **One combined CSV** per month with every call (number, name, extension,
  direction, result, duration, recording filename). New runs append to the
  same file via Box versioning — never a second copy.
- **MP3s named** `YYYYMMDD-HHMMSS_<asset/VAN name>_<asset number>_<extension>.mp3`
  so they sort chronologically and show which VAN took the call. The asset is
  the receiving line on inbound calls, the originating line on outbound.
- `migrate_existing.py` — one-time script that reorganizes files uploaded
  under the old flat layout into the new structure (move + rename in Box,
  no re-downloading).
- **Never duplicates** — tracks what's uploaded in `state.json` and checks Box
  before uploading.
- Handles RingCentral rate limits (throttle + 429 retry with backoff).

---

## Setup (one time, ~15 min)

### 1. Install
```
cd C:\path\to\ringCentralArchiver
pip install -r requirements.txt
copy .env.example .env
```

### 2. RingCentral app (JWT)
1. Go to https://developers.ringcentral.com → Console → **Create App**.
2. App type: **REST API App**. Auth: **JWT auth flow** (server, no UI).
3. Application scopes: **Read Call Log** and **Read Call Recording**.
4. Copy the **Client ID** and **Client Secret** into `.env`.
5. In the dev portal, click your profile → **Credentials** → **Create JWT** →
   authorize it for this app → paste the JWT string into `.env`.
6. Note: a brand-new app starts in **Sandbox**. To hit production data it
   needs to be graduated to production (the portal walks you through it —
   for read-only call log apps this is quick).

### 3. Box app (Client Credentials Grant)
1. Go to https://app.box.com/developers/console → **Create New App** →
   **Custom App** → **Server Authentication (Client Credentials Grant)**.
2. Configuration tab: App Access Level = **App + Enterprise Access**.
3. Copy **Client ID**, **Client Secret**, and **Enterprise ID** into `.env`.
4. Click **Review and Submit** → then approve it in
   **Admin Console → Apps → Custom Apps Manager**. (You re-approve any time
   you change scopes.)
5. Create (or pick) the single destination folder in Box. Copy the folder ID
   from the URL (`https://app.box.com/folder/123456789` → `123456789`) into `.env`.
6. **Critical step:** in the Dev Console → General Settings, copy the app's
   **Service Account email** (`AutomationUser_…@boxdevedition.com`) and invite
   it as an **Editor** collaborator on that folder. Without this the app
   cannot see the folder and every upload will 404.

### 4. Test it
```
python archiver.py --dry-run --from 2026-06-11 --to 2026-06-11
```
Dry run pulls real data from RingCentral but writes the CSV + MP3s to a local
`output\` folder instead of Box — inspect it, make sure it looks right, then:
```
python archiver.py --from 2026-06-11 --to 2026-06-11
```
and check the Box folder.

### 5. Schedule it
Windows Task Scheduler → Create Basic Task → Daily (e.g. 11:00 PM) →
Start a program → `C:\path\to\ringCentralArchiver\run_archiver.bat`
(Set "Start in" to the folder path.)

With no arguments, each run automatically archives everything since the
previous run (with a 1-hour overlap for safety — dedupe makes overlap harmless).

---

## Configuration (.env)

| Setting | Default | What it does |
|---|---|---|
| `CSV_ROTATION` | `monthly` | `monthly` = one CSV per month. `never` = one CSV forever. |
| `RECORDINGS_SUBFOLDER` | `false` | `true` puts MP3s in a single `Recordings` subfolder. |
| `DOWNLOAD_RECORDINGS` | `true` | `false` = CSV only. |
| `MEDIA_THROTTLE_SECONDS` | `1.2` | Pause between recording downloads. |

## Backfilling history
```
python archiver.py --from 2026-05-01 --to 2026-05-31
```
Note: RingCentral retains detailed call logs/recordings for a limited window
(typically 90 days for recordings on most plans), so backfill what you need soon.

## Files
- `archiver.py` — the whole pipeline
- `test_mock.py` — offline test suite (`python test_mock.py`), no credentials needed
- `state.json` — created on first run; tracks last run + uploaded recordings.
  Delete it to force a full re-scan (Box-side checks still prevent duplicates).
- `archiver.log` — run history

## Troubleshooting
- **`OAU-213` / invalid grant (RingCentral):** JWT not authorized for this
  app, or app still in sandbox while pointing at production URL.
- **Box 404 on folder:** service account not added as collaborator (step 3.6).
- **Box 403:** app not approved in Admin Console, or approval pending
  re-authorization after a scope change.
- **Lots of 429s:** raise `MEDIA_THROTTLE_SECONDS` to 2–3.
