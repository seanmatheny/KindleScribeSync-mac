#!/usr/bin/env python3
#
# Kindle Scribe Sync
# Copyright (c) 2025 Koloss5421
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import argparse
import base64
import io
import json
import logging
import os
import pickle
import plistlib
import re
import subprocess
import sys
import signal
import tarfile
import time
from datetime import datetime
from pathlib import Path
from shutil import rmtree
from urllib.parse import quote, urlencode

import img2pdf
import requests
import schedule

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

## CONSTANTS
RENDER_HEIGHT = 2500
RENDER_WIDTH = 1200
NOTEBOOK_JSON_PATH = "notebooks.json"
BEAR_PENDING_DELETES_PATH = "bear_pending_deletes.json"
BEAR_DELETE_RETRY_RUNS = 24
COOKIES_FILE = "cookies.pkl"
UPDATE_MINUTES = 30
BEAR_SYNC_VERSION = 3
CONFIG_FILE = "config.json"
LOCK_FILE = "kindlescribesync.lock"
MANUAL_SYNC_REQUEST_FILE = "kindlescribesync.manual-sync.request"
LAUNCH_AGENT_LABEL = "com.github.kindlescribesync"
LOG_DIR = Path.home() / "Library" / "Logs" / "KindleScribeSync"
APP_LOG_FILE = LOG_DIR / "KindleScribeSync.log"
LAUNCHD_STDOUT_LOG = LOG_DIR / "launchd.out.log"
LAUNCHD_STDERR_LOG = LOG_DIR / "launchd.err.log"

## Where to extract tar images
EXTRACT_PATH = "extraction"
## Where do we want to save the kindle notebooks?
SYNC_PATH = "kindle_notebooks"
## User agent must be an android user agent to allow you to access the kindle-notebook site and see the api/notes page.
USER_AGENT = "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-G973U) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/14.2 Chrome/87.0.4280.141 Mobile Safari/537.36"

AMZ_RENDER_HEADER = "x-amzn-karamel-notebook-rendering-token"
URL_AUTH = "https://read.amazon.com/kindle-notebook?ref_=neo_mm_yn_na_kfa"
URL_GET_NOTEBOOKS = "https://read.amazon.com/kindle-notebook/api/notes"
URL_OPEN_NOTEBOOK = "https://read.amazon.com/openNotebook?notebookId=[NOTEBOOK_ID]&marketplaceId=ATVPDKIKX0DER"
URL_RENDER_NOTEBOOK = "https://read.amazon.com/renderPage?startPage=0&endPage=[NOTEBOOK_LENGTH]&width={}&height={}&dpi=160".format(RENDER_WIDTH, RENDER_HEIGHT)

## Setup Logging
LOG_DIR.mkdir(parents=True, exist_ok=True)
log_handlers = [logging.FileHandler(APP_LOG_FILE, encoding="utf-8")]
if sys.stdout.isatty():
    log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s",
    handlers=log_handlers,
)
logger = logging.getLogger()

## GLOBALS
notebooks = {}
cookies = None
session = None
driver = None
running = True
update_count = 0
last_update = "No Updates"
args = None
bear_sync_enabled = False
bear_dry_run = False
bear_force_resync = False
notebook_name_counts = {}
bear_pending_deletes = {}
config = {
    "update_minutes": UPDATE_MINUTES,
    "bear_sync": False,
    "bear_dry_run": False,
    "bear_force_resync": False,
}


def get_lock_path():
    return Path(__file__).resolve().parent / LOCK_FILE


def get_manual_sync_request_path():
    return Path(__file__).resolve().parent / MANUAL_SYNC_REQUEST_FILE


def acquire_single_instance_lock():
    """
    Write a PID lock file so only one instance runs at a time.
    Returns True if the lock was acquired, False if another instance is already running.
    """
    lock_path = get_lock_path()
    try:
        if lock_path.exists():
            try:
                old_pid = int(lock_path.read_text().strip())
                os.kill(old_pid, 0)  # raises OSError if process is gone
                logger.warning("Another instance is already running (PID %s). Exiting.", old_pid)
                return False
            except (OSError, ValueError):
                pass  # stale lock
        lock_path.write_text(str(os.getpid()))
        return True
    except Exception as ex:
        logger.warning("Could not acquire instance lock: %s", ex)
        return True  # allow running if the lock mechanism itself fails


def release_single_instance_lock():
    try:
        lock_path = get_lock_path()
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass


def request_manual_sync_from_running_instance():
    """
    Signal an already-running instance to perform an immediate sync pass.
    """
    request_path = get_manual_sync_request_path()
    payload = {
        "requested_at": datetime.now().isoformat(timespec="seconds"),
        "requested_by_pid": os.getpid(),
    }
    try:
        request_path.write_text(json.dumps(payload), encoding="utf-8")
        logger.info("Queued manual sync request for running instance: %s", request_path)
        return True
    except Exception as ex:
        logger.error("Failed to queue manual sync request: %s", ex)
        return False


def process_pending_manual_sync_request():
    """
    Handle a one-shot manual sync request written by a CLI --once invocation.
    """
    request_path = get_manual_sync_request_path()
    if not request_path.exists():
        return

    payload_text = ""
    try:
        payload_text = request_path.read_text(encoding="utf-8").strip()
    except Exception:
        pass

    logger.info("Processing queued manual sync request: %s", payload_text or "(no payload)")

    try:
        request_path.unlink(missing_ok=True)
    except Exception as ex:
        logger.warning("Could not clear manual sync request file: %s", ex)

    check_notebooks()


