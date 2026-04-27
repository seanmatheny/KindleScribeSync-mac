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
import re
import subprocess
import sys
import tarfile
import time
from datetime import datetime
from pathlib import Path
from shutil import rmtree
from urllib.parse import quote, urlencode

import img2pdf
import requests
import schedule

try:
    import pystray
    from PIL import Image
except Exception:
    pystray = None
    Image = None

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

## CONSTANTS
RENDER_HEIGHT = 2500
RENDER_WIDTH = 1200
NOTEBOOK_JSON_PATH = "notebooks.json"
COOKIES_FILE = "cookies.pkl"
UPDATE_MINUTES = 30
BEAR_SYNC_VERSION = 2

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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-12.12s] [%(levelname)-5.5s]  %(message)s",
    handlers=[
        logging.FileHandler("debug.log"),
        logging.StreamHandler(sys.stdout)
    ]
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
use_tray = True
args = None
tray_icon = None
bear_sync_enabled = False
bear_dry_run = False


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


def build_bear_note_title(notebook_path):
    """
    Build a human-readable, path-based note title to avoid collisions.
    """
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
    logger.info("Bear sync params: title=%s tags=%s", params.get("title"), params.get("tags"))
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


def bear_trash_note(search_term, reason):
    """
    Trash a Bear note using a unique search term.
    """
    logger.info("Attempting Bear trash for reason='%s' search='%s'", reason, search_term)
    return bear_open_xurl("trash", {"search": search_term, "show_window": "no"})


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
    bear_title = build_bear_note_title(notebook_path)
    notebook_meta["bearTitle"] = bear_title
    note_text = (
        "Notebook Path: {}\n"
        "Last Synced: {}\n\n"
        "Attached: latest Kindle Scribe PDF export.\n\n"
        "<!-- {} -->\n"
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

    # Migrate notes created with the previous UUID title scheme.
    if previous_bear_title and previous_bear_title != bear_title:
        bear_trash_note(previous_bear_title, "migrate_title")

    if notebook_meta.get("bearCreated", False):
        bear_trash_note(marker, "refresh_existing_note")

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

def save_notebook_json():
    """
    Saves the `notebooks` global to a file
    """
    global notebooks
    logger.info("Saving notebook data file")
    with open(NOTEBOOK_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(notebooks, f)

def close_app():
    """
    shutdown the application
    """
    global driver
    global running
    running = False
    logger.info("Closing application")
    save_cookies()
    if tray_icon is not None:
        tray_icon.stop()
    if driver is not None:
        try:
            driver.quit()
        except Exception:
            pass
    try:
        sys.exit()
    except Exception:
        pass

def update_info(icon, item):
    """
    callback for `pystray` menu item. `Last Update` button notification stating the last update time and number of items updated.
    """
    global last_update
    global update_count
    notify_string = "Last Updated: {} | Updated '{}' item(s)".format(last_update, str(update_count))
    icon.notify(notify_string)

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

        if not id in parentItems:
            if 'path' in parentObj:
                newPath = os.path.join(parentObj['path'], safe_title)
            else:
                newPath = safe_title

            parentItems[id] = {
                'type': x['type'],
                'name': x['title'],
                'path': newPath,
                'updateTime': 0,
                'items': {},
                'bearCreated': False,
                'bearTitle': "Scribe {}".format(id),
            }
        else:
            ensure_notebook_tracking_defaults(id, parentItems[id])
        
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
            desired_bear_title = build_bear_note_title(parentItems[id]['path'])
            should_render_pdf = modification_time > current_update_time
            should_seed_bear = (
                bear_sync_enabled
                and os.path.exists(pdf_path)
                and (
                    not parentItems[id].get('bearCreated', False)
                    or parentItems[id].get('bearTitle') != desired_bear_title
                    or parentItems[id].get('bearSyncVersion', 0) < BEAR_SYNC_VERSION
                )
            )

            logger.info(
                "Notebook '%s': remote_modification=%s local_update=%s should_render_pdf=%s should_seed_bear=%s bear_title=%s bear_version=%s",
                parentItems[id]['name'],
                modification_time,
                current_update_time,
                should_render_pdf,
                should_seed_bear,
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
            elif should_seed_bear:
                logger.info("Skipping PDF render for '%s'; reusing existing PDF for initial Bear sync", parentItems[id]['name'])
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
                folder_path = os.path.join(SYNC_PATH, dict_object['path'])
                try:
                    logger.info("Pruning '{}' Folder".format(folder_path))
                    rmtree(folder_path)
                    update_count += 1
                except Exception:
                    logger.error("Pruning '{}' Folder Failed!".format(folder_path))
            if dict_object["type"] == "notebook":
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
    iterate_notebooks(data['itemsList'], notebooks)
    prune_orphans(notebooks, data['itemsList'])
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
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray integration.")
    parser.add_argument("--bear-sync", action="store_true", help="Sync updated notebook exports to Bear notes using x-callback-url (macOS only).")
    parser.add_argument("--bear-dry-run", action="store_true", help="Print Bear x-callback URLs without opening them.")
    parser.add_argument("--update-minutes", type=int, default=UPDATE_MINUTES, help="Minutes between sync checks when running continuously.")
    return parser.parse_args()


def setup_tray_if_available():
    global tray_icon
    if not use_tray:
        return
    if pystray is None or Image is None:
        logger.warning("pystray/Pillow not available; running without tray.")
        return

    icon_path = Path(__file__).resolve().parent / "KindleScribeSyncIcon.png"
    if not icon_path.exists():
        logger.warning("Tray icon image not found; running without tray.")
        return

    menu = pystray.Menu(
        pystray.MenuItem('Last Update', update_info),
        pystray.MenuItem('Force Sync', check_notebooks),
        pystray.MenuItem('Quit', close_app)
    )

    tray_image = Image.open(str(icon_path))
    tray_icon = pystray.Icon("Kindle Scribe Sync", tray_image, "Kindle Scribe Sync", menu)
    logger.info("Running Tray Icon")
    tray_icon.run_detached()


def main():
    global UPDATE_MINUTES
    global use_tray
    global args
    global bear_sync_enabled
    global bear_dry_run

    args = parse_args()
    UPDATE_MINUTES = max(args.update_minutes, 1)
    use_tray = not args.no_tray
    bear_sync_enabled = args.bear_sync
    bear_dry_run = args.bear_dry_run

    if not os.path.exists(EXTRACT_PATH):
        os.mkdir(EXTRACT_PATH)

    # Ensure output root exists to avoid path errors on first run.
    os.makedirs(SYNC_PATH, exist_ok=True)

    load_notebook_json()
    setup_tray_if_available()

    logger.info("Running initial check")
    check_notebooks()

    if args.once:
        logger.info("Single-run mode complete")
        return

    schedule.every(UPDATE_MINUTES).minutes.do(check_notebooks)
    while running:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()