# Kindle Scribe Sync

![Kindle Scribe Sync Icon](https://github.com/Koloss5421/KindleScribeSync/blob/main/KindleScribeSyncIcon.png?raw=true)

### Built with
 - Python 3.14+
 - Requests
 - Selenium
 - img2pdf
 - pystray
 - schedule
 - tarfile

## Getting Started
Syncs Kindle Scribe notebooks using an android user agent to access the notebook files.
Keeps a local record of the last update time. Runs a check every 5 minutes.
Stores the files in a destination folder in PDF format.

Using `pystray` to have a "Force Sync" and "Last Update" button in the system tray.

![Kindle Scribe Sync Screenshot](https://github.com/Koloss5421/KindleScribeSync/blob/main/docs/screenshot.png?raw=true)

This now supports both macOS and Windows.
You must authenticate through the selenium browser, 2 min timeout when it opens.
The cookies are then saved for use later by requests.

There may be some bugs. Not sure how much I will maintain it.
No Longer have a Kindle Scribe to test with.

## Installation

Clone the repository
```
git clone https://github.com/Koloss5421/KindleScribeSync
```

Setup virtual environment
```
python3 -m venv ./venv
```

Activate virtual environment (macOS / Linux)
```
source ./venv/bin/activate
```

Activate virtual environment (Windows)
```
./venv/Scripts/activate
```

Install requirements
```
pip install -r requirements.txt
```

Run it!
```
python KindleScribeSync.py
```

Run one sync pass without tray UI (useful for terminal-only environments)
```
python KindleScribeSync.py --once --no-tray
```

Sync updated notebooks into Bear Notes (macOS)
```
python KindleScribeSync.py --bear-sync
```

Run Bear sync once without tray (recommended for manual plug-in syncs)
```
python KindleScribeSync.py --once --no-tray --bear-sync
```

Force Bear note recreation even when local sync state says notes are already current
```
python KindleScribeSync.py --once --no-tray --bear-sync --bear-force-resync
```

Clear local Bear sync markers before a run
```
python KindleScribeSync.py --once --no-tray --bear-sync --reset-bear-state
```

Install the app as a macOS launch agent so the menubar app starts automatically at login
```
python KindleScribeSync.py --launchd-install
```

Check whether the launch agent is installed
```
python KindleScribeSync.py --launchd-status
```

Remove the launch agent
```
python KindleScribeSync.py --launchd-remove
```

Dry-run Bear calls to preview x-callback URLs
```
python KindleScribeSync.py --once --no-tray --bear-sync --bear-dry-run
```

## Bear Notes Sync Behavior

- Bear sync is optional and enabled only with `--bear-sync`.
- A root tag `#scribe` is applied to all synced notes.
- A subtag is generated from notebook path, for example `#scribe/work`.
- Bear note titles use the plain notebook name when it is unique.
- When notebook names collide in different folders, the title falls back to a path-based name such as `Work / Daily Work Notes`.
- On first sync for a notebook, the note is created in Bear with the exported PDF attached.
- On subsequent syncs, the previous Bear note is replaced with a fresh note containing the latest attached PDF.
- If a local PDF export is missing, the script regenerates it even when the remote notebook has not changed.
- If Bear notes were deleted manually, use `--bear-force-resync` to recreate them from current local exports.
- If you want to clear only the local Bear sync markers first, use `--reset-bear-state`.

## Menubar And Launchd

- On macOS, the tray icon acts as a menubar app.
- The menubar menu shows the last sync time and current sync interval.
- You can trigger a manual sync from the menubar.
- You can change the sync interval from the menubar; the choice is saved in `settings.json`.
- You can reset local Bear sync state from the menubar.
- You can install, remove, and check the launch agent from the menubar or CLI.
- The launch agent starts the menubar app automatically at login; the app itself manages the sync frequency.

Each Bear note includes:
- Notebook path
- Source path
- Last sync timestamp
- Attached PDF export