def get_config_path():
    return Path(__file__).resolve().parent / CONFIG_FILE


def load_config():
    """
    Load config.json into the global config dict.
    CLI flags always take precedence; this only fills in values not supplied on the command line.
    """
    global config
    config_path = get_config_path()
    if config_path.exists():
        try:
            config.update(json.loads(config_path.read_text(encoding="utf-8")))
            logger.info("Loaded config from %s", config_path)
        except Exception as ex:
            logger.warning("Failed to load config file %s: %s", config_path, ex)
    else:
        logger.info("No config.json found at %s; using defaults.", config_path)


def get_launch_agent_path():
    return Path.home() / "Library" / "LaunchAgents" / "{}.plist".format(LAUNCH_AGENT_LABEL)


def notify(message, title="Kindle Scribe Sync"):
    logger.info("%s: %s", title, message)


def build_launch_agent_plist():
    script_path = str(Path(__file__).resolve())
    python_path = sys.executable
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [python_path, script_path],
        "WorkingDirectory": str(Path(__file__).resolve().parent),
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LAUNCHD_STDOUT_LOG),
        "StandardErrorPath": str(LAUNCHD_STDERR_LOG),
    }


def run_launchctl(arguments):
    result = subprocess.run(["launchctl", *arguments], capture_output=True, text=True)
    if result.stdout.strip():
        logger.info("launchctl stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.info("launchctl stderr: %s", result.stderr.strip())
    return result


def bootout_launch_agent(plist_path):
    domain = "gui/{}".format(os.getuid())
    result = run_launchctl(["bootout", domain, str(plist_path)])
    if result.returncode == 0:
        return True

    stderr = (result.stderr or "").lower()
    if "boot-out failed: 5" in stderr or "could not find specified service" in stderr:
        logger.info("Launch agent was not loaded; continuing.")
        return True

    return False


def install_launch_agent(icon=None, item=None):
    plist_path = get_launch_agent_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(plist_path, "wb") as handle:
        plistlib.dump(build_launch_agent_plist(), handle)

    if not bootout_launch_agent(plist_path):
        notify("Launch agent install failed while unloading previous job. See logs in ~/Library/Logs/KindleScribeSync.")
        return

    domain = "gui/{}".format(os.getuid())
    result = run_launchctl(["bootstrap", domain, str(plist_path)])
    if result.returncode == 0:
        notify("Launch agent installed. App will start automatically at login.")
    else:
        notify("Launch agent install failed. See logs in ~/Library/Logs/KindleScribeSync.")


def remove_launch_agent(icon=None, item=None):
    plist_path = get_launch_agent_path()
    if plist_path.exists() and not bootout_launch_agent(plist_path):
        notify("Launch agent removal failed. See logs in ~/Library/Logs/KindleScribeSync.")
        return

    if plist_path.exists():
        plist_path.unlink()
    notify("Launch agent removed.")


def launch_agent_status(icon=None, item=None):
    plist_path = get_launch_agent_path()
    if not plist_path.exists():
        notify("Launch agent is not installed.")
        return

    domain_target = "gui/{}/{}".format(os.getuid(), LAUNCH_AGENT_LABEL)
    result = run_launchctl(["print", domain_target])
    if result.returncode == 0:
        notify("Launch agent is installed and loaded.")
    else:
        notify("Launch agent plist exists but is not currently loaded.")


def reset_bear_sync_state(items):
    """
    Clear persisted Bear sync markers so notes can be recreated from scratch.
    """
    if isinstance(items, dict):
        iterable = items.values()
    else:
        iterable = items

    for entry in iterable:
        if isinstance(entry, dict):
            entry["bearCreated"] = False
            entry["bearTitle"] = None
            entry["bearSyncVersion"] = 0
            if "items" in entry:
                reset_bear_sync_state(entry["items"])


def handle_reset_bear_state(icon=None, item=None):
    reset_bear_sync_state(notebooks)
    save_notebook_json()
    notify("Reset local Bear sync state. The next Bear sync will recreate notes.")


def configure_schedule():
    schedule.clear("sync")
    schedule.every(UPDATE_MINUTES).minutes.do(check_notebooks).tag("sync")
    logger.info("Scheduled sync every %s minutes", UPDATE_MINUTES)


def slugify_tag_part(value):
    """
    Convert notebook title/path parts into Bear tag-safe slugs.
    """
    cleaned = sanitize_name(value).lower()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"[^a-z0-9_\-/]", "", cleaned)
    cleaned = cleaned.strip("-/")
    return cleaned or "untitled"


def build_bear_tags(notebook_path):
    """
    Build tags like #scribe and #scribe/work/projects from notebook path.
    """
    parts = [slugify_tag_part(part) for part in notebook_path.split(os.sep) if part]
    if not parts:
        return ["scribe"]
    return ["scribe", "scribe/{}".format("/".join(parts))]


def collect_notebook_name_counts(items, counts=None):
    """
    Count notebook names in the current Kindle tree so unique names can use plain Bear titles.
    """
    if counts is None:
        counts = {}

    for item in items:
        if item.get("type") == "folder":
            collect_notebook_name_counts(item.get("items", []), counts)
        elif item.get("type") == "notebook":
            title = item.get("title", "Untitled Notebook")
            counts[title] = counts.get(title, 0) + 1

    return counts


