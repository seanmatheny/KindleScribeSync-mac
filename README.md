# Kindle Scribe Sync

![Kindle Scribe Sync Icon](https://github.com/Koloss5421/KindleScribeSync/blob/main/KindleScribeSyncIcon.png?raw=true)

### Built with
 - Python 3.14+
 - Requests
 - Selenium
 - img2pdf
 - schedule

## Overview
Syncs Kindle Scribe notebooks to local PDF files and optionally into one or more of three sync targets:

1. **Bear Notes** — creates/replaces a Bear note per notebook with the exported PDF attached.
2. **Obsidian** — copies the PDF into your vault's attachments folder and creates/updates a markdown file that embeds it.
3. **Local folder** — copies exported PDFs into a directory of your choosing, mirroring the Kindle folder hierarchy.

All three targets are disabled by default and can be enabled independently (you may use any combination). Preferences are set in `config.json` (or overridden with CLI flags — see [Configuration](#configuration) and [Running](#running) below).

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
| `obsidian_sync` | bool | false | Sync updated notebooks into an Obsidian vault |
| `obsidian_vault_path` | string | — | Absolute path to your Obsidian vault directory |
| `obsidian_force_resync` | bool | false | Recreate Obsidian markdown files even if already synced |
| `pdf_folder_sync` | bool | false | Copy updated PDFs into a local folder |
| `pdf_folder_path` | string | — | Absolute path to the destination folder for exported PDFs |
| `pdf_folder_force_resync` | bool | false | Re-copy all PDFs to the export folder even if already synced |

CLI flags always override config file values. `config.json` is gitignored; use `config.json.example` as the template to commit.

> **Choosing sync targets**: all three destinations (Bear, Obsidian, local folder) are independent — enable whichever ones you want by setting their `*_sync` key to `true`. You may enable multiple at the same time.

> **Migrating from settings.json**: if you had a `settings.json` from a previous version, copy its values into `config.json` using the key names above.

## Running

Run one sync pass (useful for testing)
```
python KindleScribeSync.py --once
```

If the launchd agent is already running, the same command queues an immediate one-shot sync request for the running background instance and exits.

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

Sync updated notebooks into an Obsidian vault
```
python KindleScribeSync.py --once --obsidian-sync --obsidian-vault /path/to/vault
```

Force Obsidian markdown recreation even when notes are already current
```
python KindleScribeSync.py --once --obsidian-sync --obsidian-force-resync
```

Clear local Obsidian sync markers before a run
```
python KindleScribeSync.py --once --obsidian-sync --reset-obsidian-state
```

Copy updated PDFs into a local folder
```
python KindleScribeSync.py --once --pdf-folder-sync --pdf-folder-path /path/to/folder
```

Force re-copy of all PDFs to the export folder
```
python KindleScribeSync.py --once --pdf-folder-sync --pdf-folder-force-resync
```

Clear local PDF folder sync markers before a run
```
python KindleScribeSync.py --once --pdf-folder-sync --reset-pdf-folder-state
```

## launchd (Run at Login)

Note: The script will detect the currently sourced virtual environment, and use that for the launchd agent. So best to try out a `--once` run first 

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

> **How launchd finds config.json**: the daemon sets its working directory to the folder containing `KindleScribeSync.py` and reads `config.json` from that same folder. To configure which sync targets are active (Bear, Obsidian, local folder) and any path options, edit `config.json` in the repository directory before installing (or reinstall after editing).

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

## Obsidian Sync Behavior

- Obsidian sync is disabled by default; enable it with `"obsidian_sync": true` in `config.json` or `--obsidian-sync`.
- You must also set `obsidian_vault_path` in `config.json` (or pass `--obsidian-vault`) to point at your vault directory.
- On each sync the PDF is copied into `<vault>/attachments/KindleScribe/` and a markdown file is created or overwritten under `<vault>/KindleScribe/`, mirroring the notebook folder hierarchy.
- The markdown file embeds the PDF with an Obsidian `![[...]]` link and records the notebook path and last sync timestamp.
- If an Obsidian note was deleted manually, run with `--obsidian-force-resync` to recreate it.
- To wipe only the local sync markers (not trigger a PDF re-render), use `--reset-obsidian-state`.

## Local Folder Sync Behavior

- Local folder sync is disabled by default; enable it with `"pdf_folder_sync": true` in `config.json` or `--pdf-folder-sync`.
- You must also set `pdf_folder_path` in `config.json` (or pass `--pdf-folder-path`) to point at the destination directory.
- On each sync the exported PDF is copied into `<folder>/<notebook-path>.pdf`, mirroring the Kindle folder hierarchy. The destination file is always overwritten with the latest version.
- To force re-copy of all PDFs even when the script believes they are current, run with `--pdf-folder-force-resync`.
- To wipe only the local sync markers (not trigger a PDF re-render), use `--reset-pdf-folder-state`.

