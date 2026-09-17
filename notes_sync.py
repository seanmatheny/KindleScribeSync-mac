#!/usr/bin/env python3
"""
OCR the exported Kindle Scribe PDFs into a notes app, and collect handwritten
"TODO:" lines as tasks.

The PDFs stay the source of truth. Every pass compares each notebook's PDF with
what was last written to the notes app and repairs whatever no longer matches: a
changed PDF, a note that was edited or trashed, a renamed notebook. When everything
already matches, nothing is written.

Handwriting recognition is Apple's Vision framework, reached through the small
Swift program in ocr/ (built on demand into bin/scribe-ocr). Bear is driven through
the `bearcli` tool that ships inside Bear.app.
"""

import fcntl
import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from fnmatch import fnmatch
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / "notes_sync_state.json"
CACHE_DIR = BASE_DIR / "notes_sync_cache"
LOCK_PATH = BASE_DIR / "notes_sync.lock"
OCR_SOURCE = BASE_DIR / "ocr" / "scribe-ocr.swift"
OCR_BUILD_SCRIPT = BASE_DIR / "ocr" / "build.sh"
OCR_BINARY = BASE_DIR / "bin" / "scribe-ocr"
DEFAULT_BEARCLI = "/Applications/Bear.app/Contents/MacOS/bearcli"

STATE_VERSION = 1
SUPPORTED_NOTES_APPS = ("bear",)
SUPPORTED_TODO_APPS = ("bear",)

OCR_TIMEOUT = 900  # the first run after a reboot can spend ~30s loading models
BUILD_TIMEOUT = 600
BEAR_TIMEOUT = 120

## Layout heuristics, in fractions of the page.
# A row further than this many typical row pitches from the previous one starts a paragraph.
PARAGRAPH_GAP = 1.6
# A TODO line reaching this far across the page ran out of room and wraps onto the next line.
WRAP_MARGIN = 0.80
# Two OCR readings this similar are the same handwritten TODO read slightly differently.
SAME_TODO_RATIO = 0.80


class SyncError(Exception):
    """An expected failure, reported to the log without a traceback."""


@dataclass
class Settings:
    notes_app: str = "bear"
    root_tag: str = "scribe"
    attach_pdf: bool = True
    exclude: list = field(default_factory=list)
    force_resync: bool = False
    ocr_languages: list = field(default_factory=lambda: ["en-US"])
    todo_sync: bool = False
    todo_app: str = "bear"
    todo_note_title: str = "Scribe Tasks"
    bearcli_path: str = DEFAULT_BEARCLI


@dataclass
class Notebook:
    id: str
    name: str
    path: str  # relative to the sync folder, without ".pdf"


@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def middle(self):
        return self.y + self.h / 2

    @property
    def right(self):
        return self.x + self.w


@dataclass
class Todo:
    text: str
    page: int


## OCR


def ensure_ocr_binary():
    """
    Build bin/scribe-ocr if it is missing or older than its source.
    """
    if OCR_BINARY.exists() and OCR_BINARY.stat().st_mtime >= OCR_SOURCE.stat().st_mtime:
        return OCR_BINARY

    logger.info("Building %s", OCR_BINARY)
    result = subprocess.run(
        ["/bin/sh", str(OCR_BUILD_SCRIPT)], capture_output=True, text=True, timeout=BUILD_TIMEOUT
    )
    if result.returncode != 0:
        raise SyncError(
            "Could not build the OCR helper; are the Xcode Command Line Tools installed "
            "(xcode-select --install)? {}".format((result.stderr or result.stdout).strip())
        )
    return OCR_BINARY


def run_ocr(pdf_path, languages):
    """
    Recognise the handwriting in a PDF. Returns the helper's JSON: text fragments
    per page with normalised, top-left-origin bounding boxes.
    """
    binary = ensure_ocr_binary()
    result = subprocess.run(
        [str(binary), str(pdf_path), "--languages", ",".join(languages)],
        capture_output=True,
        timeout=OCR_TIMEOUT,
    )
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise SyncError(
            "OCR helper returned no JSON (exit {}): {}".format(
                result.returncode, result.stderr.decode("utf-8", "replace").strip()
            )
        )
    if "error" in payload:
        raise SyncError("OCR failed: {}".format(payload["error"].get("message")))
    return payload


