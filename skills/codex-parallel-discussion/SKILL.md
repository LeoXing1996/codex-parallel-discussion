---
name: codex-parallel-discussion
description: Open N interactive Codex forks from a list of topics or messages in tmux panes, collect discussion results, and close the group. Use for parallel conversations the human can participate in directly.
---

# Codex Parallel Discussion

Give each independent topic its own interactive fork and keep the main conversation
as the synthesis point. The input list determines N. Do not invent extra topics
to reach a fixed count. For blank discussion entry points, send an initial message
asking each fork to acknowledge readiness and wait.

## What these sessions are

- These are **independent forks**, not native sub-agents. On Codex CLI 0.162.1,
  resuming a native sub-agent opens a viewer with direct input disabled.
- Forks inherit the source history but do not report back automatically.
  Collect their results from the main conversation. `/finish` explicitly sends a completion notice.
- Discussion is the default; do not edit repository files until asked.
  Forks share a working directory. Use separate worktrees if implementation needs isolation.
- The helper uses `codex exec fork --json` to obtain actual thread IDs, then
  `codex resume <id> <message>` to launch each TUI. It preserves model and permission
  settings and does not edit the Codex database.
- Read panes with `tmux capture-pane`, not an interactive `tmux attach` in tool logs.

## Open a group

The bundled helper needs Python 3.9+, tmux, and a Codex CLI supporting promptless
`codex exec fork --json`. Verified with Codex 0.162.1 and tmux 3.8. See the
[official CLI reference](https://learn.chatgpt.com/docs/cli/reference).

Write a nonempty JSON array. Each item is a message string or an object containing
`topic`, `message`, or both. A topic-only object's topic is also its message.
**N list items create N forks.**

```json
[
  {"topic": "CLI surface", "message": "Discuss which commands could be combined. Do not edit code."},
  {"topic": "Runtime flow", "message": "Confirm readiness and wait for my question."}
]
```

1. Verify the parent pane. Use a reliable `TMUX_PANE`, or list panes and match the
   current conversation on screen. Do not guess from the active window or reuse an old ID.
2. Use the current `CODEX_THREAD_ID` as the source; never use `--last`.
3. Run the helper from this skill's directory:

```bash
python3 scripts/discussions.py open --items /tmp/topics.json --parent-pane %1
```

Replace `%1` with the verified pane ID. `--items -` reads stdin. Optional flags:
`--source-thread <uuid>`, `--cwd <directory>`, `--state <path>`, and
`--tmux-socket <path>`. Pass messages through a JSON file or stdin, not shell interpolation.

Save the returned `state_file`. The default is
`${CODEX_HOME:-~/.codex}/parallel-discussions/<group-id>/state.json`.
The helper preserves existing panes and tiles the window dynamically. With the
default tmux prefix, press `Ctrl+B`, then `Z` to zoom the selected pane.

Run `status` after opening. Check that each fork received its message and is not
stuck on an error or approval prompt. Creation does not mean the reply is complete.
Do not automatically approve additional permissions or inject the same message twice.

## Inspect and collect

```bash
python3 scripts/discussions.py status --state <state.json>
python3 scripts/discussions.py collect --state <state.json> --lines 120
```

Collection returns topics, thread IDs, and **bounded terminal snapshots** in input
order, and saves a collection file. It sends no message and triggers no model turn.
Snapshots are not full transcripts: earlier or folded content may be missing.
Summarize actual agreement, disagreements, open questions, and next steps. Disclose
missing context and active/blocked turns. When needed within the user's request,
ask a fork to summarize its own discussion before collecting again.

## Close

In a discussion pane, the human can type **`/finish`** on a line by itself and press
Enter once the fork is idle. This is a **tmux adapter**, not a registered Codex slash
command. It saves a snapshot, runs `codex queue --thread <source-id> --message ...`,
then exits that fork's TUI and closes only its pane. The main thread receives the
fork ID, topic, and local result/state paths. No extra model turn is needed in the
finishing fork; the parent may process the queued notice as a new turn.

The adapter temporarily wraps tmux's Enter binding. Only marked fork panes invoke
it; unrelated panes retain the original action. Ordinary input and Shift+Enter are
not finish triggers. The original binding is restored after the final group closes,
unless the user has changed it in the meantime. It matches the single-line Codex
composer on the tested CLI version; if another TUI version rejects `/finish`, use
the group's explicit close workflow instead.

If a notification fails or its delivery is uncertain, keep the pane open and inspect
the parent before manually closing it; never resend blindly. A visible active turn
also blocks `/finish`. Results from completed forks remain available in collection.

When asked to close, collect the results and check that no work still needs to finish:

```bash
python3 scripts/discussions.py close --state <state.json>
```

This exits the group's TUIs and panes, preserving conversation history. It does not
archive/delete threads or guarantee cancellation of backend turns. The helper
refuses closure when a visible active-turn indicator is present.

Server, window, pane ownership, and process identities must match. The helper saves
pre-close snapshots and restores layout/settings only when still owned by the group.
Repeated close is safe. After a partial open failure, inspect or close the recorded
group using the state path in the error; do not reopen the entire list automatically.
