"""Offline tests for the daily-note checkbox sync: python3 -m unittest -v test_daily_notes"""
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import todoist_sync as ts

TODAY = dt.date(2026, 10, 19)

TASKS = """# Tasks

## Active
- [ ] **Alpha** - first %%td:A1%%
- [ ] **Beta** - second %%td:B1%%
- [ ] **Gamma** %%td:G1%%

## Done
- [x] **Old** %%td:O1%%
"""


def note(active_lines, extra=""):
    return (
        "# Monday\n\n## Needs attention\n- [x] **Alpha** — not a task list\n\n"
        "## Open tasks\n### Active\n" + "\n".join(active_lines) + "\n\n### Waiting On\n- —\n\n"
        "## Suggested new tasks\n- [x] **Gamma** - suggestion with same title\n- [x] **Brand new**\n" + extra +
        "\n## Notes\n"
    )


class DailyNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Claude").mkdir()
        self.md = self.tmp / "Claude" / "TASKS.md"
        self.md.write_text(TASKS)
        self.state = self.tmp / "state"
        self.state.mkdir()

    def write_note(self, date, lines):
        p = self.tmp / f"{date.isoformat()}.md"
        p.write_text(note(lines))
        return p

    def ticks(self, today=TODAY):
        ts.apply_daily_ticks(self.md, self.tmp, self.state, False, today)

    def reflect(self, today=TODAY):
        ts.reflect_to_daily(self.md, self.tmp, self.state, False, today)

    def done_titles(self):
        doc = ts.parse_doc(self.md.read_text(), {})
        return {t.task.content for t in doc.tasks if t.task.done}

    def test_parses_only_open_tasks_block(self):
        lines = ts.daily_task_lines(note(["- [ ] **Alpha** - x", "- [x] **Beta**"]))
        self.assertEqual([(c, t) for _, c, t in lines], [(False, "alpha"), (True, "beta")])

    def test_tick_marks_done_and_keeps_id(self):
        self.write_note(TODAY, ["- [ ] **Alpha** - first", "- [x] **Beta** - second"])
        self.ticks()
        self.assertEqual(self.done_titles(), {"Old", "Beta"})
        self.assertIn("- [x] **Beta** - second %%td:B1%%", self.md.read_text())
        self.assertLess(self.md.read_text().index("## Done"), self.md.read_text().index("**Beta**"))

    def test_annotated_line_matches_by_title(self):
        self.write_note(TODAY, ["- [x] **Alpha** - first — meeting is today 11:45"])
        self.ticks()
        self.assertIn("Alpha", self.done_titles())

    def test_needs_attention_ignored(self):
        self.write_note(TODAY, ["- [ ] **Alpha** - first"])
        self.ticks()
        self.assertEqual(self.done_titles(), {"Old"})

    def open_titles(self):
        doc = ts.parse_doc(self.md.read_text(), {})
        return {t.task.content: t.task for t in doc.tasks if not t.task.done}

    def test_ticked_suggestion_added_as_open_task(self):
        self.write_note(TODAY, ["- [ ] **Alpha** - first"])
        self.ticks()
        tasks = self.open_titles()
        self.assertIn("Brand new", tasks)
        self.assertNotIn("Gamma", self.done_titles())  # same-title suggestion: already known, not added again
        self.assertEqual(self.md.read_text().count("**Gamma**"), 1)
        text = self.md.read_text()
        self.assertLess(text.index("**Brand new**"), text.index("## Done"))

    def test_suggestion_added_once(self):
        self.write_note(TODAY, ["- [ ] **Alpha** - first"])
        self.write_note(TODAY - dt.timedelta(days=1), ["- [ ] **Alpha** - first"])
        self.ticks()
        self.ticks()
        self.assertEqual(self.md.read_text().count("**Brand new**"), 1)

    def test_suggestion_not_readded_after_deletion(self):
        self.write_note(TODAY, ["- [ ] **Alpha** - first"])
        self.ticks()
        self.md.write_text(TASKS)  # user deleted it again
        self.ticks()
        self.assertNotIn("Brand new", self.md.read_text())

    def test_parse_plain_suggestion(self):
        self.assertEqual(
            ts.parse_suggestion("Skim the tickets from Friday — AM-1 x, AM-2 y ([AM-1](u)) · [[fielmann]]"),
            ("Skim the tickets from Friday", "AM-1 x, AM-2 y ([AM-1](u)) · [[fielmann]]"))
        self.assertEqual(
            ts.parse_suggestion("Read the \"BF 2026: Prep\" mail sent on Friday ([open](u))"),
            ("Read the \"BF 2026: Prep\" mail sent on Friday", "([open](u))"))
        self.assertEqual(ts.parse_suggestion("Complete the training by 23 Oct"),
                         ("Complete the training by 23 Oct", ""))

    def test_stale_tick_does_not_reclose_reopened_task(self):
        self.write_note(TODAY, ["- [x] **Alpha** - first"])
        self.ticks()
        self.assertIn("Alpha", self.done_titles())
        # reopened in Todoist -> sync writes it back as open
        self.md.write_text(TASKS)
        self.ticks()
        self.assertNotIn("Alpha", self.done_titles())

    def test_untick_never_reopens(self):
        self.md.write_text(TASKS.replace("- [ ] **Alpha**", "- [x] **Alpha**"))
        self.write_note(TODAY, ["- [ ] **Alpha** - first"])
        self.ticks()
        self.assertIn("Alpha", self.done_titles())

    def test_notes_outside_window_ignored(self):
        self.write_note(TODAY - dt.timedelta(days=30), ["- [x] **Alpha** - first"])
        self.ticks()
        self.assertNotIn("Alpha", self.done_titles())

    def test_ambiguous_title_ignored(self):
        self.md.write_text(TASKS.replace("- [ ] **Gamma** %%td:G1%%", "- [ ] **Gamma** %%td:G1%%\n- [ ] **gamma** - dup"))
        self.write_note(TODAY, ["- [x] **Gamma**"])
        self.ticks()
        self.assertNotIn("Gamma", self.done_titles())

    def test_reflect_ticks_task_done_elsewhere(self):
        p = self.write_note(TODAY, ["- [ ] **Alpha** - first", "- [ ] **Beta** - second"])
        self.md.write_text(TASKS.replace("- [ ] **Beta**", "- [x] **Beta**"))
        self.ticks()
        self.reflect()
        text = p.read_text()
        self.assertIn("- [x] **Beta** - second", text)
        self.assertIn("- [ ] **Alpha** - first", text)
        self.assertIn("- [x] **Brand new**", text)  # suggestions are never toggled
        self.assertTrue(json.loads((self.state / "state.json").read_text())["daily"][p.name]["beta"])

    def test_reflect_only_writes_today(self):
        old = self.write_note(TODAY - dt.timedelta(days=1), ["- [ ] **Beta** - second"])
        self.md.write_text(TASKS.replace("- [ ] **Beta**", "- [x] **Beta**"))
        self.reflect()
        self.assertIn("- [ ] **Beta**", old.read_text())

    def test_reflect_unticks_only_processed_ticks(self):
        p = self.write_note(TODAY, ["- [x] **Alpha** - first"])
        # not yet processed: the user's tick must survive even though TASKS.md says open
        self.reflect()
        self.assertIn("- [x] **Alpha**", p.read_text())
        self.ticks()                      # processed -> Alpha done
        self.md.write_text(TASKS)         # reopened in Todoist
        self.reflect()
        self.assertIn("- [ ] **Alpha**", p.read_text())
        self.ticks()                      # and it stays open
        self.assertNotIn("Alpha", self.done_titles())

    def test_dry_run_writes_nothing(self):
        p = self.write_note(TODAY, ["- [x] **Alpha** - first"])
        before = p.read_text()
        ts.apply_daily_ticks(self.md, self.tmp, self.state, True, TODAY)
        ts.reflect_to_daily(self.md, self.tmp, self.state, True, TODAY)
        self.assertEqual(self.md.read_text(), TASKS)
        self.assertEqual(p.read_text(), before)
        self.assertFalse((self.state / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
