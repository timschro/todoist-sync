#!/usr/bin/env python3
"""Two-way sync between an Obsidian TASKS.md file and a Todoist project.

Mapping
  file                        -> Todoist project (default "SCAYLE")
  "## <Heading>" (not Done)   -> section of the same name
  "## Done" / "- [x]"         -> completed task
  "**Title** - details"       -> content "Title", description "details"
  "due <when>" in details     -> due date (sent once, on creation)

Each synced line carries a hidden Obsidian comment "%%td:<todoist id>%%".
A snapshot of the last synced state enables a 3-way merge. On conflicts
(both sides changed since the last sync) Todoist wins.

Stdlib only; runs on the macOS system python3 (3.9+).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_FILE = (
    Path.home()
    / "Library/CloudStorage/GoogleDrive-tim.schroeder@scayle.com/My Drive/Obsidian/Scayle/Claude/TASKS.md"
)
DEFAULT_PROJECT = "SCAYLE"
DEFAULT_SECTION = "Active"
DONE_HEADING = "Done"
STATE_DIR = Path.home() / ".local/state/todoist-sync"
API = "https://api.todoist.com/api/v1"
KEYCHAIN_SERVICE = "todoist-sync"

TASK_RE = re.compile(
    r"^- \[(?P<check>[ xX])\] (?P<body>.*?)(?:\s*%%td:(?P<id>[A-Za-z0-9_-]+)%%)?\s*$"
)
HEADING_RE = re.compile(r"^##\s+(?P<name>.+?)\s*$")
BOLD_RE = re.compile(r"^\*\*(?P<title>.+?)\*\*(?P<rest>.*)$")
DUE_RE = re.compile(r"\bdue\s+(?P<due>[^,;()]+)", re.I)
GROUP_RE = re.compile(r"\bgroup\s+(?:task|action)s?\b", re.I)
ME_RE = re.compile(r"\bTim(?:'s)?\b|\bSchr(?:ö|oe|o)der\b")
WEEKDAY_RE = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+", re.I)

FIELDS = ("content", "description", "section", "done")


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def norm(s: Optional[str]) -> str:
    return " ".join((s or "").split())


# --------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Task:
    content: str
    description: str
    section: str
    done: bool

    def key(self) -> tuple:
        return (norm(self.content), norm(self.description), self.section.lower(), self.done)

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(d["content"], d.get("description", ""), d.get("section", DEFAULT_SECTION), bool(d.get("done")))


def eq(f: str, a, b) -> bool:
    if f in ("content", "description"):
        return norm(a) == norm(b)
    if f == "section":
        return a.lower() == b.lower()
    return a == b


def body_hash(t: Task) -> str:
    return hashlib.sha1(f"{norm(t.content)}\n{norm(t.description)}".encode()).hexdigest()


# --------------------------------------------------------------------------- markdown


@dataclass
class LocalTask:
    line_no: int
    id: Optional[str]
    task: Task
    body: str  # original body text, without checkbox and marker
    marker: Optional[str] = None  # id actually present on the line


@dataclass
class Doc:
    lines: List[str]
    tasks: List[LocalTask]
    headings: List[str]


def parse_body(body: str) -> Tuple[str, str]:
    body = body.strip()
    m = BOLD_RE.match(body)
    if m:
        rest = re.sub(r"^[-–—:]\s*", "", m["rest"].strip())
        return m["title"].strip(), rest
    return body, ""


def is_done_heading(name: Optional[str]) -> bool:
    return name is not None and name.lower() == DONE_HEADING.lower()


def parse_doc(text: str, base: Dict[str, Task]) -> Doc:
    lines = text.split("\n")
    section: Optional[str] = None
    tasks: List[LocalTask] = []
    headings: List[str] = []
    for i, line in enumerate(lines):
        h = HEADING_RE.match(line)
        if h:
            section = h["name"]
            headings.append(section)
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        content, desc = parse_body(m["body"])
        if not content:
            continue
        tid = m["id"]
        in_done = is_done_heading(section)
        done = in_done or m["check"] in "xX"
        if in_done or section is None:
            sec = base[tid].section if tid and tid in base else DEFAULT_SECTION
        else:
            sec = section
        tasks.append(LocalTask(i, tid, Task(content, desc, sec, done), m["body"].strip(), tid))
    return Doc(lines, tasks, headings)


def render_line(tid: Optional[str], t: Task, body: Optional[str]) -> str:
    if body is None:
        body = f"**{t.content}**" + (f" - {norm(t.description)}" if norm(t.description) else "")
    marker = f" %%td:{tid}%%" if tid else ""
    return f"- [{'x' if t.done else ' '}] {body}{marker}"


@dataclass
class Decision:
    tid: Optional[str]
    task: Task
    body: Optional[str]  # keep original body text if not None
    raw: Optional[str] = None  # keep the original line verbatim if not None


def render_doc(doc: Doc, decisions: Dict[int, Optional[Decision]], appends: List[Tuple[str, Task]]) -> str:
    blocks: List[List] = [[None, []]]  # [heading name | None, lines]
    moved: List[Decision] = []

    def target(t: Task) -> str:
        return DONE_HEADING if t.done else t.section

    def placed_ok(block_name: Optional[str], t: Task) -> bool:
        if block_name is None:
            return not t.done and t.section.lower() == DEFAULT_SECTION.lower()
        return block_name.lower() == target(t).lower()

    for i, line in enumerate(doc.lines):
        h = HEADING_RE.match(line)
        if h:
            blocks.append([h["name"], [line]])
            continue
        if i not in decisions:
            blocks[-1][1].append(line)
            continue
        d = decisions[i]
        if d is None:
            continue  # removed
        if placed_ok(blocks[-1][0], d.task):
            blocks[-1][1].append(d.raw if d.raw is not None else render_line(d.tid, d.task, d.body))
        else:
            moved.append(d)

    for tid, t in appends:
        moved.append(Decision(tid, t, None))

    for d in moved:
        name = target(d.task)
        blk = next((b for b in blocks if b[0] is not None and b[0].lower() == name.lower()), None)
        if blk is None:
            blk = [name, [f"## {name}", ""]]
            done_idx = next((k for k, b in enumerate(blocks) if is_done_heading(b[0])), None)
            pos = done_idx if (done_idx is not None and not is_done_heading(name)) else len(blocks)
            prev = blocks[pos - 1][1]
            if prev and prev[-1].strip() != "":
                prev.append("")
            blocks.insert(pos, blk)
        lines = blk[1]
        k = len(lines)
        while k > 1 and lines[k - 1].strip() == "":
            k -= 1
        lines.insert(k, render_line(d.tid, d.task, d.body))

    out: List[str] = []
    for _, lines in blocks:
        out.extend(lines)
    text = "\n".join(out)
    if not text.endswith("\n"):
        text += "\n"
    return text


# --------------------------------------------------------------------------- merge


def merge(B: Task, L: Task, R: Task) -> Tuple[Task, List[str]]:
    """3-way merge. Remote (Todoist) wins any conflict.

    content/description/section are merged field by field. The done/open status
    is special: a local status change only wins if Todoist did not change the
    task at all since the last sync.
    """
    out = {}
    conflicts: List[str] = []
    remote_changed_any = any(not eq(f, getattr(R, f), getattr(B, f)) for f in FIELDS)
    for f in FIELDS:
        b, l, r = getattr(B, f), getattr(L, f), getattr(R, f)
        rc, lc = not eq(f, r, b), not eq(f, l, b)
        if f == "done" and lc and not rc and remote_changed_any:
            out[f] = r
            conflicts.append(f"done: local={l} discarded, Todoist={r} (task changed in Todoist)")
        elif rc:
            out[f] = r
            if lc and not eq(f, l, r):
                conflicts.append(f"{f}: local={l!r} discarded, Todoist={r!r}")
        elif lc:
            out[f] = l
        else:
            out[f] = r
    return Task(**out), conflicts


@dataclass
class Plan:
    commands: List[dict] = field(default_factory=list)  # {"cmd": {...}, "tid": str, "optional": bool}
    decisions: Dict[int, Optional[Decision]] = field(default_factory=dict)
    appends: List[Tuple[str, Task]] = field(default_factory=list)
    new_base: Dict[str, Task] = field(default_factory=dict)
    local_changed: set = field(default_factory=set)
    notes: List[str] = field(default_factory=list)


def _cmd(type_: str, args: dict, tid: str, optional: bool = False, temp_id: Optional[str] = None) -> dict:
    c = {"type": type_, "uuid": str(uuid.uuid4()), "args": args}
    if temp_id:
        c["temp_id"] = temp_id
    return {"cmd": c, "tid": tid, "optional": optional}


def extract_due(description: str) -> Optional[str]:
    m = DUE_RE.search(description or "")
    if not m:
        return None
    return WEEKDAY_RE.sub("", m["due"].strip()) or None


def reconcile(
    doc: Doc,
    remote_active: Dict[str, Task],
    fetch: Callable[[str], Optional[Task]],
    base: Dict[str, Task],
    project_id: str,
    section_id: Callable[[str], Optional[str]],
) -> Plan:
    p = Plan()

    def push(tid: str, R: Task, M: Task) -> None:
        if R.done and not M.done:
            p.commands.append(_cmd("item_uncomplete", {"id": tid}, tid))
        args = {}
        if not eq("content", M.content, R.content):
            args["content"] = M.content
        if not eq("description", M.description, R.description):
            args["description"] = M.description
        if args:
            p.commands.append(_cmd("item_update", {"id": tid, **args}, tid))
        if not eq("section", M.section, R.section):
            p.commands.append(_cmd("item_move", {"id": tid, "section_id": section_id(M.section)}, tid))
        if M.done and not R.done:
            p.commands.append(_cmd("item_close", {"id": tid}, tid))

    def set_local(L: LocalTask, tid: str, M: Task) -> None:
        same_text = eq("content", M.content, L.task.content) and eq("description", M.description, L.task.description)
        unchanged = M.key() == L.task.key() and tid == L.marker
        raw = doc.lines[L.line_no] if unchanged else None
        p.decisions[L.line_no] = Decision(tid, M, L.body if same_text else None, raw)
        if M.key() != L.task.key():
            p.local_changed.add(tid)

    local_by_id: Dict[str, LocalTask] = {}
    new_local: List[LocalTask] = []
    for lt in doc.tasks:
        if lt.id and lt.id not in local_by_id:
            local_by_id[lt.id] = lt
        else:
            if lt.id:
                p.notes.append(f"duplicate marker {lt.id} on line {lt.line_no + 1}; treating as new task")
                lt.id = None
            new_local.append(lt)

    remote: Dict[str, Optional[Task]] = dict(remote_active)
    for tid in (set(base) | set(local_by_id)) - set(remote_active):
        B, L = base.get(tid), local_by_id.get(tid)
        if B is not None and B.done and (L is None or L.task.done):
            remote[tid] = B  # completed on both sides already; assume unchanged in Todoist
        else:
            remote[tid] = fetch(tid)

    for tid in sorted(set(base) | set(local_by_id) | set(remote_active)):
        B, L, R = base.get(tid), local_by_id.get(tid), remote.get(tid)

        if B is None:
            if L and R:
                set_local(L, tid, R)  # no history: adopt Todoist
                p.new_base[tid] = R
            elif L:
                p.notes.append(f"{tid} unknown in Todoist; re-creating '{L.task.content}'")
                L.id = None
                new_local.append(L)
            elif R and not R.done:
                p.appends.append((tid, R))
                p.local_changed.add(tid)
                p.new_base[tid] = R
                p.notes.append(f"new from Todoist: '{R.content}' -> {R.section}")
            continue

        if R is None:
            if L:
                p.decisions[L.line_no] = None
                p.local_changed.add(tid)
            p.notes.append(f"deleted in Todoist: '{B.content}'")
            continue

        if L is None:
            if R.key() == B.key():
                if not R.done:
                    p.commands.append(_cmd("item_close", {"id": tid}, tid))
                    p.notes.append(f"removed locally -> closing in Todoist: '{R.content}'")
            else:
                p.appends.append((tid, R))
                p.local_changed.add(tid)
                p.new_base[tid] = R
                p.notes.append(f"removed locally but changed in Todoist -> restored: '{R.content}'")
            continue

        M, conflicts = merge(B, L.task, R)
        for c in conflicts:
            p.notes.append(f"conflict on '{M.content}' ({tid}): {c}")
        push(tid, R, M)
        set_local(L, tid, M)
        p.new_base[tid] = M

    for lt in new_local:
        if lt.task.done:
            continue  # never push tasks that are already done
        tmp = f"tmp-{uuid.uuid4()}"
        args = {"content": lt.task.content, "project_id": project_id, "section_id": section_id(lt.task.section)}
        if norm(lt.task.description):
            args["description"] = lt.task.description
        p.commands.append(_cmd("item_add", args, tmp, temp_id=tmp))
        due = extract_due(lt.task.description)
        if due:
            p.commands.append(_cmd("item_update", {"id": tmp, "due": {"string": due}}, tmp, optional=True))
        p.decisions[lt.line_no] = Decision(tmp, lt.task, lt.body)
        p.new_base[tmp] = lt.task
        p.notes.append(f"new local -> Todoist: '{lt.task.content}' ({lt.task.section})" + (f", due '{due}'" if due else ""))

    return p


# --------------------------------------------------------------------------- Todoist API


class NotFound(Exception):
    pass


class Todoist:
    def __init__(self, token: str):
        self.token = token

    def _req(self, method: str, path: str, data: Optional[dict] = None):
        headers = {"Authorization": f"Bearer {self.token}"}
        body = None
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        for attempt in range(4):
            req = urllib.request.Request(API + path, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    raw = r.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise NotFound(path)
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise RuntimeError(f"{method} {path}: HTTP {e.code} {e.read()[:300]!r}")
            except urllib.error.URLError:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise

    def full_sync(self) -> dict:
        return self._req(
            "POST", "/sync", {"sync_token": "*", "resource_types": json.dumps(["projects", "sections", "items"])}
        )

    def get_task(self, tid: str) -> Optional[dict]:
        try:
            return self._req("GET", f"/tasks/{urllib.parse.quote(tid)}")
        except NotFound:
            return None

    def run(self, cmds: List[dict]) -> Tuple[dict, dict]:
        """Execute sync commands in chunks; resolve temp ids across chunks."""
        status: dict = {}
        mapping: dict = {}
        for i in range(0, len(cmds), 90):
            chunk = json.loads(json.dumps(cmds[i : i + 90]))
            for c in chunk:
                for k, v in list(c["args"].items()):
                    if isinstance(v, str) and v in mapping:
                        c["args"][k] = mapping[v]
            r = self._req("POST", "/sync", {"commands": json.dumps(chunk)})
            status.update(r.get("sync_status", {}))
            mapping.update(r.get("temp_id_mapping", {}))
        return status, mapping


def get_token() -> str:
    tok = os.environ.get("TODOIST_API_TOKEN")
    if tok:
        return tok.strip()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        sys.exit(
            "No Todoist token. Store it with:\n"
            f'  security add-generic-password -s {KEYCHAIN_SERVICE} -a "$USER" -w <token>\n'
            "or set TODOIST_API_TOKEN."
        )


# --------------------------------------------------------------------------- state & io


def load_state(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"project_id": None, "tasks": {}, "pending": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- main


def to_task(item: dict, sec_names: Dict[str, str]) -> Task:
    return Task(
        item.get("content", ""),
        item.get("description") or "",
        sec_names.get(item.get("section_id") or "", DEFAULT_SECTION),
        bool(item.get("checked")),
    )


def file_created(path: Path) -> float:
    st = path.stat()
    return getattr(st, "st_birthtime", st.st_ctime)


def collect_new_tasks(scan_dir: Path, since: float, known_titles: set, skip: Path) -> List[Tuple[Path, Task]]:
    """Open top-level '- [ ]' tasks from .md files created after `since`, not already known by title."""
    found: List[Tuple[Path, Task]] = []
    if not scan_dir.is_dir():
        log(f"WARN scan dir not found: {scan_dir}")
        return found
    files = [
        p for p in scan_dir.rglob("*.md")
        if not any(part.startswith(".") for part in p.relative_to(scan_dir).parts) and p.resolve() != skip.resolve()
    ]
    for p in sorted((p for p in files if file_created(p) > since), key=file_created):
        for line in p.read_text(encoding="utf-8").split("\n"):
            m = TASK_RE.match(line)
            if not m or m["check"] != " ":
                continue
            content, desc = parse_body(m["body"])
            title = norm(content).lower()
            if not content or title in known_titles:
                continue
            if GROUP_RE.search(m["body"]) and not ME_RE.search(m["body"]):
                log(f"skipped group task (Tim not named) from '{p.name}': '{content}'")
                continue
            known_titles.add(title)
            found.append((p, Task(content, desc, DEFAULT_SECTION, False)))
    return found


def import_new_tasks(md_path: Path, scan_dir: Path, state_dir: Path, dry_run: bool) -> None:
    """Copy open tasks from newly created files into TASKS.md (## Active) before syncing."""
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    started = time.time()
    since = state.get("last_scan")
    if since is None:  # first run: include everything created today
        since = dt.datetime.combine(dt.date.today(), dt.time()).timestamp()

    base = {k: Task.from_dict(v) for k, v in state.get("tasks", {}).items()}
    text0 = md_path.read_text(encoding="utf-8")
    doc = parse_doc(text0, base)
    known = {norm(t.task.content).lower() for t in doc.tasks} | {norm(t.content).lower() for t in base.values()}

    new = collect_new_tasks(scan_dir, since, known, md_path)
    for p, t in new:
        log(f"{'[dry-run] would import' if dry_run else 'imported'} from '{p.name}': '{t.content}'")
    if dry_run:
        return
    if new:
        new_text = render_doc(doc, {}, [(None, t) for _, t in new])
        if md_path.read_text(encoding="utf-8") != text0:
            log("TASKS.md changed during import; will retry next run")
            return
        atomic_write(state_dir / "TASKS.md.bak", text0)
        atomic_write(md_path, new_text)
    state = load_state(state_path)  # re-read, keep everything else intact
    state["last_scan"] = started
    save_state(state_path, state)


# --------------------------------------------------------------------------- daily notes

DAILY_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})(?: Afternoon)?\.md$")
DAILY_BLOCK = "open tasks"
DAILY_WINDOW_DAYS = 7