def ensure_notebook_tracking_defaults(notebook_id, notebook_meta):
    """
    Backfill state fields for notebook entries created before Bear sync support existed.
    """
    if "bearCreated" not in notebook_meta:
        notebook_meta["bearCreated"] = False
    if "bearTitle" not in notebook_meta:
        notebook_meta["bearTitle"] = None
    if "bearSyncVersion" not in notebook_meta:
        notebook_meta["bearSyncVersion"] = 0


def build_bear_note_title(notebook_name, notebook_path):
    """
    Use the plain notebook name when unique, else fall back to a path-based title.
    """
    if notebook_name_counts.get(notebook_name, 0) <= 1:
        return notebook_name

    parts = [part for part in notebook_path.split(os.sep) if part]
    return " / ".join(parts) if parts else "Untitled Notebook"


def build_bear_marker(notebook_id):
    """
    Hidden unique marker used to find and replace existing Bear notes safely.
    """
    return "kindle-scribe-sync-id:{}".format(notebook_id)


def encode_file_for_bear(file_path):
    """
    Base64 encode a file for Bear x-callback file attachment parameters.
    """
    file_bytes = Path(file_path).read_bytes()
    encoded = base64.b64encode(file_bytes).decode("ascii")
    logger.info(
        "Prepared Bear attachment: path=%s size_bytes=%s encoded_chars=%s",
        file_path,
        len(file_bytes),
        len(encoded),
    )
    return encoded


def bear_open_xurl(action, params):
    """
    Execute a Bear x-callback-url action via macOS `open`.
    """
    if sys.platform != "darwin":
        logger.warning("Bear sync requested but this platform is not macOS; skipping")
        return False

    query = urlencode(params, quote_via=quote, safe=",")
    url = "bear://x-callback-url/{}?{}".format(action, query)
    logger.info("Bear sync action: %s", action)
    logger.info(
        "Bear sync params: title=%s search=%s identifier=%s tags=%s",
        params.get("title"),
        params.get("search"),
        params.get("id") or params.get("identifier"),
        params.get("tags"),
    )
    logger.info("Bear URL length: %s", len(url))
    if bear_dry_run:
        logger.info("Bear dry-run URL: %s", url)
        return True

    try:
        logger.info("Opening Bear x-callback URL via osascript")
        applescript = 'open location "{}"\n'.format(url.replace('"', '\\"'))
        subprocess.run(["osascript"], input=applescript, text=True, check=True)
        logger.info("Bear x-callback URL accepted by osascript")
        return True
    except Exception as ex:
        logger.error("Bear x-callback open failed: %s", ex)
        return False


def bear_delete_note_by_title(title, reason):
    """
    Delete a Bear note by exact title using AppleScript.
    Returns True if the delete command succeeded, False otherwise.
    """
    if not title:
        logger.warning("Cannot delete Bear note: no title provided")
        return False

    if bear_dry_run:
        logger.info("Bear dry-run: would delete note with title '%s'", title)
        return True

    try:
        # Escape single quotes in the title for AppleScript
        escaped_title = title.replace("'", "'\"'\"'")
        applescript = (
            "tell application \"Bear\"\n"
            "  try\n"
            "    move (first note where title is equal to '{}') to trash\n"
            "    return true\n"
            "  on error\n"
            "    return false\n"
            "  end try\n"
            "end tell\n"
        ).format(escaped_title)
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            timeout=5,
        )
        success = result.returncode == 0 and "true" in result.stdout.lower()
        if success:
            logger.info("Successfully deleted Bear note '%s' via AppleScript for reason='%s'", title, reason)
        else:
            logger.warning("AppleScript delete for '%s' failed or returned no match (reason='%s')", title, reason)
        return success
    except Exception as ex:
        logger.error("AppleScript delete failed for '%s': %s", title, ex)
        return False


def bear_delete_note_by_marker(marker, reason):
    """
    Delete a Bear note by marker search using AppleScript.
    Returns True if the delete command succeeded, False otherwise.
    """
    if not marker:
        logger.warning("Cannot delete Bear note: no marker provided")
        return False

    if bear_dry_run:
        logger.info("Bear dry-run: would search for and delete note with marker '%s'", marker)
        return True

    try:
        # Search for notes containing the marker, then delete them
        applescript = (
            "tell application \"Bear\"\n"
            "  try\n"
            "    set notesToDelete to notes where body contains \"{}\"\n"
            "    repeat with noteToDelete in notesToDelete\n"
            "      move noteToDelete to trash\n"
            "    end repeat\n"
            "    return true\n"
            "  on error\n"
            "    return false\n"
            "  end try\n"
            "end tell\n"
        ).format(marker)
        result = subprocess.run(
            ["osascript", "-e", applescript],
            capture_output=True,
            text=True,
            timeout=5,
        )
        success = result.returncode == 0 and "true" in result.stdout.lower()
        if success:
            logger.info("Successfully deleted Bear notes with marker '%s' via AppleScript for reason='%s'", marker, reason)
        else:
            logger.warning("AppleScript delete by marker '%s' failed or returned no match (reason='%s')", marker, reason)
        return success
    except Exception as ex:
        logger.error("AppleScript delete by marker failed for '%s': %s", marker, ex)
        return False


