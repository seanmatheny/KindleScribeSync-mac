"""
Tests for notes_sync: page layout, TODO extraction, note rendering, and the
decisions reconcile makes, run against an in-memory stand-in for Bear.

    python -m unittest discover tests
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import notes_sync
from notes_sync import Line, Notebook, Settings, Todo


def fragment(text, x, y, w=0.3, h=0.04, alternates=()):
    return {"text": text, "alternates": list(alternates), "x": x, "y": y, "w": w, "h": h, "confidence": 1.0}


def page_of(*texts):
    """A page holding one paragraph of short, left-aligned lines."""
    return [[Line(text, 0.05, 0.1 + 0.045 * row, 0.3, 0.04) for row, text in enumerate(texts)]]


def tasks_in(*texts):
    return [todo.text for todo in notes_sync.extract_todos([page_of(*texts)])]


class LayoutTests(unittest.TestCase):
    def test_fragments_on_one_row_are_joined_left_to_right(self):
        page = notes_sync.layout_page([fragment("the car", 0.45, 0.101), fragment("TODO: Wash", 0.05, 0.10)])
        self.assertEqual([[line.text for line in paragraph] for paragraph in page], [["TODO: Wash the car"]])

    def test_text_squeezed_in_above_a_line_stays_separate(self):
        page = notes_sync.layout_page([fragment("colleagues in Mayo", 0.05, 0.50, w=0.6), fragment("Edinburgh", 0.40, 0.485, w=0.2)])
        self.assertEqual(sorted(line.text for paragraph in page for line in paragraph), ["Edinburgh", "colleagues in Mayo"])

    def test_a_skipped_rule_starts_a_new_paragraph(self):
        rows = [0.10, 0.145, 0.19, 0.28, 0.325]
        page = notes_sync.layout_page([fragment("line {}".format(i), 0.05, y) for i, y in enumerate(rows)])
        self.assertEqual([len(paragraph) for paragraph in page], [3, 2])

    def test_rows_come_out_top_to_bottom_whatever_order_vision_used(self):
        page = notes_sync.layout_page([fragment("second", 0.05, 0.145), fragment("first", 0.05, 0.10)])
        self.assertEqual([line.text for line in page[0]], ["first", "second"])

    def test_empty_page(self):
        self.assertEqual(notes_sync.layout_page([]), [])

    def test_alternate_reading_rescues_a_misread_todo(self):
        misread = fragment("TODD: Thank Claude", 0.05, 0.1, alternates=["TODD: Thank Claud", "TODO: Thank Claude", "TODO: Thank Claud"])
        page = notes_sync.layout_page([misread])
        self.assertEqual(page[0][0].text, "TODO: Thank Claude")
        self.assertEqual(notes_sync.extract_todos([page]), [Todo("Thank Claude", 1)])

    def test_first_reading_wins_otherwise(self):
        todd = fragment("TODD: will handle the budget", 0.05, 0.1, alternates=["TODD: will handle the budge"])
        self.assertEqual(notes_sync.layout_page([todd])[0][0].text, "TODD: will handle the budget")
        already = fragment("TODO: Wash the car", 0.05, 0.1, alternates=["TODO: Wash he car"])
        self.assertEqual(notes_sync.layout_page([already])[0][0].text, "TODO: Wash the car")
        plain = {"text": "no alternates key", "x": 0.05, "y": 0.1, "w": 0.3, "h": 0.04}
        self.assertEqual(notes_sync.layout_page([plain])[0][0].text, "no alternates key")


class TodoTests(unittest.TestCase):
    def test_the_sample_notebook(self):
        self.assertEqual(tasks_in("TODO: Wash the car"), ["Wash the car"])

    def test_marker_variants_the_recogniser_produces(self):
        for line in ("- TODO: call Bob", "T0DO; call Bob", "TODO - call Bob", "To do: call Bob", "todo: call Bob", "• TOD0: call Bob"):
            self.assertEqual(tasks_in(line), ["call Bob"], line)

    def test_marker_in_the_middle_of_a_line(self):
        self.assertEqual(tasks_in("Budget meeting. TODO: send slides"), ["send slides"])

    def test_lines_that_are_not_tasks(self):
        for line in ("TODO list for the garden", "TODOs: many", "things to do: relax", "nothing to do with it", "TODO wash the car"):
            self.assertEqual(tasks_in(line), [], line)

    def test_task_needs_some_text(self):
        self.assertEqual(tasks_in("TODO: -"), [])

    def test_bare_heading_takes_the_bullets_under_it(self):
        self.assertEqual(tasks_in("TODO:", "- wash car", "- buy milk", "Meeting notes"), ["wash car", "buy milk"])

    def test_bare_heading_without_bullets_takes_one_line(self):
        self.assertEqual(tasks_in("TODO", "wash car", "unrelated note"), ["wash car"])

    def test_bare_heading_stops_at_the_paragraph(self):
        pages = [[[Line("TODO:", 0.05, 0.1, 0.1, 0.04)], [Line("- unrelated", 0.05, 0.3, 0.3, 0.04)]]]
        self.assertEqual(notes_sync.extract_todos(pages), [])

    def test_line_reaching_the_margin_wraps_onto_the_next(self):
        paragraph = [Line("TODO: email Bob about the", 0.05, 0.10, 0.85, 0.04), Line("budget numbers", 0.05, 0.145, 0.3, 0.04)]
        self.assertEqual(notes_sync.extract_todos([[paragraph]]), [Todo("email Bob about the budget numbers", 1)])

    def test_short_line_does_not_swallow_the_next(self):
        self.assertEqual(tasks_in("TODO: email Bob", "budget numbers"), ["email Bob"])

    def test_wrap_does_not_swallow_a_bullet_or_another_todo(self):
        wide = dict(x=0.05, w=0.85, h=0.04)
        for following in ("- next point", "TODO: second"):
            paragraph = [Line("TODO: first", y=0.10, **wide), Line(following, y=0.145, **wide)]
            self.assertEqual(notes_sync.extract_todos([[paragraph]])[0].text, "first", following)

    def test_page_numbers(self):
        pages = [page_of("nothing here"), page_of("TODO: on page two")]
        self.assertEqual(notes_sync.extract_todos(pages), [Todo("on page two", 2)])


class RenderTests(unittest.TestCase):
    notebook = Notebook("724f0e7f-ebdd", "todo", os.path.join("Work", "todo"))

    def render(self, pages, **settings):
        return notes_sync.render_note(self.notebook, "todo", pages, 0, Settings(**settings))

    def test_recognised_text_cannot_create_tags_or_headings(self):
        self.assertEqual(notes_sync.escape_markdown("#budget is *big*"), r"\#budget is \*big\*")
        self.assertEqual(notes_sync.escape_markdown("# Chris"), r"\# Chris")
        self.assertEqual(notes_sync.escape_markdown("---"), r"\---")
        self.assertEqual(notes_sync.escape_markdown("> quoted"), r"\> quoted")
        self.assertEqual(notes_sync.escape_markdown("- a bullet stays a bullet"), "- a bullet stays a bullet")

    def test_note_layout(self):
        note = self.render([page_of("TODO: Wash the car"), []])
        self.assertTrue(note.startswith("# todo\n#scribe/work\n\n## Page 1\n\n**TODO:** Wash the car\n"))
        self.assertIn("## Page 2\n\n*No handwriting recognised on this page.*", note)
        self.assertIn("Sync ID 724f0e7f-ebdd", note)
        self.assertTrue(note.endswith("## Handwritten original\n"))

    def test_bullet_before_the_marker_survives(self):
        self.assertEqual(notes_sync.render_line("- T0DO; call Bob"), "- **TODO:** call Bob")

    def test_no_attachment_heading_without_attachment(self):
        self.assertNotIn("Handwritten original", self.render([page_of("x")], attach_pdf=False))

    def test_rendering_is_deterministic(self):
        self.assertEqual(self.render([page_of("x")]), self.render([page_of("x")]))

    def test_tags_follow_the_folders(self):
        nested = Notebook("id", "Notebook 1", os.path.join("Personal", "book notes", "Notebook 1"))
        self.assertEqual(notes_sync.note_tag(nested, "scribe"), "scribe/personal/book-notes")
        self.assertEqual(notes_sync.note_tag(Notebook("id", "Loose", "Loose"), "scribe"), "scribe")

    def test_titles_fall_back_to_the_path_when_names_collide(self):
        notebooks = [
            Notebook("a", "Notebook 1", os.path.join("Work", "Notebook 1")),
            Notebook("b", "Notebook 1", os.path.join("Personal", "Notebook 1")),
            Notebook("c", "todo", os.path.join("Work", "todo")),
        ]
        self.assertEqual(notes_sync.note_titles(notebooks), {"a": "Work / Notebook 1", "b": "Personal / Notebook 1", "c": "todo"})


class FakeBear:
    """Holds notes in memory and mimics the parts of bearcli's behaviour reconcile relies on."""

    def __init__(self):
        self.notes = {}
        self.writes = 0

    def read(self, note_id):
        note = self.notes.get(note_id)
        return None if note is None else (note["content"], note["location"])

    def find(self, phrase):
        return next((note_id for note_id, note in self.notes.items() if phrase in note["content"]), None)

    def create(self, content, title=None):
        if title is not None:  # --if-not-exists
            for note_id, note in self.notes.items():
                if note["content"].startswith("# {}\n".format(title)):
                    return note_id
        note_id = "note-{}".format(len(self.notes) + 1)
        self.notes[note_id] = {"content": content, "location": "notes"}
        self.writes += 1
        return note_id

    def overwrite(self, note_id, content):
        self.notes[note_id]["content"] = content
        self.writes += 1

    def attach(self, note_id, filename, data):
        self.notes[note_id]["content"] += "[{0}]({0})<!-- {{\"embed\":\"true\"}} -->\n".format(filename)

    def append(self, note_id, content, section=None):
        self.notes[note_id]["content"] += content + "\n"

    def restore(self, note_id):
        self.notes[note_id]["location"] = "notes"

    def trash(self, note_id):
        self.notes[note_id]["location"] = "trash"

    link = staticmethod(notes_sync.BearCLI.link)


