# Kindle Scribe Sync

## Note
This is working, but there is a delay in some cases between when the Kindle Apps update and when these are available via API, specifically with documents that have modified pages, rather than brand new or deleted ones.

![Kindle Scribe Sync Icon](https://github.com/Koloss5421/KindleScribeSync/blob/main/KindleScribeSyncIcon.png?raw=true)

### Built with
 - Python 3.14+
 - Requests
 - Selenium
 - img2pdf
 - schedule
 - Swift + Apple Vision (handwriting OCR, optional)

## Overview
Syncs Kindle Scribe notebooks to local PDF files and optionally into one or more of four sync targets:

1. **Bear Notes** — creates/replaces a Bear note per notebook with the exported PDF attached. Note that the Bear API doesn't allow updating existing PDFs in notes.
2. **Obsidian** — copies the PDF into your vault's attachments folder and creates/updates a markdown file that embeds it.
3. **Local folder** — copies exported PDFs into a directory of your choosing, mirroring the Kindle folder hierarchy.
4. **Craft Notes** — stores a single PDF per notebook in a ScribeNotes folder and creates a Craft document linking to it.

All four targets are disabled by default and can be enabled independently (you may use any combination). Preferences are set in `config.json` (or overridden with CLI flags — see [Configuration](#configuration) and [Running](#running) below).

On top of the PDFs, two optional text features (macOS only):

5. **OCR notes** — recognises the handwriting in every notebook and keeps a searchable note per notebook in a notes app, checked against the PDF on every sync. Bear is supported today; Apple Notes and Craft are planned behind the same `notes_app` setting. See [OCR Notes Sync Behavior](#ocr-notes-sync-behavior).
6. **TODO tasks** — every handwritten `TODO:` line becomes a task. See [TODO Tasks Behavior](#todo-tasks-behavior).

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
| `craft_sync` | bool | false | Sync updated PDFs into a ScribeNotes folder and create Craft documents |
| `craft_notes_path` | string | ~/Documents/ScribeNotes | Path to the ScribeNotes folder for PDF storage |
| `craft_force_resync` | bool | false | Recreate Craft documents even if already synced |
| `craft_space_id` | string | — | Craft space ID for document creation (optional) |
| `craft_folder_id` | string | — | Craft folder ID for document creation (optional) |
| `notes_sync` | bool | false | OCR every notebook into a note and keep the notes matching the PDFs |
| `notes_app` | string | bear | Notes app to write to. Supported: `bear` (needs Bear 2.10+) |
| `notes_root_tag` | string | scribe | Tag the notes are filed under; Kindle folders become subtags (`#scribe/work`) |
| `notes_attach_pdf` | bool | true | Attach the handwritten PDF to each note |
| `notes_exclude` | list | [] | Notebook paths to leave out, as glob patterns, e.g. `["Personal/*", "Work/scratch"]` |
| `notes_force_resync` | bool | false | Rewrite every note even when it already matches its PDF (prefer the CLI flag; left on, it rewrites every note on every sync) |
| `ocr_languages` | list | ["en-US"] | Languages Vision should expect, most likely first |
| `todo_sync` | bool | false | Collect handwritten `TODO:` lines as tasks. Needs `notes_sync` |
| `todo_app` | string | bear | Where tasks go. Supported: `bear` (one central tasks note) |
| `todo_note_title` | string | Scribe Tasks | Title of the Bear note that collects the tasks |

CLI flags always override config file values. `config.json` is gitignored; use `config.json.example` as the template to commit.

> **Choosing sync targets**: all four destinations (Bear, Obsidian, local folder, Craft) are independent — enable whichever ones you want by setting their `*_sync` key to `true`. You may enable multiple at the same time.

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

Sync updated notebooks into Craft Notes
```
python KindleScribeSync.py --once --craft-sync
```

Specify Craft space and folder for document placement
```
python KindleScribeSync.py --once --craft-sync --craft-space-id YOUR_SPACE_ID --craft-folder-id YOUR_FOLDER_ID
```

Override the ScribeNotes folder location
```
python KindleScribeSync.py --once --craft-sync --craft-notes-path /path/to/custom/folder
```

Force Craft document recreation
```
python KindleScribeSync.py --once --craft-sync --craft-force-resync
```

Clear local Craft sync markers before a run
```
python KindleScribeSync.py --once --craft-sync --reset-craft-state
```

OCR the notebooks into notes and collect their TODO lines
```
python KindleScribeSync.py --once --notes-sync --todo-sync
```

Do only that, against the PDFs already on disk, without contacting Amazon. This is safe to run while the launchd agent is running, and is the quickest way to try the feature or to seed the notes the first time
```
python KindleScribeSync.py --notes-only --todo-sync
```

Rewrite every note even when it already matches its PDF
```
python KindleScribeSync.py --notes-only --notes-force-resync
```

Run the tests
```
python -m unittest discover tests
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

The plist sets `KeepAlive` with `SuccessfulExit = false`, so launchd relaunches the daemon if it ever exits with an error or is killed (throttled to once per 60 seconds). A failed sync check (for example no network right after the Mac wakes from sleep) is logged with a traceback and retried at the next interval instead of terminating the process. If you installed the agent before this behaviour existed, run `--launchd-remove` then `--launchd-install` once to pick up the new plist.

> **How launchd finds config.json**: the daemon sets its working directory to the folder containing `KindleScribeSync.py` and reads `config.json` from that same folder. To configure which sync targets are active (Bear, Obsidian, local folder, Craft) and any path options, edit `config.json` in the repository directory before installing (or reinstall after editing).

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

## Craft Notes Sync Behavior

- Craft sync is disabled by default; enable it with `"craft_sync": true` in `config.json` or `--craft-sync`.
- PDFs are stored in a ScribeNotes folder (configurable via `craft_notes_path`, default `~/Documents/ScribeNotes`), mirroring the Kindle folder hierarchy.
- Each notebook maintains a **single PDF** at a stable path (`ScribeNotes/<notebook-path>.pdf`). When the remote notebook changes, the PDF is overwritten in place — there is never more than one copy.
- On first sync, a Craft document is created via the `craftdocs://` URL scheme containing a link to the local PDF file.
- On subsequent syncs the PDF file is replaced; the Craft document's link remains valid because the file path does not change.
- To recreate Craft documents (e.g. if you deleted them in Craft), run with `--craft-force-resync`.
- To wipe only the local sync markers, use `--reset-craft-state`.
- You may optionally set `craft_space_id` and `craft_folder_id` to control where documents are created in Craft.
- When a notebook is deleted remotely, the corresponding PDF in ScribeNotes is removed automatically. The Craft document must be deleted manually (Craft's URL scheme does not support programmatic deletion).

### Craft Permissions

Craft must be granted permission to open URL scheme requests. On first use, macOS will prompt you to allow the `craftdocs://` URL to open Craft. Accept this prompt. No additional permissions or API keys are required — the integration uses Craft's built-in URL scheme support.

If running via launchd (background), ensure Craft is installed and has been launched at least once so macOS recognizes the URL scheme handler.

## OCR Notes Sync Behavior

- OCR notes are disabled by default; enable them with `"notes_sync": true` in `config.json` or `--notes-sync`.
- Requirements: macOS, [Bear](https://bear.app) 2.10 or later (it ships the `bearcli` command line tool this uses), and the Xcode Command Line Tools (`xcode-select --install`). There are no extra Python packages.
- Handwriting is recognised on-device by Apple's Vision framework, through a small Swift program (`ocr/scribe-ocr.swift`). It is compiled into `bin/scribe-ocr` automatically on first use, or by hand with `ocr/build.sh`. It runs as a short-lived process, so the recognition models (about 500 MB) are never held in the sync daemon's memory.
- Each notebook gets one Bear note: the notebook name as title (or `Folder / Name` when two notebooks share a name), a tag mirroring its Kindle folder (`#scribe/work`), the recognised text under a heading per page, and the handwritten PDF attached at the end.
- **The note is a mirror and the PDF is the source of truth.** On every sync, including the ones where nothing changed on the Kindle, each note is checked against its PDF and rewritten only if they no longer match:
  - the notebook changed → the note is rewritten in place and its PDF attachment replaced;
  - the note was edited in Bear → your edit is overwritten, so keep your own thoughts in a separate note and link to this one;
  - the note was trashed → it is restored (to stop syncing a notebook, add it to `notes_exclude`);
  - the note was archived → it stays in the archive and keeps being updated there;
  - the notebook was renamed or moved → the same note is retitled and retagged;
  - the notebook was deleted from the Kindle → its note is moved to Bear's trash.
- Bear does not need to be running and there are no permission prompts: `bearcli` works on Bear's database directly.
- Sync state lives in `notes_sync_state.json`, and recognised text is cached in `notes_sync_cache/` so only changed notebooks are re-read. Both are safe to delete: every note carries a `Sync ID` line, so existing notes are found again rather than duplicated.
- This is independent of the older `bear_sync` target, which attaches PDFs through Bear's x-callback-url and cannot update a note in place. Since OCR notes carry the PDF too, you will most likely want one or the other; with both enabled you get two Bear notes per notebook.

### How good is the OCR?

It depends almost entirely on the handwriting. Deliberate, separated lettering is read accurately. Fast joined-up cursive is not: expect roughly half of the words to come out right — enough to find a page by searching for a name or a keyword, not enough to read in place of the original, which is why the PDF is attached to every note. The page template (rules, grids, dots) and highlighter are removed before recognition, which recovers lines that are otherwise missed entirely.

## TODO Tasks Behavior

- TODO tasks are disabled by default; enable them with `"todo_sync": true` in `config.json` or `--todo-sync`. They are found in the OCR text, so `notes_sync` must be enabled too.
- Write `TODO: wash the car` in a notebook and it becomes a task. Matching is deliberately a little looser than the literal `TODO:`, because handwriting recognition misreads colons and the letter O. Exactly what counts:

  | Where | Case | Must be followed by | Examples |
  |---|---|---|---|
  | Start of a line (a bullet in front is fine) | upper case only | one of `:` `;` `.` `,` `-` | `TODO: x`, `- TODO: x`, `TODO - x`, `TODO; x`, `T0DO: x` |
  | Start of a line | any case; `to do` and `to-do` too | `:` or `;` | `todo: x`, `Todo: x`, `To do: x` |
  | Further along a line | upper case only | `:` or `;` | `Budget meeting. TODO: send slides` |

  - A zero is accepted for either O (`T0DO`, `TOD0`).
  - Vision often reads a handwritten `TODO:` as `TODD:` while rating both readings equally. When its first reading of a line has no TODO marker but one of its alternate readings does, the alternate is used, in the note as well as for the task. A line that really says `Todd:` is unaffected unless Vision itself proposes `TODO:` for it.
  - Not tasks: `TODO wash the car` (nothing after the word), `todo - x` (lower case needs a colon), `TODO list`, `TODOs`, "things to do: relax".
  - `TODO:` or a bare `TODO` alone on a line is a heading: the bullets directly under it become tasks, or else the single line under it.
  - A TODO line that runs to the right-hand edge of the page continues onto the next line.
- Tasks are collected as checkboxes in one Bear note, `Scribe Tasks` (`todo_note_title`), each linking back to the note and page it came from:
  `- [ ] Wash the car — [todo, p. 1](bear://…) · 2026-09-17`
- That note is only ever appended to. Tick tasks off, reword them, move them under your own headings or delete them; nothing there is rewritten, and a task that has been delivered once is never added again.
- Upper-case `TODO:` on a line of its own, in your clearest handwriting, gives the most reliable results.