def bear_trash_note(*, reason, title=None, search_term=None):
    """
    Trash a Bear note using AppleScript for direct, reliable deletion.
    """
    if title:
        logger.info("Attempting Bear delete by title for reason='%s' title='%s'", reason, title)
        return bear_delete_note_by_title(title, reason)
    if search_term:
        # Use marker search if it looks like our sync marker
        if "kindle-scribe-sync-id:" in search_term:
            logger.info("Attempting Bear delete by marker for reason='%s' marker='%s'", reason, search_term)
            return bear_delete_note_by_marker(search_term, reason)
        # Otherwise try title search
        logger.info("Attempting Bear delete by content search for reason='%s' search='%s'", reason, search_term)
        return bear_delete_note_by_marker(search_term, reason)
    logger.warning("Skipping Bear delete for reason='%s'; no title or search term", reason)
    return False


def bear_trash_all_matches(*, reason, title=None, search_term=None, max_attempts=1):
    """
    Run multiple trash passes to remove duplicate Bear notes that match the same key.
    """
    max_attempts = max(int(max_attempts), 1)
    success = True
    for attempt in range(1, max_attempts + 1):
        attempt_reason = "{}_attempt{}".format(reason, attempt)
        ok = bear_trash_note(reason=attempt_reason, title=title, search_term=search_term)
        success = success and ok
        # Give Bear a moment to update search results between repeated trash requests.
        if attempt < max_attempts and not bear_dry_run:
            time.sleep(0.2)
    return success


def trash_bear_for_notebook(notebook_id, notebook_meta, reason, aggressive=False):
    """
    Remove the Bear note(s) associated with a notebook ID.
    """
    if not bear_sync_enabled:
        return False

    marker = build_bear_marker(notebook_id)
    title_attempts = 5 if aggressive else 2
    marker_attempts = 10 if aggressive else 3
    name_attempts = 3 if aggressive else 1
    success = True

    previous_bear_title = notebook_meta.get("bearTitle")
    if previous_bear_title:
        # Most reliable path: delete by exact stored Bear title.
        success = success and bear_trash_all_matches(
            title=previous_bear_title,
            reason="{}_title".format(reason),
            max_attempts=title_attempts,
        )

    # Fallback for any older notes that still need marker-based matching.
    success = success and bear_trash_all_matches(
        search_term=marker,
        reason="{}_marker".format(reason),
        max_attempts=marker_attempts,
    )

    notebook_name = notebook_meta.get("name")
    if notebook_name and notebook_name != previous_bear_title:
        # Last-resort cleanup for legacy state where stored bearTitle may be missing/stale.
        success = success and bear_trash_all_matches(
            title=notebook_name,
            reason="{}_name_fallback".format(reason),
            max_attempts=name_attempts,
        )

    notebook_path = notebook_meta.get("path")
    if notebook_path:
        # Legacy notes can still be found by their path line in note body.
        success = success and bear_trash_all_matches(
            search_term="Notebook Path: {}".format(notebook_path),
            reason="{}_path_fallback".format(reason),
            max_attempts=name_attempts,
        )

    return success


def sync_pdf_to_bear(notebook_id, notebook_path, notebook_name, pdf_path, notebook_meta):
    """
    Recreate the notebook note in Bear with the latest attached PDF.
    """
    if not bear_sync_enabled:
        logger.debug("Bear sync disabled; skipping notebook '%s'", notebook_name)
        return

    ensure_notebook_tracking_defaults(notebook_id, notebook_meta)
    tags = build_bear_tags(notebook_path)
    marker = build_bear_marker(notebook_id)
    previous_bear_title = notebook_meta.get("bearTitle")
    bear_title = build_bear_note_title(notebook_name, notebook_path)
    notebook_meta["bearTitle"] = bear_title
    note_text = (
        "Notebook Path: {}\n"
        "Last Synced: {}\n\n"
        "Attached: latest Kindle Scribe PDF export.\n\n"
        "Sync Marker: {}\n"
    ).format(notebook_path, datetime.now().isoformat(timespec="seconds"), marker)
    filename = Path(pdf_path).name
    encoded_file = encode_file_for_bear(pdf_path)

    create_params = {
        "title": bear_title,
        "text": note_text,
        "file": encoded_file,
        "filename": filename,
        "tags": ",".join(tags),
        "open_note": "no",
        "show_window": "no",
    }

    logger.info(
        "Preparing Bear sync for notebook '%s' (id=%s, pdf_exists=%s, bear_created=%s)",
        notebook_name,
        notebook_id,
        os.path.exists(pdf_path),
        notebook_meta.get("bearCreated", False),
    )

    if previous_bear_title and previous_bear_title != bear_title:
        bear_trash_note(title=previous_bear_title, reason="migrate_title")

    # Always remove existing copies first so one notebook maps to one Bear note.
    trash_bear_for_notebook(notebook_id, notebook_meta, "refresh_existing_note", aggressive=False)

    created = bear_open_xurl("create", create_params)
    if created:
        notebook_meta["bearCreated"] = True
        notebook_meta["bearSyncVersion"] = BEAR_SYNC_VERSION
        logger.info("Bear note recreated with attachment for '%s'", notebook_name)
    else:
        logger.warning("Bear note create/recreate failed for '%s'", notebook_name)