def load_ocr(notebook, pdf_path, pdf_hash, languages):
    """
    OCR a notebook, reusing the cached result while the PDF, the helper and the
    languages are unchanged. Notes get rewritten for reasons that do not involve the
    PDF (an edit in Bear, a rename), and those should not cost a recognition run.
    """
    key = {
        "pdf": pdf_hash,
        "helper": hashlib.sha256(OCR_SOURCE.read_bytes()).hexdigest(),
        "languages": list(languages),
    }
    cache_path = CACHE_DIR / "{}.json".format(re.sub(r"[^A-Za-z0-9_.-]", "_", notebook.id))
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("key") == key:
            return cached["ocr"]
    except (OSError, ValueError, KeyError):
        pass

    logger.info("Running OCR on '%s'", notebook.path)
    ocr = run_ocr(pdf_path, languages)
    CACHE_DIR.mkdir(exist_ok=True)
    write_json_atomically(cache_path, {"key": key, "ocr": ocr})
    return ocr


## Page layout


def horizontal_overlap(a, b):
    return max(0.0, min(a.right, b.right) - max(a.x, b.x))


def shares_row(row, fragment):
    top = min(item.y for item in row)
    bottom = max(item.y + item.h for item in row)
    if abs(fragment.middle - (top + bottom) / 2) > 0.5 * min(bottom - top, fragment.h):
        return False
    # Text stacked over other text (a word squeezed in above a line) is its own line.
    return all(horizontal_overlap(fragment, item) <= 0.2 * min(fragment.w, item.w) for item in row)


def join_row(row):
    row = sorted(row, key=lambda item: item.x)
    x = min(item.x for item in row)
    y = min(item.y for item in row)
    return Line(
        text=" ".join(item.text for item in row),
        x=x,
        y=y,
        w=max(item.right for item in row) - x,
        h=max(item.y + item.h for item in row) - y,
    )


def preferred_reading(observation):
    """
    Pick between Vision's readings of a fragment. It reads a handwritten "TODO:" as
    "TODD:" often enough to lose tasks, while scoring both readings the same. So when
    its first choice carries no TODO marker and one of its alternates does, take the
    alternate; Vision has to have proposed it, which keeps a real "Todd:" safe.
    """
    text = observation.get("text", "").strip()
    if text and split_todo(text) is None:
        for alternate in observation.get("alternates", []):
            if split_todo(alternate) is not None:
                return alternate.strip()
    return text


def layout_page(observations):
    """
    Arrange one page's OCR fragments into paragraphs of lines. Vision often returns
    a handwritten line in several pieces, in no dependable order, so pieces at the
    same height are joined left to right; a skipped rule starts a new paragraph.
    """
    fragments = sorted(
        (
            Line(preferred_reading(o), o["x"], o["y"], o["w"], o["h"])
            for o in observations
            if o.get("text", "").strip()
        ),
        key=lambda item: item.middle,
    )
    rows = []
    for fragment in fragments:
        if rows and shares_row(rows[-1], fragment):
            rows[-1].append(fragment)
        else:
            rows.append([fragment])

    lines = [join_row(row) for row in rows]
    if not lines:
        return []

    paragraphs = [[lines[0]]]
    if len(lines) > 1:
        pitches = [after.middle - before.middle for before, after in zip(lines, lines[1:])]
        typical = statistics.median(pitches)
        for line, pitch in zip(lines[1:], pitches):
            if pitch > typical * PARAGRAPH_GAP:
                paragraphs.append([line])
            else:
                paragraphs[-1].append(line)
    return paragraphs


def layout_pages(ocr):
    return [layout_page(page.get("observations", [])) for page in ocr.get("pages", [])]


## TODO extraction

# Handwriting costs the recogniser the odd O (read as 0) and colon (read as another
# mark), so the marker is matched loosely where the intent is unmistakable:
#   "TODO: x", "- T0DO; x", "TODO - x", or a bare "TODO" heading, opening a line;
#   "To do: x" opening a line, colon required;
#   "... TODO: x" anywhere, upper case and colon required.
# A delimiter is always needed, so a heading like "TODO list" is not a task.
TODO_MARKERS = (
    re.compile(r"^(?P<lead>\W*)T[O0]D[O0]\s*(?:[:;.,\-–—]+\s*|$)"),
    re.compile(r"^(?P<lead>\W*)to[\s-]?do\s*[:;]\s*", re.IGNORECASE),
    re.compile(r"(?P<lead>)\bT[O0]D[O0]\s*[:;]\s*"),
)
BULLET = re.compile(r"^\s*(?:[-–—•*·▪◦‣+=]|\d{1,2}[.)])\s+")