class SyncNoteTests(unittest.TestCase):
    def setUp(self):
        self.bear = FakeBear()
        self.notebook = Notebook("nb-1", "todo", os.path.join("Work", "todo"))
        self.entry = {}
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pdf = Path(self.directory.name) / "todo.pdf"
        self.pdf.write_bytes(b"%PDF-1")

    def sync(self, *lines, pdf_hash="hash-1", **settings):
        return notes_sync.sync_note(
            self.bear, self.notebook, "todo", [page_of(*lines)], self.pdf, pdf_hash, self.entry, Settings(**settings)
        )

    def test_creates_then_leaves_a_matching_note_alone(self):
        self.assertEqual(self.sync("TODO: Wash the car"), "created")
        self.assertIn("[todo.pdf](todo.pdf)", self.bear.notes["note-1"]["content"])
        self.assertIsNone(self.sync("TODO: Wash the car"))
        self.assertEqual(self.bear.writes, 1)

    def test_changed_notebook_is_rewritten_in_place(self):
        self.sync("first draft")
        self.assertEqual(self.sync("second draft", pdf_hash="hash-2"), "notebook changed")
        self.assertEqual(list(self.bear.notes), ["note-1"])
        self.assertIn("second draft", self.bear.notes["note-1"]["content"])
        self.assertEqual(self.bear.notes["note-1"]["content"].count("[todo.pdf]"), 1)

    def test_same_text_but_new_pdf_refreshes_the_attachment(self):
        self.sync("text")
        self.assertEqual(self.sync("text", pdf_hash="hash-2"), "notebook changed")
        self.assertIsNone(self.sync("text", pdf_hash="hash-2", attach_pdf=True))

    def test_edit_made_in_bear_is_reverted(self):
        self.sync("text")
        self.bear.notes["note-1"]["content"] += "my own addition\n"
        self.assertEqual(self.sync("text"), "note was edited in Bear")
        self.assertNotIn("my own addition", self.bear.notes["note-1"]["content"])

    def test_trashed_note_is_restored_not_duplicated(self):
        self.sync("text")
        self.bear.trash("note-1")
        self.assertEqual(self.sync("text"), "restored from Bear's trash")
        self.assertEqual(self.bear.read("note-1")[1], "notes")
        self.assertEqual(list(self.bear.notes), ["note-1"])

    def test_deleted_note_is_recreated(self):
        self.sync("text")
        del self.bear.notes["note-1"]
        self.assertEqual(self.sync("text"), "created")

    def test_lost_state_adopts_the_existing_note(self):
        self.sync("text")
        self.entry.clear()
        self.assertEqual(self.sync("text"), "adopted existing note")
        self.assertEqual(list(self.bear.notes), ["note-1"])
        self.assertIsNone(self.sync("text"))

    def test_force_rewrites_a_matching_note(self):
        self.sync("text")
        self.assertEqual(self.sync("text", force_resync=True), "forced")


