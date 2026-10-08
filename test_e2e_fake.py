"""End-to-end test of sync() against an in-memory fake Todoist.

python3 -m unittest -v test_e2e_fake
"""
import io
import json
import time
import contextlib
import tempfile
import unittest
from pathlib import Path

import todoist_sync as ts
from test_todoist_sync import SAMPLE


class FakeTodoist:
    def __init__(self):
        self.projects, self.sections, self.items = {}, {}, {}
        self.n = 0

    def _id(self):
        self.n += 1
        return f"id{self.n}"

    def full_sync(self):
        return {
            "projects": list(self.projects.values()),
            "sections": list(self.sections.values()),
            "items": [i for i in self.items.values() if not i["checked"] and not i["is_deleted"]],
        }

    def get_task(self, tid):
        it = self.items.get(tid)
        return None if it is None or it["is_deleted"] else dict(it)

    def run(self, cmds):
        status, mapping = {}, {}
        for c in cmds:
            a = {k: mapping.get(v, v) if isinstance(v, str) else v for k, v in c["args"].items()}
            t = c["type"]
            if t == "project_add":
                pid = self._id()
                self.projects[pid] = {"id": pid, "name": a["name"]}
                mapping[c["temp_id"]] = pid
            elif t == "section_add":
                sid = self._id()
                self.sections[sid] = {"id": sid, "name": a["name"], "project_id": a["project_id"]}
                mapping[c["temp_id"]] = sid
            elif t == "item_add":
                iid = self._id()
                self.items[iid] = {"id": iid, "content": a["content"], "description": a.get("description", ""),
                                   "project_id": a["project_id"], "section_id": a.get("section_id"),
                                   "checked": False, "is_deleted": False}
                mapping[c["temp_id"]] = iid
            elif t == "item_update":
                it = self.items[a["id"]]
                for k in ("content", "description", "due"):
                    if k in a:
                        it[k] = a[k]
            elif t == "item_move":
                self.items[a["id"]]["section_id"] = a["section_id"]
            elif t == "item_close":
                self.items[a["id"]]["checked"] = True
            elif t == "item_uncomplete":
                self.items[a["id"]]["checked"] = False
            status[c["uuid"]] = "ok"
        return status, mapping