def split_todo(text):
    """
    Split a line around its TODO marker. Returns (text before, task text), or None
    when the line carries no marker.
    """
    for marker in TODO_MARKERS:
        match = marker.search(text)
        if match:
            return text[: match.start()] + match.group("lead"), text[match.end() :]
    return None


def clean_task(text):
    text = BULLET.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" \t-–—:;,")


def extract_todos(pages):
    """
    Find the tasks on every page:
      - "TODO: wash the car" is one task;
      - a TODO line that runs to the right-hand margin continues onto the next line;
      - a bare "TODO:" makes tasks of the bullets under it, or of the one line under it.
    """
    todos = []
    for page_number, paragraphs in enumerate(pages, start=1):
        for paragraph in paragraphs:
            index = 0
            while index < len(paragraph):
                line = paragraph[index]
                index += 1
                found = split_todo(line.text)
                if found is None:
                    continue

                tasks = [found[1]]
                following = paragraph[index:]
                if not clean_task(found[1]):
                    tasks = []
                    bulleted = bool(following) and bool(BULLET.match(following[0].text))
                    for candidate in following:
                        if split_todo(candidate.text) or bool(BULLET.match(candidate.text)) != bulleted:
                            break
                        tasks.append(candidate.text)
                        index += 1
                        if not bulleted:
                            break
                elif line.right >= WRAP_MARGIN and following:
                    candidate = following[0]
                    if not split_todo(candidate.text) and not BULLET.match(candidate.text):
                        tasks = ["{} {}".format(found[1], candidate.text)]
                        index += 1

                for task in tasks:
                    task = clean_task(task)
                    if len(re.sub(r"[\W_]", "", task)) >= 2:
                        todos.append(Todo(task, page_number))
    return todos


def todo_key(text):
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


## Note rendering


def escape_markdown(text):
    """
    Keep recognised text literal. A stray "#word" would otherwise become a tag in
    Bear's sidebar, and OCR noise is full of stray symbols.
    """
    text = re.sub(r"([\\`*_\[\]<#])", r"\\\1", text)
    text = text.replace("~~", r"\~\~").replace("==", r"\=\=")
    if re.match(r"^\s*(>|[-=_]{3,}\s*$)", text):
        text = "\\" + text
    return text


def render_line(text):
    found = split_todo(text)
    if found is None:
        return escape_markdown(text)
    before, task = found
    return "{}**TODO:** {}".format(escape_markdown(before), escape_markdown(task)).rstrip()


def slugify_tag_part(value):
    cleaned = re.sub(r"\s+", "-", value.strip().lower())
    return re.sub(r"[^a-z0-9_\-]", "", cleaned).strip("-") or "untitled"


def note_tag(notebook, root_tag):
    folders = notebook.path.split(os.sep)[:-1]
    return "/".join([root_tag] + [slugify_tag_part(folder) for folder in folders])


def note_titles(notebooks):
    """
    Plain notebook names where they are unique, "Folder / Name" where they collide,
    as the PDF-attachment Bear target does.
    """
    counts = Counter(notebook.name for notebook in notebooks)
    return {
        notebook.id: notebook.name if counts[notebook.name] == 1 else " / ".join(notebook.path.split(os.sep))
        for notebook in notebooks
    }


def sync_marker(notebook):
    return "Sync ID {}".format(notebook.id)


def render_note(notebook, title, pages, pdf_mtime, settings):
    """
    Build the note body. It must come out identical for identical input, because
    its hash decides whether the note needs rewriting: no "synced at" timestamps.
    """
    out = ["# {}".format(escape_markdown(title)), "#{}".format(note_tag(notebook, settings.root_tag)), ""]
    for page_number, paragraphs in enumerate(pages, start=1):
        out += ["## Page {}".format(page_number), ""]
        if not paragraphs:
            out += ["*No handwriting recognised on this page.*", ""]
        for paragraph in paragraphs:
            out += [render_line(line.text) for line in paragraph]
            out.append("")

    out += [
        "---",
        "*Recognised from the handwriting in Kindle Scribe notebook “{}” ({} page{}, last changed {}). "
        "The notebook is the source of truth: edits made to this note are overwritten.*".format(
            escape_markdown(notebook.path),
            len(pages),
            "" if len(pages) == 1 else "s",
            datetime.fromtimestamp(pdf_mtime).strftime("%Y-%m-%d %H:%M"),
        ),
        "*{}*".format(sync_marker(notebook)),
        "",
    ]
    if settings.attach_pdf:
        # Bear appends the attachment's link to the end of the note, so it lands here.
        out += ["## Handwritten original", ""]
    return "\n".join(out)


