# Kindle Scribe Sync

![Kindle Scribe Sync Icon](https://github.com/Koloss5421/KindleScribeSync/blob/main/KindleScribeSyncIcon.png?raw=true)

### Built with
 - Python 3.14+
 - Requests
 - Selenium
 - img2pdf
 - schedule

## Overview
Syncs Kindle Scribe notebooks to local PDF files and optionally into Bear Notes.
Runs as a headless background daemon — no GUI or dock icon.
Intended to be installed as a macOS launchd agent that starts automatically at login.

You must authenticate through the Selenium browser window the first time.
Cookies are saved and reused for subsequent runs.

## Installation

Clone the repository
```
git clone https://github.com/Koloss5421/KindleScribeSync
```

Setup virtual environment
```
python3 -m venv ./venv
```

Activate virtual environment
```
source ./venv/bin/activate
```

Install requirements
```
pip install -r requirements.txt
```

## Configuration

Copy the example config and edit it before first run:
```
cp config.json.example config.json
```

`config.json` options:

| Key | Type | Default | Description |
|---|---|---|---|
| `update_minutes` | int | 30 | How often to check for notebook changes |
| `bear_sync` | bool | false | Sync updated PDFs into Bear Notes |
| `bear_dry_run` | bool | false | Log Bear x-callback URLs without opening them |
| `bear_force_resync` | bool | false | Recreate all Bear notes even if already synced |

CLI flags always override config file values. `config.json` is gitignored; use `config.json.example` as the template to commit.

> **Migrating from settings.json**: if you had a `settings.json` from a previous version, copy its values into `config.json` using the key names above.

## Running

Run one sync pass (useful for testing)
```
python KindleScribeSync.py --once
```

Run in continuous mode (syncs on the configured interval)
```
python KindleScribeSync.py
```

Override the sync interval at the command line
```
python KindleScribeSync.py --update-minutes 15
```

Sync updated notebooks into Bear Notes
```
python KindleScribeSync.py --once --bear-sync
```

Force Bear note recreation even when local sync state says notes are already current
```
python KindleScribeSync.py --once --bear-sync --bear-force-resync
```

Clear local Bear sync markers before a run (Bear notes will be recreated)
```
python KindleScribeSync.py --once --bear-sync --reset-bear-state
```

Dry-run Bear calls to preview x-callback URLs without opening Bear
```
python KindleScribeSync.py --once --bear-sync --bear-dry-run
```

## launchd (Run at Login)

Install as a macOS launch agent (starts at login, restarts on crash)
```
python KindleScribeSync.py --launchd-install
```

Check whether the launch agent is installed and loaded
```
python KindleScribeSync.py --launchd-status
```

Remove the launch agent
```
python KindleScribeSync.py --launchd-remove
```

The plist is written to `~/Library/LaunchAgents/com.github.kindlescribesync.plist`.
Application logs are written to `~/Library/Logs/KindleScribeSync/KindleScribeSync.log`.
launchd stdout/stderr are written to `~/Library/Logs/KindleScribeSync/launchd.out.log` and `~/Library/Logs/KindleScribeSync/launchd.err.log`.
After changing `config.json`, run `--launchd-remove` then `--launchd-install` to reload.

## Bear Notes Sync Behavior

- Bear sync is disabled by default; enable it with `"bear_sync": true` in `config.json` or `--bear-sync`.
- A root tag `#scribe` is applied to all synced notes.
- A subtag is generated from the notebook path, for example `#scribe/work`.
- Bear note titles use the plain notebook name when it is unique across all notebooks.
- When notebook names collide in different folders, the title falls back to a path-based name such as `Work / Daily Work Notes`.
- On first sync, the note is created in Bear with the exported PDF attached.
- On subsequent syncs, the previous Bear note is replaced with a fresh note containing the latest PDF.
- If a local PDF export is missing, the script regenerates it even when the remote notebook has not changed.
- If Bear notes were deleted manually, run with `--bear-force-resync` to recreate them.
- To wipe only the local sync markers (not trigger a PDF re-render), use `--reset-bear-state`.

Each Bear note includes:
- Notebook path
- Last sync timestamp
- Attached PDF export