def sanitize_name(name):
    """
    Sanitize notebook/folder names for cross-platform file systems.
    """
    cleaned = re.sub(r"[\\/:*?\"<>|]", "_", name).strip()
    return cleaned or "untitled"


def build_driver():
    """
    Build a Firefox webdriver instance with a mobile user-agent.
    """
    options = Options()
    options.set_preference("general.useragent.override", USER_AGENT)
    options.set_preference("network.proxy.type", 0)
    return webdriver.Firefox(options=options)


def ensure_driver():
    """
    Create the webdriver lazily so startup works even with valid saved cookies.
    """
    global driver
    if driver is None:
        driver = build_driver()

def load_notebook_json():
    """
    Loads the json file into the `notebooks` global
    """
    global notebooks
    logger.info("Attempting to load notebook data file")
    if os.path.exists(NOTEBOOK_JSON_PATH):
        logger.info("Loading notebook data file")
        with open(NOTEBOOK_JSON_PATH, "r", encoding="utf-8") as f:
            notebooks = json.load(f)


def load_bear_pending_deletes():
    """
    Load queued Bear notebook deletes that still need retry.
    """
    global bear_pending_deletes
    logger.info("Attempting to load pending Bear delete file")
    if os.path.exists(BEAR_PENDING_DELETES_PATH):
        logger.info("Loading pending Bear delete file")
        with open(BEAR_PENDING_DELETES_PATH, "r", encoding="utf-8") as f:
            loaded = json.load(f)
            bear_pending_deletes = loaded if isinstance(loaded, dict) else {}
    else:
        bear_pending_deletes = {}

    recover_pending_bear_deletes_from_log()

def save_notebook_json():
    """
    Saves the `notebooks` global to a file
    """
    global notebooks
    logger.info("Saving notebook data file")
    with open(NOTEBOOK_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(notebooks, f)


def save_bear_pending_deletes():
    """
    Save queued Bear notebook deletes so retries survive process restarts.
    """
    logger.info("Saving pending Bear delete file")
    with open(BEAR_PENDING_DELETES_PATH, "w", encoding="utf-8") as f:
        json.dump(bear_pending_deletes, f)


def queue_bear_delete(notebook_id, notebook_meta):
    """
    Persist enough metadata to retry Bear note cleanup later.
    """
    global bear_pending_deletes
    existing = bear_pending_deletes.get(notebook_id, {})
    bear_pending_deletes[notebook_id] = {
        "name": notebook_meta.get("name") or existing.get("name"),
        "path": notebook_meta.get("path") or existing.get("path"),
        "bearTitle": notebook_meta.get("bearTitle") or existing.get("bearTitle"),
        "queuedAt": existing.get("queuedAt") or datetime.now().isoformat(timespec="seconds"),
        "lastAttemptAt": existing.get("lastAttemptAt"),
        "attempts": int(existing.get("attempts", 0)),
        "remainingRuns": max(int(existing.get("remainingRuns", 0)), BEAR_DELETE_RETRY_RUNS),
    }


def collect_active_notebook_ids(items):
    """
    Collect active notebook IDs currently present in the local notebook tree.
    """
    ids = set()
    for notebook_id, meta in items.items():
        if meta.get("type") == "notebook":
            ids.add(notebook_id)
        elif meta.get("type") == "folder":
            ids.update(collect_active_notebook_ids(meta.get("items", {})))
    return ids


def recover_pending_bear_deletes_from_log(max_lines=5000):
    """
    Recover missed pending deletes from prior prune log entries.
    """
    global bear_pending_deletes
    if not APP_LOG_FILE.exists():
        return

    try:
        lines = APP_LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-max_lines:]
    except Exception as ex:
        logger.warning("Could not read log file for pending delete recovery: %s", ex)
        return

    active_ids = collect_active_notebook_ids(notebooks)
    last_seen_id = None
    recovered = 0
    for line in lines:
        id_match = re.search(
            r"reason='(?:prune_orphan|retry_pending_delete)[^']*' search='kindle-scribe-sync-id:([0-9a-fA-F\-]+)'",
            line,
        )
        if id_match:
            last_seen_id = id_match.group(1)
            continue

        path_match = re.search(r"Pruning 'kindle_notebooks/(.+?)\\.pdf' Notebook", line)
        if path_match and last_seen_id:
            notebook_path = path_match.group(1)
            if last_seen_id not in active_ids and last_seen_id not in bear_pending_deletes:
                notebook_name = os.path.basename(notebook_path)
                queue_bear_delete(
                    last_seen_id,
                    {
                        "name": notebook_name,
                        "path": notebook_path,
                        "bearTitle": notebook_name,
                    },
                )
                recovered += 1
            last_seen_id = None

    if recovered > 0:
        logger.info("Recovered %s pending Bear deletions from prior logs", recovered)


def process_pending_bear_deletes():
    """
    Retry Bear cleanup for notebooks deleted in earlier runs.
    """
    global bear_pending_deletes
    if not bear_sync_enabled or not bear_pending_deletes:
        return

    for notebook_id in list(bear_pending_deletes.keys()):
        pending_meta = bear_pending_deletes[notebook_id]
        pending_meta["attempts"] = int(pending_meta.get("attempts", 0)) + 1
        pending_meta["lastAttemptAt"] = datetime.now().isoformat(timespec="seconds")
        pending_meta["remainingRuns"] = int(pending_meta.get("remainingRuns", BEAR_DELETE_RETRY_RUNS)) - 1
        trash_bear_for_notebook(notebook_id, pending_meta, "retry_pending_delete", aggressive=True)
        if pending_meta["remainingRuns"] <= 0:
            logger.info("Expiring pending Bear cleanup retries for notebook id=%s", notebook_id)
            del bear_pending_deletes[notebook_id]