class SyncTodosTests(unittest.TestCase):
    def setUp(self):
        self.bear = FakeBear()
        self.state = {}
        self.tasks = notes_sync.BearTasks(self.bear, self.state, Settings(todo_sync=True))
        self.entry = {"noteId": "source-note"}

    def sync(self, *lines):
        return notes_sync.sync_todos(self.tasks, [page_of(*lines)], "todo", self.entry)

    def inbox(self):
        return self.bear.notes[self.state["tasksNoteId"]]["content"]

    def test_a_task_is_added_once(self):
        self.assertEqual(self.sync("TODO: Wash the car"), 1)
        self.assertEqual(self.sync("TODO: Wash the car"), 0)
        self.assertEqual(self.inbox().count("- [ ] Wash the car"), 1)
        self.assertIn("(bear://x-callback-url/open-note?id=source-note)", self.inbox())

    def test_ticked_or_deleted_tasks_do_not_come_back(self):
        self.sync("TODO: Wash the car")
        note = self.bear.notes[self.state["tasksNoteId"]]
        note["content"] = note["content"].replace("- [ ] Wash", "- [x] Wash")
        self.assertEqual(self.sync("TODO: Wash the car"), 0)
        self.assertNotIn("- [ ] Wash the car", self.inbox())

    def test_new_task_joins_existing_ones(self):
        self.sync("TODO: Wash the car")
        self.assertEqual(self.sync("TODO: Wash the car", "TODO: Buy milk"), 1)

    def test_same_handwriting_read_differently_is_not_a_new_task(self):
        self.sync("TODO: Wash the car today")
        self.assertEqual(self.sync("TODO: Wash the cor today"), 0)
        self.assertEqual(self.sync("TODO: Wash the car today"), 0)

    def test_similar_tasks_side_by_side_are_both_kept(self):
        self.assertEqual(self.sync("TODO: Book flight 1", "TODO: Book flight 2"), 2)

    def test_task_written_twice_is_added_once(self):
        self.assertEqual(self.sync("TODO: Wash the car", "TODO: wash the car!"), 1)

    def test_lost_state_does_not_duplicate_tasks(self):
        self.sync("TODO: Wash the car")
        self.entry.pop("todos")
        self.state.clear()
        self.assertEqual(self.sync("TODO: Wash the car"), 0)
        self.assertEqual(self.inbox().count("Wash the car"), 1)
        self.assertEqual(len(self.bear.notes), 1)

    def test_trashed_tasks_note_is_restored(self):
        self.sync("TODO: Wash the car")
        self.bear.trash(self.state["tasksNoteId"])
        self.sync("TODO: Wash the car", "TODO: Buy milk")
        self.assertEqual(self.bear.read(self.state["tasksNoteId"])[1], "notes")
        self.assertIn("Buy milk", self.inbox())


class TreeTests(unittest.TestCase):
    def test_flatten_and_exclude(self):
        tree = {
            "f1": {"type": "folder", "name": "Personal", "path": "Personal", "items": {
                "n1": {"type": "notebook", "name": "Diary", "path": os.path.join("Personal", "Diary"), "items": {}}}},
            "n2": {"type": "notebook", "name": "todo", "path": "todo", "items": {}},
        }
        notebooks = notes_sync.flatten_notebooks(tree)
        self.assertEqual(sorted(notebook.id for notebook in notebooks), ["n1", "n2"])
        excluded = [notebook.id for notebook in notebooks if notes_sync.is_excluded(notebook, ["personal/*"])]
        self.assertEqual(excluded, ["n1"])


if __name__ == "__main__":
    unittest.main()