def daily_task_lines(text: str) -> List[Tuple[int, bool, str]]:
    """(line_no, checked, normalized title) for task lines under '## Open tasks' (up to the next '## ')."""
    out: List[Tuple[int, bool, str]] = []
    inside = False
    for i, line in enumerate(text.split("\n")):
        h = HEADING_RE.match(line)
        if h:  # '## x' only; '### Active' does not match and stays inside the block
            inside = h["name"].lower() == DAILY_BLOCK
            continue
        if not inside:
            continue
        m = TASK_RE.match(line)
        if not m:
            continue
        content, _ = parse_body(m["body"])
        if content:
            out.append((i, m["check"] in "xX", norm(content).lower()))
    return out


def daily_notes(daily_dir: Path, today: dt.date, window: int = DAILY_WINDOW_DAYS) -> List[Tuple[dt.date, Path]]:
    if not daily_dir.is_dir():
        log(f"WARN daily notes dir not found: {daily_dir}")
        return []
    notes = []
    for p in daily_dir.iterdir():
        m = DAILY_NAME_RE.match(p.name)
        if not m:
            continue
        try:
            d = dt.date.fromisoformat(m["date"])
        except ValueError:
            continue
        if today - dt.timedelta(days=window) <= d <= today:
            notes.append((d, p))
    return sorted(notes)