def close_app():
    """
    shutdown the application
    """
    global driver
    global running
    running = False
    logger.info("Closing application")
    save_cookies()
    save_bear_pending_deletes()
    release_single_instance_lock()
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass


def run_sync_loop():
    logger.info("Running initial check")
    check_notebooks()

    if args is not None and args.once:
        logger.info("Single-run mode complete")
        return

    configure_schedule()
    while running:
        process_pending_manual_sync_request()
        schedule.run_pending()
        time.sleep(1)


def convert_to_pdf(images, savepath):
    """
    Uses `img2pdf` to convert an array of image paths (`images`) to a pdf (`savepath`).
    """
    logger.info("Converting extracted images to pdf output: {}".format(savepath))
    os.makedirs(os.path.dirname(savepath), exist_ok=True)
    with open(savepath, "wb") as f:
        pdfdata = img2pdf.convert(images)
        f.write(pdfdata)

def extract_tarfile(tar_file_data):
    """
    Uses `tarfile` to extract the images from the amazon tar file. Returns an array of image paths.
    """
    logger.info("Extracting notebook tar file data")
    tar_stream = io.BytesIO(tar_file_data)
    tar_file = tarfile.open(fileobj=tar_stream)
    images = []
    for member in tar_file:
        if member.name.endswith(".png"):
            tar_file.extract(member, path=EXTRACT_PATH)
            extr_path = os.path.join(EXTRACT_PATH, member.name)
            images.append(extr_path)

    return images

def render_notebook(renderingToken, notebook_len):
    """
    Uses requests to call the render notebook url with the renderingToken. 
    The Response content should contain a tar file of the notebook.
    """
    global cookies
    global session
    logger.info("Rendering notebook")
    request_url = URL_RENDER_NOTEBOOK.replace("[NOTEBOOK_LENGTH]", str(notebook_len))
    session.headers[AMZ_RENDER_HEADER] = renderingToken
    while True:
        resp = session.get(request_url)
        if resp.is_redirect:
            rm_cookies()
            authenticate()
        else:
            break
    session.headers.pop(AMZ_RENDER_HEADER)
    cookies = requests.utils.dict_from_cookiejar(session.cookies)
    return resp.content

def get_notebook(id):
    """
    Uses requests to call the open notebook url to get the notebooks metadata and renderingToken.
    """
    global cookies
    global session
    logger.info("Getting notebook '{}' data".format(id))
    request_url = URL_OPEN_NOTEBOOK.replace("[NOTEBOOK_ID]", id)
    while True:
        resp = session.get(request_url)
        if resp.is_redirect:
            rm_cookies()
            authenticate()
        elif resp.status_code != 200:
            pass
        else:
            break
    cookies = requests.utils.dict_from_cookiejar(session.cookies)
    return resp.json()