class E2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.md = self.tmp / "TASKS.md"
        self.md.write_text(SAMPLE)
        self.fake = FakeTodoist()
        ts.Todoist = lambda token: self.fake
        ts.get_token = lambda: "x"

    def sync(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = ts.sync(self.md, "SCAYLE", self.tmp / "state", dry_run=False)
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue()

    def test_full_cycle(self):
        self.sync()
        self.assertEqual([p["name"] for p in self.fake.projects.values()], ["SCAYLE"])
        self.assertEqual(sorted(s["name"] for s in self.fake.sections.values()), ["Active", "Someday", "Waiting On"])
        self.assertEqual(len(self.fake.items), 3)
        text = self.md.read_text()
        self.assertEqual(text.count("%%td:id"), 3)
        joy = next(i for i in self.fake.items.values() if i["content"].startswith("Ask Mrdjan"))
        self.assertEqual(joy["due"], {"string": "6 Oct"})

        # second run is a no-op
        log = self.sync()
        self.assertNotIn("pushed", log)
        self.assertEqual(self.md.read_text(), text)

        # phone: complete Datadog, add a new task
        dd = next(i for i in self.fake.items.values() if i["content"] == "Datadog access")
        dd["checked"] = True
        active = next(s["id"] for s in self.fake.sections.values() if s["name"] == "Active")
        self.fake.items["p1"] = {"id": "p1", "content": "From phone", "description": "", "project_id": joy["project_id"],
                                 "section_id": active, "checked": False, "is_deleted": False}
        # local: rename Buddy check-in
        self.md.write_text(self.md.read_text().replace("**Buddy check-in with Alex**", "**Buddy check-in w/ Alex**"))
        self.sync()
        text = self.md.read_text()
        self.assertIn("- [ ] **From phone** %%td:p1%%", text)
        self.assertRegex(text, r"## Done\n- \[x\] \*\*Datadog access\*\*")
        self.assertTrue(any(i["content"] == "Buddy check-in w/ Alex" for i in self.fake.items.values()))
        self.assertEqual(len(self.fake.items), 4)

        # converged
        before = self.md.read_text()
        log = self.sync()
        self.assertNotIn("pushed", log)
        self.assertEqual(self.md.read_text(), before)

    def test_aborted_local_write_recovers_ids(self):
        real_write = ts.atomic_write

        def boom(path, text):
            if path == self.md:
                raise RuntimeError("simulated crash")
            real_write(path, text)

        ts.atomic_write = boom
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    ts.sync(self.md, "SCAYLE", self.tmp / "state", dry_run=False)
        finally:
            ts.atomic_write = real_write
        self.assertEqual(len(self.fake.items), 3)
        self.sync()
        self.assertEqual(len(self.fake.items), 3)  # no duplicates
        self.assertEqual(self.md.read_text().count("%%td:id"), 3)


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "Claude").mkdir()
        self.scan = self.tmp / "Meeting Summaries"
        self.scan.mkdir()
        self.md = self.tmp / "Claude" / "TASKS.md"
        self.md.write_text(SAMPLE)
        self.state = self.tmp / "state"
        self.state.mkdir()

    def imp(self, dry=False):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            ts.import_new_tasks(self.md, self.scan, self.state, dry)
        return out.getvalue()

    def test_imports_open_tasks_from_new_files_once(self):
        (self.scan / "m1.md").write_text(
            "# M\n## Tasks for Tim\n- [ ] **Invite Nico** - send invite (from [[m1]])\n"
            "- [x] **Already done** - x\n- [ ] **Datadog access** - duplicate title\n"
            "  - [ ] **nested** - ignored\n")
        self.imp()
        text = self.md.read_text()
        self.assertIn("- [ ] **Invite Nico** - send invite (from [[m1]])\n\n## Waiting On", text)
        self.assertNotIn("Already done", text)
        self.assertEqual(text.count("Datadog access"), 1)
        self.assertNotIn("nested", text)
        # second run: file is no longer new
        self.imp()
        self.assertEqual(self.md.read_text(), text)

    def test_old_files_ignored_and_known_titles_skipped(self):
        (self.scan / "old.md").write_text("- [ ] **Old task**\n")
        ts.save_state(self.state / "state.json", {"tasks": {"1": {"content": "Synced before", "description": "",
                      "section": "Active", "done": True}}, "pending": {}, "last_scan": time.time() + 5})
        self.imp()
        self.assertNotIn("Old task", self.md.read_text())
        st = json.loads((self.state / "state.json").read_text())
        st["last_scan"] = time.time() - 60
        (self.state / "state.json").write_text(json.dumps(st))
        (self.scan / "new.md").write_text("- [ ] **Synced before** - done already\n- [ ] **Fresh**\n")
        self.imp()
        text = self.md.read_text()
        self.assertNotIn("Synced before", text)
        self.assertIn("**Fresh**", text)

    def test_group_tasks_skipped_unless_tim_named(self):
        (self.scan / "g.md").write_text(
            "- [ ] **Automate KPIs** - group task: metrics from Datadog\n"
            "- [ ] **Invite Nico** - group action: contact Nico\n"
            "- [ ] **Client pages** - group task: Tim and Alex compile pages\n"
            "- [ ] **Solo** - just me\n")
        out = self.imp()
        text = self.md.read_text()
        self.assertNotIn("Automate KPIs", text)
        self.assertNotIn("Invite Nico", text)
        self.assertIn("**Client pages**", text)
        self.assertIn("**Solo**", text)
        self.assertEqual(out.count("skipped group task"), 2)

    def test_dry_run_writes_nothing(self):
        (self.scan / "m.md").write_text("- [ ] **X**\n")
        out = self.imp(dry=True)
        self.assertIn("would import", out)
        self.assertEqual(self.md.read_text(), SAMPLE)
        self.assertFalse((self.state / "state.json").exists())


if __name__ == "__main__":
    unittest.main()