def title_index(doc: Doc) -> Dict[str, List[LocalTask]]:
    idx: Dict[str, List[LocalTask]] = {}
    for lt in doc.tasks:
        idx.setdefault(norm(lt.task.content).lower(), []).append(lt)
    return idx


def apply_daily_ticks(md_path: Path, daily_dir: Path, state_dir: Path, dry_run: bool,
                      today: Optional[dt.date] = None) -> None:
    """Daily notes -> TASKS.md: a task newly ticked under '## Open tasks' is marked done in TASKS.md.

    Only a change since the last seen value of that line counts, so an old tick never re-closes a task
    that was reopened later, and an unticked line never reopens anything.
    """
    today = today or dt.date.today()
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    seen: Dict[str, Dict[str, bool]] = state.get("daily", {})
    base = {k: Task.from_dict(v) for k, v in state.get("tasks", {}).items()}
    text0 = md_path.read_text(encoding="utf-8")
    doc = parse_doc(text0, base)
    idx = title_index(doc)

    decisions: Dict[int, Optional[Decision]] = {}
    new_seen: Dict[str, Dict[str, bool]] = {}
    for _, p in daily_notes(daily_dir, today):
        prev = seen.get(p.name, {})
        cur = new_seen.setdefault(p.name, {})
        for _, checked, title in daily_task_lines(p.read_text(encoding="utf-8")):
            cur[title] = checked
            if not checked or prev.get(title) is True:
                continue
            matches = idx.get(title, [])
            open_ = [lt for lt in matches if not lt.task.done]
            if not open_:
                if not matches:
                    log(f"daily note '{p.name}': ticked task not in TASKS.md, ignored: '{title}'")
                continue
            if len(matches) > 1:
                log(f"daily note '{p.name}': title matches {len(matches)} tasks, ignored: '{title}'")
                continue
            lt = open_[0]
            if lt.line_no in decisions:
                continue
            decisions[lt.line_no] = Decision(lt.id, Task(lt.task.content, lt.task.description, lt.task.section, True),
                                             lt.body)
            log(f"{'[dry-run] would mark' if dry_run else 'marked'} done from daily note '{p.name}': "
                f"'{lt.task.content}'")
    if dry_run:
        return
    if decisions:
        new_text = render_doc(doc, decisions, [])
        if md_path.read_text(encoding="utf-8") != text0:
            log("TASKS.md changed while applying daily-note ticks; will retry next run")
            return
        atomic_write(state_dir / "TASKS.md.bak", text0)
        atomic_write(md_path, new_text)
    state = load_state(state_path)  # re-read, keep everything else intact
    state["daily"] = new_seen
    save_state(state_path, state)