def iterate_notebooks(obj, parentObj):
    """
    Recursive function that iterates over an items object, checking if the item is in a parent object.
    If the item is a folder, it creates the path and recurses.
    If the item is a notebook, it checks the updateTime against the modificationTime, if it is greater
    it updates the local copy of the notebook.
    """
    if 'items' in parentObj:
        parentItems = parentObj['items']
    else:
        parentItems = parentObj

    for x in obj:
        id = x['id']
        safe_title = sanitize_name(x['title'])
        if 'path' in parentObj:
            new_path = os.path.join(parentObj['path'], safe_title)
        else:
            new_path = safe_title

        if not id in parentItems:
            parentItems[id] = {
                'type': x['type'],
                'name': x['title'],
                'path': new_path,
                'updateTime': 0,
                'items': {},
                'bearCreated': False,
                'bearTitle': None,
            }
        else:
            ensure_notebook_tracking_defaults(id, parentItems[id])
            old_path = parentItems[id].get('path')
            if old_path != new_path:
                if x['type'] == 'folder':
                    old_folder_path = os.path.join(SYNC_PATH, old_path)
                    new_folder_path = os.path.join(SYNC_PATH, new_path)
                    if os.path.exists(old_folder_path) and not os.path.exists(new_folder_path):
                        os.makedirs(os.path.dirname(new_folder_path), exist_ok=True)
                        os.rename(old_folder_path, new_folder_path)
                elif x['type'] == 'notebook':
                    old_pdf_path = os.path.join(SYNC_PATH, "{}.pdf".format(old_path))
                    new_pdf_path = os.path.join(SYNC_PATH, "{}.pdf".format(new_path))
                    if os.path.exists(old_pdf_path) and not os.path.exists(new_pdf_path):
                        os.makedirs(os.path.dirname(new_pdf_path), exist_ok=True)
                        os.rename(old_pdf_path, new_pdf_path)
                parentItems[id]['path'] = new_path

            parentItems[id]['name'] = x['title']
        
        if x['type'] == "folder":
            folder_path = os.path.join(SYNC_PATH, parentItems[id]['path'])
            if not os.path.exists(folder_path):
                os.makedirs(folder_path)
            iterate_notebooks(x['items'], parentItems[id])

        
        if x['type'] == "notebook":
            nb_data = get_notebook(id)
            time.sleep(1)
            modification_time = nb_data['metadata']['modificationTime']
            current_update_time = parentItems[id]['updateTime']
            pdf_path = os.path.join(SYNC_PATH, "{}.pdf".format(parentItems[id]['path']))
            desired_bear_title = build_bear_note_title(parentItems[id]['name'], parentItems[id]['path'])
            pdf_missing = not os.path.exists(pdf_path)
            should_render_pdf = modification_time > current_update_time or pdf_missing
            should_seed_bear = (
                bear_sync_enabled
                and os.path.exists(pdf_path)
                and (
                    bear_force_resync
                    or not parentItems[id].get('bearCreated', False)
                    or parentItems[id].get('bearTitle') != desired_bear_title
                    or parentItems[id].get('bearSyncVersion', 0) < BEAR_SYNC_VERSION
                )
            )

            logger.info(
                "Notebook '%s': remote_modification=%s local_update=%s pdf_missing=%s should_render_pdf=%s should_seed_bear=%s bear_force_resync=%s bear_title=%s bear_version=%s",
                parentItems[id]['name'],
                modification_time,
                current_update_time,
                pdf_missing,
                should_render_pdf,
                should_seed_bear,
                bear_force_resync,
                parentItems[id].get('bearTitle'),
                parentItems[id].get('bearSyncVersion', 0),
            )

            if should_render_pdf:
                total_pages = nb_data['metadata']['totalPages']

                if (total_pages > 0):
                    total_pages = total_pages - 1

                tardata = render_notebook(nb_data['renderingToken'], total_pages)
                images = extract_tarfile(tardata)

                convert_to_pdf(images, pdf_path)
                sync_pdf_to_bear(id, parentItems[id]['path'], parentItems[id]['name'], pdf_path, parentItems[id])

                for x in images:
                    os.remove(x)
                
                global update_count
                update_count += 1
                parentItems[id]['updateTime'] = int(time.time())
            elif should_seed_bear and os.path.exists(pdf_path):
                logger.info("Skipping PDF render for '%s'; reusing existing PDF for Bear sync", parentItems[id]['name'])
                sync_pdf_to_bear(id, parentItems[id]['path'], parentItems[id]['name'], pdf_path, parentItems[id])
            elif bear_sync_enabled:
                logger.info("Skipping Bear sync for '%s'; no PDF changes and Bear note already tracked", parentItems[id]['name'])

def id_exists_in_object(id, sync_items = []):
    """
    Checks if an id exists in an array object. Returns the index of the object.
    """
    for x in sync_items:
        if x["id"] == id:
            return sync_items.index(x)
    return -1


def prune_orphans(items, sync_items):
    """
    Recursive function that iterates over a set of items, if the item is not in the sync_items array it removes it based on the item type.
    If the item exists in both and is of type folder, it recurses on the next set of items.
    """
    global update_count
    for k in list(items.keys()):
        dict_object = items[k]
        index = id_exists_in_object(k, sync_items)
        if  index == -1:
            if dict_object["type"] == "folder":
                # Remove Bear notes for notebooks that disappeared remotely.
                prune_orphans(dict_object["items"], [])
                folder_path = os.path.join(SYNC_PATH, dict_object['path'])
                try:
                    logger.info("Pruning '{}' Folder".format(folder_path))
                    rmtree(folder_path)
                    update_count += 1
                except Exception:
                    logger.error("Pruning '{}' Folder Failed!".format(folder_path))
            if dict_object["type"] == "notebook":
                queue_bear_delete(k, dict_object)
                trash_bear_for_notebook(k, dict_object, "prune_orphan", aggressive=True)
                pdf_path = os.path.join(SYNC_PATH, "{}.pdf".format(dict_object['path']))
                try:
                    logger.info("Pruning '{}' Notebook".format(pdf_path))
                    os.remove(pdf_path)
                    update_count += 1
                except Exception:
                    logger.error("Pruning '{}' Notebook Failed!".format(pdf_path))
            del items[k]
        else:
            if dict_object["type"] == "folder":   
                prune_orphans(dict_object["items"], sync_items[index]["items"])

def get_all_notebooks():
    """
    Uses requests to query the notes api. The object returned contains the entire structure
    of notebooks from the scribe.
    """
    logger.info("Getting all notebooks")
    global cookies
    global notebook_name_counts
    global session
    while True:
        resp = session.get(URL_GET_NOTEBOOKS)
        if resp.is_redirect:
            rm_cookies()
            authenticate()
        else:
            break
    cookies = requests.utils.dict_from_cookiejar(session.cookies)
    data = resp.json()
    notebook_name_counts = collect_notebook_name_counts(data['itemsList'])
    iterate_notebooks(data['itemsList'], notebooks)
    prune_orphans(notebooks, data['itemsList'])
    process_pending_bear_deletes()
    save_bear_pending_deletes()
    save_notebook_json()

