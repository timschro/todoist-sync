"""Offline tests: python3 -m unittest -v test_todoist_sync"""
import unittest

import todoist_sync as ts
from todoist_sync import Task

SAMPLE = """# Tasks

## Active
- [ ] **Ask Mrdjan about JoyConf plans** - is SCAYLE attending? reply to Stefan (Turbine Kreuzberg), due Mon 6 Oct
- [ ] **Buddy check-in with Alex** - Mon 5 Oct 15:00

## Waiting On
- [ ] **Datadog access** - ITHELP-7764, In Testing (since 2 Oct)

## Someday

## Done
"""

SECTIONS = {"active": "s1", "waiting on": "s2", "someday": "s3"}


def sid(name):
    return SECTIONS.get(name.lower())


def run(text, remote=None, base=None, fetched=None):
    base = base or {}
    doc = ts.parse_doc(text, base)
    plan = ts.reconcile(doc, remote or {}, lambda tid: (fetched or {}).get(tid), base, "P", sid)
    return doc, plan, ts.render_doc(doc, plan.decisions, plan.appends)


def types(plan):
    return [c["cmd"]["type"] for c in plan.commands]


class ParseTests(unittest.TestCase):
    def test_parse_sample(self):
        doc = ts.parse_doc(SAMPLE, {})
        self.assertEqual(len(doc.tasks), 3)
        t = doc.tasks[0].task
        self.assertEqual(t.content, "Ask Mrdjan about JoyConf plans")
        self.assertTrue(t.description.startswith("is SCAYLE attending?"))
        self.assertEqual(t.section, "Active")
        self.assertEqual(doc.tasks[2].task.section, "Waiting On")

    def test_marker_and_done(self):
        doc = ts.parse_doc("## Done\n- [x] **A** - b %%td:abc%%\n", {"abc": Task("A", "b", "Waiting On", True)})
        lt = doc.tasks[0]
        self.assertEqual(lt.id, "abc")
        self.assertTrue(lt.task.done)
        self.assertEqual(lt.task.section, "Waiting On")

    def test_due_extraction(self):
        self.assertEqual(ts.extract_due("reply to Stefan, due Mon 6 Oct"), "6 Oct")
        self.assertEqual(ts.extract_due("questions, due Fri 9 Oct 14:00"), "9 Oct 14:00")
        self.assertIsNone(ts.extract_due("Mon 5 Oct 15:00"))


class ReconcileTests(unittest.TestCase):
    def test_initial_push_creates_all(self):
        _, plan, out = run(SAMPLE)
        self.assertEqual(types(plan).count("item_add"), 3)
        self.assertEqual(types(plan).count("item_update"), 1)  # due on JoyConf only
        adds = [c["cmd"]["args"] for c in plan.commands if c["cmd"]["type"] == "item_add"]
        self.assertEqual(adds[2]["section_id"], "s2")
        self.assertIn("%%td:tmp-", out)

    def test_idempotent(self):
        text = "## Active\n- [ ] **A** - x %%td:1%%\n\n## Done\n"
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, out = run(text, {"1": base["1"]}, base)
        self.assertEqual(plan.commands, [])
        self.assertEqual(out, text)

    def test_remote_completion_moves_line_to_done(self):
        text = "## Active\n- [ ] **A** - x %%td:1%%\n\n## Done\n"
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, out = run(text, {}, base, fetched={"1": Task("A", "x", "Active", True)})
        self.assertEqual(plan.commands, [])
        self.assertEqual(out, "## Active\n\n## Done\n- [x] **A** - x %%td:1%%\n")

    def test_local_completion_closes_in_todoist(self):
        text = "## Active\n- [x] **A** - x %%td:1%%\n\n## Done\n"
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, out = run(text, {"1": base["1"]}, base)
        self.assertEqual(types(plan), ["item_close"])
        self.assertIn("## Done\n- [x] **A**", out)

    def test_local_edit_pushed(self):
        text = "## Active\n- [ ] **A2** - x %%td:1%%\n"
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, _ = run(text, {"1": base["1"]}, base)
        self.assertEqual(types(plan), ["item_update"])
        self.assertEqual(plan.commands[0]["cmd"]["args"]["content"], "A2")

    def test_local_section_move(self):
        text = "## Active\n\n## Waiting On\n- [ ] **A** - x %%td:1%%\n"
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, _ = run(text, {"1": base["1"]}, base)
        self.assertEqual(types(plan), ["item_move"])
        self.assertEqual(plan.commands[0]["cmd"]["args"]["section_id"], "s2")

    def test_conflict_todoist_wins(self):
        text = "## Active\n- [ ] **Local title** - x %%td:1%%\n"
        base = {"1": Task("A", "x", "Active", False)}
        remote = {"1": Task("Remote title", "x", "Active", False)}
        _, plan, out = run(text, remote, base)
        self.assertEqual(plan.commands, [])
        self.assertIn("**Remote title**", out)
        self.assertTrue(any("conflict" in n for n in plan.notes))

    def test_field_wise_merge(self):
        text = "## Active\n- [ ] **A** - local desc %%td:1%%\n"
        base = {"1": Task("A", "x", "Active", False)}
        remote = {"1": Task("Remote", "x", "Active", False)}
        _, plan, out = run(text, remote, base)
        self.assertEqual(plan.commands[0]["cmd"]["args"], {"id": "1", "description": "local desc"})
        self.assertIn("**Remote** - local desc", out)

    def test_local_done_loses_if_todoist_changed(self):
        text = "## Active\n- [x] **A** - x %%td:1%%\n"
        base = {"1": Task("A", "x", "Active", False)}
        remote = {"1": Task("A renamed", "x", "Active", False)}
        _, plan, out = run(text, remote, base)
        self.assertEqual(plan.commands, [])
        self.assertIn("- [ ] **A renamed**", out)

    def test_local_delete_closes(self):
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, _ = run("## Active\n", {"1": base["1"]}, base)
        self.assertEqual(types(plan), ["item_close"])
        self.assertNotIn("1", plan.new_base)

    def test_local_delete_but_remote_changed_restores(self):
        base = {"1": Task("A", "x", "Active", False)}
        _, plan, out = run("## Active\n", {"1": Task("A", "y", "Active", False)}, base)
        self.assertEqual(plan.commands, [])
        self.assertIn("- [ ] **A** - y %%td:1%%", out)

    def test_remote_delete_removes_even_if_edited_locally(self):
        text = "## Active\n- [ ] **edited** %%td:1%%\n"
        base = {"1": Task("A", "", "Active", False)}
        _, plan, out = run(text, {}, base, fetched={})
        self.assertEqual(out, "## Active\n")

    def test_new_remote_task_appended_and_section_created(self):
        _, plan, out = run(SAMPLE, {"9": Task("From phone", "", "Errands", False)}, {"x": Task("z", "", "Active", True)})
        self.assertIn("## Errands\n- [ ] **From phone** %%td:9%%\n\n## Done", out)

    def test_remote_reopen(self):
        text = "## Active\n\n## Done\n- [x] **A** %%td:1%%\n"
        base = {"1": Task("A", "", "Active", True)}
        _, plan, out = run(text, {"1": Task("A", "", "Active", False)}, base)
        self.assertEqual(out, "## Active\n- [ ] **A** %%td:1%%\n\n## Done\n")

    def test_done_without_id_not_pushed(self):
        _, plan, _ = run("## Done\n- [x] **old** \n")
        self.assertEqual(plan.commands, [])


if __name__ == "__main__":
    unittest.main()