## Bear


class BearCLI:
    """
    Thin wrapper over the `bearcli` tool shipped inside Bear.app (Bear 2.10+).
    It talks to Bear's database directly, so Bear does not need to be running.
    """

    def __init__(self, path):
        self.path = path

    def check(self):
        if not os.access(self.path, os.X_OK):
            raise SyncError("Bear's command line tool was not found at {}; Bear 2.10 or later is required".format(self.path))

    def run(self, args, stdin=None):
        return subprocess.run([self.path, *args], input=stdin, capture_output=True, timeout=BEAR_TIMEOUT)

    def query(self, args):
        """Run a read command. Returns the parsed JSON, or None if the note does not exist."""
        result = self.run([*args, "--format", "json"])
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise SyncError("bearcli {} failed: {}".format(args[0], result.stderr.decode("utf-8", "replace").strip()))
        if isinstance(payload, dict) and "error" in payload:
            if payload["error"].get("code") == "note_not_found":
                return None
            raise SyncError("bearcli {} failed: {}".format(args[0], payload["error"].get("message")))
        return payload

    def command(self, args, stdin=None):
        """Run a write command; these print nothing and signal failure by exit code."""
        result = self.run(args, stdin=stdin)
        if result.returncode != 0:
            raise SyncError("bearcli {} failed: {}".format(args[0], result.stderr.decode("utf-8", "replace").strip()))

    def read(self, note_id):
        """Returns (content, location) with location one of notes/archive/trash, or None."""
        note = self.query(["show", note_id, "--fields", "id,location,content"])
        return None if note is None else (note.get("content", ""), note.get("location", "notes"))

    def find(self, phrase):
        """Find a note by an exact phrase in its text, preferring live notes over trashed ones."""
        matches = self.query(["search", '"{}"'.format(phrase), "--location", "all", "--fields", "id,location"]) or []
        order = {"notes": 0, "archive": 1, "trash": 2}
        matches.sort(key=lambda match: order.get(match.get("location"), 3))
        return matches[0]["id"] if matches else None

    def create(self, content, title=None):
        """Create a note; with a title, return the existing note of that name instead if there is one."""
        args = ["create", "--fields", "id"] + ([title, "--if-not-exists"] if title else [])
        result = self.run([*args, "--format", "json"], stdin=content.encode("utf-8"))
        try:
            return json.loads(result.stdout)["id"]
        except (ValueError, KeyError):
            raise SyncError(
                "bearcli create failed: {}".format((result.stderr or result.stdout).decode("utf-8", "replace").strip())
            )

    def overwrite(self, note_id, content):
        # --force lets the rewrite drop the previous PDF attachment.
        self.command(["overwrite", note_id, "--force"], stdin=content.encode("utf-8"))

    def attach(self, note_id, filename, data):
        self.command(["attachments", "add", note_id, "--filename", filename], stdin=data)

    def append(self, note_id, content, section=None):
        args = ["append", note_id] + (["--section", section] if section else [])
        self.command(args, stdin=content.encode("utf-8"))

    def restore(self, note_id):
        self.command(["restore", note_id])

    def trash(self, note_id):
        self.command(["trash", note_id])

    @staticmethod
    def link(note_id):
        return "bear://x-callback-url/open-note?id={}".format(note_id)


class BearTasks:
    """
    One Bear note collecting every TODO as a checkbox. It is only ever appended to,
    so ticking tasks off, rewording or reorganising them there is safe.
    """

    INBOX = "## Inbox"

    def __init__(self, bear, state, settings):
        self.bear = bear
        self.state = state
        self.settings = settings

    def note(self):
        """Returns (note id, current content) of the tasks note, creating or restoring it as needed."""
        note_id = self.state.get("tasksNoteId")
        found = self.bear.read(note_id) if note_id else None
        if found is None:
            title = self.settings.todo_note_title
            note_id = self.bear.create(
                "# {}\n#{}\n\nHandwritten TODO lines from Kindle Scribe notebooks are collected here. Tick them off, "
                "reword or reorganise them freely: new tasks are only ever added, nothing here is rewritten.\n\n"
                "{}\n".format(escape_markdown(title), self.settings.root_tag, self.INBOX),
                title=title,
            )
            self.state["tasksNoteId"] = note_id
            found = self.bear.read(note_id)
            if found is None:
                raise SyncError("tasks note {} disappeared straight after being created".format(note_id))
        if found[1] == "trash":
            self.bear.restore(note_id)
        return note_id, found[0]

    def add(self, todo, source_title, source_note_id):
        """Append a task. Returns False if the note already lists it (the sync state was lost)."""
        note_id, content = self.note()
        text = escape_markdown(todo.text)
        link = self.bear.link(source_note_id)
        if any(text in line and link in line for line in content.splitlines()):
            return False

        line = "- [ ] {} — [{}, p. {}]({}) · {}".format(
            text, escape_markdown(source_title), todo.page, link, datetime.now().strftime("%Y-%m-%d")
        )
        try:
            self.bear.append(note_id, line, section=self.INBOX)
        except SyncError:
            # The Inbox heading was renamed or removed; the end of the note will do.
            self.bear.append(note_id, line)
        return True