def reflect_to_daily(md_path: Path, daily_dir: Path, state_dir: Path, dry_run: bool,
                     today: Optional[dt.date] = None) -> None:
    """TASKS.md -> today's daily note(s): toggle checkboxes of existing lines under '## Open tasks'.

    Ticks a line when its task is done. Unticks only a tick that was already applied to TASKS.md
    (i.e. the task was reopened afterwards), so an unprocessed tick by the user is never lost.
    """
    today = today or dt.date.today()
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    seen: Dict[str, Dict[str, bool]] = state.get("daily", {})
    doc = parse_doc(md_path.read_text(encoding="utf-8"), {})
    idx = title_index(doc)
    applied: Dict[str, Dict[str, bool]] = {}
    for _, p in daily_notes(daily_dir, today, window=0):
        text0 = p.read_text(encoding="utf-8")
        lines = text0.split("\n")
        note_seen = seen.get(p.name, {})
        updates: Dict[str, bool] = {}
        for i, checked, title in daily_task_lines(text0):
            matches = idx.get(title, [])
            if len(matches) != 1:
                continue
            done = matches[0].task.done
            if done and not checked:
                lines[i] = lines[i].replace("- [ ]", "- [x]", 1)
            elif not done and checked and note_seen.get(title) is True:
                lines[i] = re.sub(r"^- \[[xX]\]", "- [ ]", lines[i], count=1)
            else:
                continue
            updates[title] = done
            log(f"{'[dry-run] would ' if dry_run else ''}{'tick' if done else 'untick'} in '{p.name}': "
                f"'{matches[0].task.content}'")
        if not updates or dry_run:
            continue
        if p.read_text(encoding="utf-8") != text0:
            log(f"'{p.name}' changed during sync; checkbox update postponed to next run")
            continue
        atomic_write(p, "\n".join(lines))
        applied[p.name] = updates
    if applied:
        state = load_state(state_path)  # re-read, keep everything else intact
        daily = state.setdefault("daily", {})
        for name, updates in applied.items():
            daily.setdefault(name, {}).update(updates)
        save_state(state_path, state)


