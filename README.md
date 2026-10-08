# todoist-sync

Two-way sync between `Obsidian/Scayle/Claude/TASKS.md` and the Todoist project **SCAYLE**.
It uses only the Python standard library and runs on the macOS system `python3`.

## Setup

1. Get your API token from Todoist (Settings → Integrations → Developer) and store it in the Keychain:
   ```sh
   security add-generic-password -s todoist-sync -a "$USER" -w <token>
   ```
2. Preview what the first run would do:
   ```sh
   python3 todoist_sync.py --dry-run
   ```
3. Run it once for real, then install the 5-minute background job:
   ```sh
   python3 todoist_sync.py
   ./install.sh
   tail -f ~/Library/Logs/todoist-sync.log
   ```
   A background job cannot show the macOS permission prompt for Google Drive. Without access it logs
   `Operation not permitted`. Grant **Full Disk Access** to the real Python app that the job runs:
   System Settings → Privacy & Security → Full Disk Access → `+` → press ⌘⇧G and paste
   `/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app`.
   Do not use `/usr/bin/python3`; it is only a stub that launches this app. If a Command Line Tools update moves the
   app, adjust the path in the plist and run `./install.sh` again.

Uninstall: `launchctl bootout gui/$(id -u)/com.scayle.todoist-sync && rm ~/Library/LaunchAgents/com.scayle.todoist-sync.plist`

## How it maps

| TASKS.md | Todoist |
|---|---|
| `## Active`, `## Waiting On`, `## Someday`, any other `##` heading | section of the same name (created if missing) |
| `## Done` or `- [x]` | completed task; done lines are moved to `## Done` |
| `**Title** - details` | content `Title`, description `details` |
| `due Fri 9 Oct 14:00` inside details | due date, sent once when the task is created |
| `%%td:<id>%%` at the end of the line | Todoist task id. Obsidian hides it in reading view. **Keep it when editing.** |

## Sync rules

The script does a 3-way merge against the last synced snapshot, kept in `~/.local/state/todoist-sync/state.json`.

- **Changed on one side only:** the change is copied to the other side (title, description, section, done/open).
- **Changed on both sides:** **Todoist wins**, field by field. A local done/open change is discarded if the task changed in Todoist at all. Discarded values are logged as `conflict`.
- **Deleted locally:** the task is closed in Todoist, never hard-deleted. If it changed in Todoist meanwhile, the line is restored instead.
- **Deleted in Todoist** (or moved to another project): the line is removed locally.
- **New task in Todoist:** appended to the matching section in the file.
- **Tasks already done locally without an id** are not pushed.

## Importing tasks from meeting summaries

Before each sync, the script scans `<vault>/Meeting Summaries/` for `.md` files **created** since the previous run.
On the very first run it scans everything created since midnight that day. Open top-level tasks (`- [ ] …`) in those
files are appended to `## Active` in TASKS.md and then synced like any other task. It skips:

- ticked tasks and indented sub-tasks
- tasks whose title already exists in TASKS.md or was synced before, including completed ones
- tasks marked "group task" or "group action" that don't name Tim (`Tim`, `Tim's`, `Schröder`). They are logged as
  `skipped group task`.

Options: `--scan-dir` or `TODOIST_SYNC_SCAN_DIR` to scan a different folder, `--no-import` to turn the step off.
Only new files are scanned, on purpose: summaries are generated automatically in one go, and later edits to them are
manual. Those edits are not imported. To change or complete a task, edit it in TASKS.md or Todoist.

## Safety

- If TASKS.md changes during a run, the local write is skipped and retried on the next run.
- A lock file prevents overlapping runs.
- The previous version of the file is kept in `~/.local/state/todoist-sync/TASKS.md.bak`.
- Tasks created in Todoist before an interrupted local write get their ids back on the next run, so no duplicates are created.

## Tests

```sh
python3 -m unittest -v test_todoist_sync test_e2e_fake
```

Overrides: `--file`, `--project`, `--state-dir`, or the matching environment variables `TODOIST_SYNC_FILE`,
`TODOIST_SYNC_PROJECT`, `TODOIST_SYNC_STATE_DIR`. You can also set `TODOIST_API_TOKEN` instead of using the Keychain.