def load_cookies():
    """
    Uses `pickle` to load cookies from disk as the `cookies` global and update the requests session.
    """
    global cookies
    global session
    logger.info("Attempting to load cookies")
    if os.path.exists(COOKIES_FILE):
        logger.info("Loading Cookies from file")
        with open(COOKIES_FILE, "rb") as f:
            cookies = pickle.load(f)

        if cookies is None:
            return False

        # Support old cookie formats and normalize to a simple dict.
        if isinstance(cookies, list):
            cookies = {cookie.get("name"): cookie.get("value") for cookie in cookies if "name" in cookie and "value" in cookie}
        if not isinstance(cookies, dict) or len(cookies) < 2:
            return False
        
        session.cookies.update(cookies)
        return True
    
    return False

def save_cookies():
    """
    Uses `pickle` to save the global `cookies` object to disk for later use.
    """
    global cookies
    logger.info("Saving cookies")
    with open(COOKIES_FILE, "wb") as f:
        pickle.dump(cookies, f)

def rm_cookies():
    """
    Clears all cookie instances including the on disk file.
    """
    global session
    global cookies
    global driver
    logger.info("Deleting all cookies")
    if driver is not None:
        try:
            driver.delete_all_cookies()
        except Exception:
            pass
    session.cookies.clear()
    cookies = None
    if (os.path.exists(COOKIES_FILE)):
        os.remove(COOKIES_FILE)

def authenticate():
    """
    Uses selenium driver to allow you to authenticate to kindle, saves the cookies 
    for later use and updates the requests session.
    """
    global driver
    global cookies
    global session
    ensure_driver()
    logger.info("Authenticating to {}".format(URL_AUTH))
    driver.get(URL_AUTH)

    try:
        logger.info("Waiting for authentication")
        element = WebDriverWait(driver, 120).until(
            EC.presence_of_element_located(
                (By.ID, "web-library-root")
            )
        )
    finally:
        pass

    driver_cookies = driver.get_cookies()
    cookies = {cookie["name"]: cookie["value"] for cookie in driver_cookies if "name" in cookie and "value" in cookie}
    save_cookies()

    session.cookies.update(cookies)

def check_notebooks():
    """
    Uses as a scheduled job with schedule and the kickoff for all notebook syncs 
    including forced updates with the system tray icon.
    """
    global session
    global driver
    global update_count
    global last_update
    update_count = 0
    logger.info("Checking for notebook changes")

    if session == None:
        session = requests.session()
        session.headers.update({'User-Agent': USER_AGENT})

    if cookies == None:
        if not load_cookies():
            authenticate()
        else:
            if driver is not None:
                try:
                    driver.quit()
                except Exception:
                    pass
                driver = None

    get_all_notebooks()

    now = datetime.now()
    last_update = now.strftime("%m/%d/%Y, %H:%M:%S")

    logger.info("Will Check again in {} minutes...".format(UPDATE_MINUTES))


def parse_args():
    parser = argparse.ArgumentParser(description="Sync Kindle Scribe notebooks to local PDF files.")
    parser.add_argument("--once", action="store_true", help="Run a single sync check then exit.")
    parser.add_argument("--bear-sync", action="store_true", help="Sync updated notebook exports to Bear notes using x-callback-url (macOS only).")
    parser.add_argument("--bear-dry-run", action="store_true", help="Print Bear x-callback URLs without opening them.")
    parser.add_argument("--bear-force-resync", action="store_true", help="Recreate Bear notes even when local sync state says they are already current.")
    parser.add_argument("--reset-bear-state", action="store_true", help="Clear local Bear sync markers before syncing.")
    parser.add_argument("--launchd-install", action="store_true", help="Install the app as a macOS launch agent.")
    parser.add_argument("--launchd-remove", action="store_true", help="Remove the macOS launch agent.")
    parser.add_argument("--launchd-status", action="store_true", help="Print whether the macOS launch agent is installed.")
    parser.add_argument("--update-minutes", type=int, default=None, help="Minutes between sync checks. Overrides config.json.")
    return parser.parse_args()


def handle_signal(signum, frame):
    global running
    logger.info("Received signal %s, shutting down.", signum)
    running = False


def main():
    global UPDATE_MINUTES
    global args
    global bear_sync_enabled
    global bear_dry_run
    global bear_force_resync

    args = parse_args()
    load_config()

    # CLI flags override config file values.
    configured_minutes = args.update_minutes if args.update_minutes is not None else config.get("update_minutes", UPDATE_MINUTES)
    UPDATE_MINUTES = max(configured_minutes, 1)
    bear_sync_enabled = args.bear_sync or config.get("bear_sync", False)
    bear_dry_run = args.bear_dry_run or config.get("bear_dry_run", False)
    bear_force_resync = args.bear_force_resync or config.get("bear_force_resync", False)

    if args.launchd_install:
        install_launch_agent()
        return
    if args.launchd_remove:
        remove_launch_agent()
        return
    if args.launchd_status:
        launch_agent_status()
        return

    if not acquire_single_instance_lock():
        if args.once and request_manual_sync_from_running_instance():
            logger.info("Another instance is active; queued one manual sync request and exiting.")
            return
        sys.exit(1)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    if not os.path.exists(EXTRACT_PATH):
        os.mkdir(EXTRACT_PATH)

    # Ensure output root exists to avoid path errors on first run.
    os.makedirs(SYNC_PATH, exist_ok=True)

    load_notebook_json()
    load_bear_pending_deletes()

    if args.reset_bear_state:
        handle_reset_bear_state()

    run_sync_loop()
    close_app()


if __name__ == "__main__":
    main()