## State


def write_json_atomically(path, payload):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("version") == STATE_VERSION:
            return state
        logger.warning("Ignoring %s: unknown version %s", STATE_PATH.name, state.get("version"))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as ex:
        logger.warning("Could not read %s (%s); existing notes will be found again by their sync IDs", STATE_PATH.name, ex)
    return {"version": STATE_VERSION, "notebooks": {}}


def save_state(state):
    write_json_atomically(STATE_PATH, state)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


## Reconciliation


def flatten_notebooks(tree):
    notebooks = []
    for item_id, meta in tree.items():
        if meta.get("type") == "folder":
            notebooks += flatten_notebooks(meta.get("items", {}))
        elif meta.get("type") == "notebook":
            notebooks.append(Notebook(item_id, meta.get("name") or "Untitled Notebook", meta["path"]))
    return notebooks


def is_excluded(notebook, patterns):
    return any(fnmatch(notebook.path.lower(), pattern.lower()) for pattern in patterns)


def sync_note(bear, notebook, title, pages, pdf_path, pdf_hash, entry, settings):
    """
    Make the notebook's note match its PDF. Returns a short reason when the note had
    to be written, or None when it already matched.
    """
    content = render_note(notebook, title, pages, pdf_path.stat().st_mtime, settings)
    # The attachment is part of what the note should hold, so a PDF whose text is
    # unchanged (a sketch was added, say) still refreshes it.
    desired = sha256("{}\0{}".format(content, pdf_hash if settings.attach_pdf else "").encode("utf-8"))

    note_id = entry.get("noteId")
    found = bear.read(note_id) if note_id else None
    if found is None:
        # No record of a note, or the record is stale: adopt one left by an earlier
        # install (or a lost state file) rather than creating a duplicate.
        note_id = bear.find(sync_marker(notebook))
        found = bear.read(note_id) if note_id else None

    if found is None:
        reason = "created"
    elif found[1] == "trash":
        reason = "restored from Bear's trash"
    elif settings.force_resync:
        reason = "forced"
    elif entry.get("desiredHash") != desired:
        reason = "notebook changed" if entry.get("desiredHash") else "adopted existing note"
    elif sha256(found[0].encode("utf-8")) != entry.get("noteHash"):
        reason = "note was edited in Bear"
    else:
        return None

    if found is None:
        note_id = bear.create(content)
    else:
        if found[1] == "trash":
            bear.restore(note_id)
        bear.overwrite(note_id, content)
    entry["noteId"] = note_id
    if settings.attach_pdf:
        bear.attach(note_id, "{}.pdf".format(os.path.basename(notebook.path)), pdf_path.read_bytes())

    # Hash what Bear now holds (it has added the attachment link), not what was sent.
    written = bear.read(note_id)
    if written is None:
        raise SyncError("note {} disappeared straight after being written".format(note_id))
    entry.update(
        {
            "path": notebook.path,
            "title": title,
            "pdfHash": pdf_hash,
            "desiredHash": desired,
            "noteHash": sha256(written[0].encode("utf-8")),
            "syncedAt": datetime.now().isoformat(timespec="seconds"),
        }
    )
    return reason