def sync(md_path: Path, project_name: str, state_dir: Path, dry_run: bool) -> int:
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    base = {k: Task.from_dict(v) for k, v in state.get("tasks", {}).items()}
    pending: Dict[str, str] = state.get("pending", {})

    text0 = md_path.read_text(encoding="utf-8")
    doc = parse_doc(text0, base)

    # Recover ids of tasks created in Todoist during a run whose local write was aborted.
    used = {t.id for t in doc.tasks if t.id}
    for lt in doc.tasks:
        if not lt.id and pending.get(body_hash(lt.task)) not in (None, *used):
            lt.id = pending[body_hash(lt.task)]
            used.add(lt.id)
            log(f"recovered id {lt.id} for '{lt.task.content}'")

    api = Todoist(get_token())
    data = api.full_sync()

    # ---- project
    projects = [
        pr for pr in data.get("projects", [])
        if not pr.get("is_deleted") and not pr.get("is_archived")
        and (pr["id"] == state.get("project_id") or pr.get("name") == project_name)
    ]
    projects.sort(key=lambda pr: pr["id"] != state.get("project_id"))
    if projects:
        project_id = projects[0]["id"]
    elif dry_run:
        project_id = "(new project)"
        log(f"would create project '{project_name}'")
    else:
        tmp = str(uuid.uuid4())
        st, mp = api.run([{"type": "project_add", "temp_id": tmp, "uuid": str(uuid.uuid4()), "args": {"name": project_name}}])
        if tmp not in mp:
            raise RuntimeError(f"project_add failed: {st}")
        project_id = mp[tmp]
        log(f"created project '{project_name}' ({project_id})")
    state["project_id"] = project_id

    # ---- sections
    sections = {
        s["id"]: s["name"] for s in data.get("sections", [])
        if s.get("project_id") == project_id and not s.get("is_deleted") and not s.get("is_archived")
    }
    wanted = [DEFAULT_SECTION] + [h for h in doc.headings if not is_done_heading(h)]
    missing, seen = [], {n.lower() for n in sections.values()}
    for name in wanted:
        if name.lower() not in seen:
            missing.append(name)
            seen.add(name.lower())
    if missing:
        if dry_run:
            for name in missing:
                sections[f"(new:{name})"] = name
                log(f"would create section '{name}'")
        else:
            cmds, tmps = [], {}
            for name in missing:
                tmps[name] = str(uuid.uuid4())
                cmds.append({"type": "section_add", "temp_id": tmps[name], "uuid": str(uuid.uuid4()),
                             "args": {"name": name, "project_id": project_id}})
            _, mp = api.run(cmds)
            for name, tmp in tmps.items():
                if tmp in mp:
                    sections[mp[tmp]] = name
                    log(f"created section '{name}'")
                else:
                    log(f"WARN could not create section '{name}'")
    section_ids = {name.lower(): sid for sid, name in sections.items()}

    def section_id(name: str) -> Optional[str]:
        return section_ids.get(name.lower())

    # ---- remote tasks
    remote_active = {
        it["id"]: to_task(it, sections) for it in data.get("items", [])
        if it.get("project_id") == project_id and not it.get("is_deleted") and not it.get("checked")
    }

    def fetch(tid: str) -> Optional[Task]:
        it = api.get_task(tid)
        if not it or it.get("is_deleted") or it.get("project_id") != project_id:
            return None
        return to_task(it, sections)

    plan = reconcile(doc, remote_active, fetch, base, project_id, section_id)
    for n in plan.notes:
        log(n)

    if dry_run:
        for c in plan.commands:
            log(f"[dry-run] {c['cmd']['type']} {json.dumps(c['cmd']['args'], ensure_ascii=False)}")
        new_text = render_doc(doc, plan.decisions, plan.appends)
        if new_text != text0:
            import difflib
            sys.stdout.writelines(difflib.unified_diff(
                text0.splitlines(True), new_text.splitlines(True), "TASKS.md", "TASKS.md (after sync)"))
        log(f"[dry-run] {len(plan.commands)} Todoist command(s), local file {'changes' if new_text != text0 else 'unchanged'}")
        return 0

    if md_path.read_text(encoding="utf-8") != text0:
        log("TASKS.md changed during sync; aborting, will retry next run")
        return 1

    # ---- push to Todoist
    failed: set = set()
    mapping: dict = {}
    if plan.commands:
        status, mapping = api.run([c["cmd"] for c in plan.commands])
        for c in plan.commands:
            res = status.get(c["cmd"]["uuid"])
            if res != "ok":
                log(f"{'WARN' if c['optional'] else 'ERROR'} {c['cmd']['type']} {c['tid']}: {res}")
                if not c["optional"]:
                    failed.add(c["tid"])
        log(f"pushed {len(plan.commands)} command(s) to Todoist, {len(failed)} failed")

    def resolve(tid: Optional[str]) -> Optional[str]:
        if tid and tid.startswith("tmp-"):
            return mapping.get(tid) if tid not in failed else None
        return tid

    for d in plan.decisions.values():
        if d is not None:
            d.tid = resolve(d.tid)

    new_base: Dict[str, Task] = {}
    for tid, t in plan.new_base.items():
        real = resolve(tid)
        if real is None:
            continue
        if tid in failed:
            if tid in base:
                new_base[tid] = base[tid]  # retry next run
            continue
        new_base[real] = t
    created = {body_hash(t): resolve(tid) for tid, t in plan.new_base.items() if tid.startswith("tmp-") and resolve(tid)}

    # Persist remote progress before touching the file: if the write below is aborted,
    # local-side changes keep their old base so they are re-applied next run.
    safe_base = dict(new_base)
    for tid in plan.local_changed:
        if tid in base:
            safe_base[tid] = base[tid]
        else:
            safe_base.pop(tid, None)
    state["tasks"] = {k: asdict(v) for k, v in safe_base.items()}
    state["pending"] = {**pending, **created}
    save_state(state_path, state)

    new_text = render_doc(doc, plan.decisions, plan.appends)
    if new_text != text0:
        if md_path.read_text(encoding="utf-8") != text0:
            log("TASKS.md changed during sync; local update postponed to next run")
            return 1
        atomic_write(state_dir / "TASKS.md.bak", text0)
        atomic_write(md_path, new_text)
        log("updated TASKS.md")

    state["tasks"] = {k: asdict(v) for k, v in new_base.items()}
    state["pending"] = {}
    save_state(state_path, state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", type=Path, default=Path(os.environ.get("TODOIST_SYNC_FILE", DEFAULT_FILE)))
    ap.add_argument("--project", default=os.environ.get("TODOIST_SYNC_PROJECT", DEFAULT_PROJECT))
    ap.add_argument("--state-dir", type=Path, default=Path(os.environ.get("TODOIST_SYNC_STATE_DIR", STATE_DIR)))
    ap.add_argument("--scan-dir", type=Path, default=None,
                    help="folder scanned for new .md files with open tasks (default: <vault>/Meeting Summaries)")
    ap.add_argument("--no-import", action="store_true", help="skip importing tasks from new files")
    ap.add_argument("--daily-dir", type=Path, default=None,
                    help="folder with YYYY-MM-DD.md daily notes (default: the vault root, two levels above --file)")
    ap.add_argument("--no-daily", action="store_true", help="skip syncing checkboxes with daily notes")
    ap.add_argument("--dry-run", action="store_true", help="show planned changes, write nothing")
    a = ap.parse_args()
    if a.scan_dir is None:
        env = os.environ.get("TODOIST_SYNC_SCAN_DIR")
        a.scan_dir = Path(env) if env else a.file.parent.parent / "Meeting Summaries"
    if a.daily_dir is None:
        env = os.environ.get("TODOIST_SYNC_DAILY_DIR")
        a.daily_dir = Path(env) if env else a.file.parent.parent

    a.state_dir.mkdir(parents=True, exist_ok=True)
    with open(a.state_dir / "lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another sync is running; skipping")
            return 0
        try:
            if not a.no_import:
                import_new_tasks(a.file, a.scan_dir, a.state_dir, a.dry_run)
            if not a.no_daily:
                apply_daily_ticks(a.file, a.daily_dir, a.state_dir, a.dry_run)
            rc = sync(a.file, a.project, a.state_dir, a.dry_run)
            if rc == 0 and not a.no_daily:
                reflect_to_daily(a.file, a.daily_dir, a.state_dir, a.dry_run)
            return rc
        except Exception as e:  # keep launchd log readable
            log(f"ERROR {type(e).__name__}: {e}")
            return 2


if __name__ == "__main__":
    sys.exit(main())