def sync_todos(tasks, pages, title, entry):
    """
    Add tasks for TODO lines not seen before. Returns how many were added.
    A task is delivered once: ticking it off, rewording or deleting it afterwards
    never brings it back.
    """
    known = entry.setdefault("todos", {})
    found = {}
    for todo in extract_todos(pages):
        found.setdefault(todo_key(todo.text), todo)

    vanished = [key for key in known if key not in found]
    added = 0
    for key, todo in found.items():
        if not key or key in known:
            continue
        # Re-rendered handwriting can be read a little differently. If a known TODO
        # has just vanished and this one is nearly identical, it is the same line.
        twin = next((old for old in vanished if SequenceMatcher(None, old, key).ratio() >= SAME_TODO_RATIO), None)
        if twin is not None:
            vanished.remove(twin)
            known[key] = known.pop(twin)
            continue

        if tasks.add(todo, title, entry["noteId"]):
            added += 1
            logger.info("Added task from '%s' p.%s: %s", title, todo.page, todo.text)
        known[key] = {"text": todo.text, "page": todo.page, "addedAt": datetime.now().isoformat(timespec="seconds")}
    return added


def remove_orphans(bear, state, notebooks):
    """
    Trash the notes of notebooks that no longer exist on the Kindle.
    """
    current = {notebook.id for notebook in notebooks}
    orphans = [notebook_id for notebook_id in state["notebooks"] if notebook_id not in current]
    if orphans and not current:
        # An empty listing is far more likely a bad API response than a wiped Kindle.
        logger.warning("No notebooks listed; leaving %s synced notes alone", len(orphans))
        return
    for notebook_id in orphans:
        entry = state["notebooks"][notebook_id]
        try:
            if entry.get("noteId") and bear.read(entry["noteId"]) is not None:
                bear.trash(entry["noteId"])
                logger.info("Notebook '%s' is gone; moved its note to Bear's trash", entry.get("path"))
        except SyncError as ex:
            logger.error("Could not trash the note for removed notebook '%s': %s", entry.get("path"), ex)
            continue
        del state["notebooks"][notebook_id]
        (CACHE_DIR / "{}.json".format(re.sub(r"[^A-Za-z0-9_.-]", "_", notebook_id))).unlink(missing_ok=True)


def reconcile(tree, sync_path, settings):
    """
    Bring the notes app in line with the exported PDFs.
    `tree` is the notebooks.json structure; `sync_path` is the folder holding the PDFs.
    """
    if settings.notes_app not in SUPPORTED_NOTES_APPS:
        raise SyncError("notes_app '{}' is not supported yet (supported: {})".format(settings.notes_app, ", ".join(SUPPORTED_NOTES_APPS)))
    if settings.todo_sync and settings.todo_app not in SUPPORTED_TODO_APPS:
        raise SyncError("todo_app '{}' is not supported yet (supported: {})".format(settings.todo_app, ", ".join(SUPPORTED_TODO_APPS)))

    bear = BearCLI(settings.bearcli_path)
    bear.check()
    ensure_ocr_binary()  # fail once here rather than once per notebook

    with open(LOCK_PATH, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logger.info("Another notes sync is running; skipping this pass")
            return

        state = load_state()
        tasks = BearTasks(bear, state, settings) if settings.todo_sync else None
        notebooks = sorted(flatten_notebooks(tree), key=lambda notebook: notebook.path)
        titles = note_titles(notebooks)
        wanted = [notebook for notebook in notebooks if not is_excluded(notebook, settings.exclude)]
        written = added = failed = 0

        for notebook in wanted:
            pdf_path = Path(sync_path) / "{}.pdf".format(notebook.path)
            if not pdf_path.exists():
                logger.warning("No PDF yet for '%s'; skipping notes sync", notebook.path)
                continue
            entry = state["notebooks"].setdefault(notebook.id, {})
            try:
                pdf_hash = sha256(pdf_path.read_bytes())
                pages = layout_pages(load_ocr(notebook, pdf_path, pdf_hash, settings.ocr_languages))
                reason = sync_note(bear, notebook, titles[notebook.id], pages, pdf_path, pdf_hash, entry, settings)
                if reason:
                    written += 1
                    logger.info("Bear note for '%s' written (%s)", notebook.path, reason)
                if tasks is not None:
                    added += sync_todos(tasks, pages, titles[notebook.id], entry)
            except (SyncError, subprocess.TimeoutExpired, OSError) as ex:
                failed += 1
                logger.error("Notes sync failed for '%s': %s", notebook.path, ex)
            except Exception:
                failed += 1
                logger.exception("Notes sync failed for '%s'", notebook.path)
            if not entry:
                del state["notebooks"][notebook.id]
            save_state(state)

        remove_orphans(bear, state, notebooks)
        save_state(state)
        logger.info(
            "Notes sync: %s notebooks checked, %s notes written, %s tasks added, %s failed",
            len(wanted), written, added, failed,
        )
