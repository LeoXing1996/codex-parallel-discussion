# Codex Parallel Discussion

**One topic. One Codex fork. One tmux pane.**

Explore several ideas at once, talk directly to each fork, then bring the conclusions
back together. Give it a list of topics or messages: N items become N independent
conversations, tiled alongside your main session.

![A natural-language Codex request opens three Todo CLI discussions; the parent collects their conclusions and closes the panes](https://raw.githubusercontent.com/LeoXing1996/codex-parallel-discussion/main/demo/demo.gif)

Start in the Codex input box, invoke the skill, follow up in a fork, then collect
and close from the main conversation. Recorded with real Codex sessions in an
isolated tmux server; waiting time is shortened.
[Download the terminal recording](demo/demo.cast).

## Quick start

Requirements: **macOS or Linux**, **Node.js 18+**, **Python 3.9+**, **tmux**, and an
authenticated **Codex CLI** with `codex exec fork --json` support. Verified with
Codex CLI 0.162.1 and tmux 3.8. The Python runtime uses only the standard library;
the npm package has no dependencies or install hooks.

Install with the [Skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills add LeoXing1996/codex-parallel-discussion --agent codex --global
```

This installs the skill for your Codex sessions. Omit `--global` to install it only
in the current project. The npm package name is `skills` (plural).

Then, in a Codex session running inside tmux:

```text
Use $codex-parallel-discussion to open discussions for these topics:
- Simplify our CLI commands
- Reduce runtime lifecycle states
- Remove duplicated web caches
Only discuss; do not edit code yet.
```

The skill finds the current thread and verified parent pane, opens one fork per item,
and keeps the group state for later collection and cleanup. In any idle fork, type
**`/finish`** to notify the main thread and close that pane. Start a new Codex session
if the installed skill is not yet listed.

The skill is installed directly from this GitHub repository; no separate npm
publication of `codex-parallel-discussion` is required.

## Use the CLI directly

For shell commands, the optional CLI runs through the GitHub package specifier:

Create `topics.json`:

```json
[
  {"topic": "CLI surface", "message": "Which overlapping commands could be combined? Discuss only."},
  {"topic": "Runtime flow", "message": "Confirm readiness and wait for my question."}
]
```

A string array such as `["Discuss the CLI", "Discuss the runtime"]` also works.
Objects accept `topic`, `message`, or both. A topic-only item uses its topic as the
initial message. Messages retain their original text, including newlines and quotes.

Run from a shell inside the intended tmux pane, supplying the source Codex thread ID:

```bash
npx --yes github:LeoXing1996/codex-parallel-discussion#v0.1.0 open \
  --items topics.json \
  --source-thread <source-thread-uuid> \
  --parent-pane "$TMUX_PANE" \
  --state /tmp/discussion-state.json
```

Inside Codex tool execution, `CODEX_THREAD_ID` supplies the source automatically.
If `TMUX_PANE` is missing, verify the correct pane with `tmux list-panes -a` and pass
its `%N` ID explicitly. Never guess the source with `--last`.

```bash
npx --yes github:LeoXing1996/codex-parallel-discussion#v0.1.0 status --state /tmp/discussion-state.json
npx --yes github:LeoXing1996/codex-parallel-discussion#v0.1.0 collect --state /tmp/discussion-state.json
npx --yes github:LeoXing1996/codex-parallel-discussion#v0.1.0 close --state /tmp/discussion-state.json
```

| Command | What it does |
| --- | --- |
| `install-skill` | Copies the self-contained skill to `$CODEX_HOME/skills` or `~/.codex/skills` |
| `open` | Creates N forks, sends their initial messages, and tiles their panes |
| `status` | Returns current pane presence and terminal snapshots as JSON |
| `collect` | Saves and returns snapshots in topic order for synthesis |
| `close` | Saves final snapshots, closes owned panes, and restores the original layout when possible |

Use `open --help` for all options. `--items -` reads JSON from stdin. `--cwd` chooses
the shared workspace. `--tmux-socket` targets another server. Without `--state`,
state lives under `$CODEX_HOME/parallel-discussions/<group-id>/state.json`.

The optional `install-skill` CLI command is an alternative to `npx skills add`.
For that command, `--force` backs up and replaces an existing installation, and
`--dest <skills-directory>` selects another directory. Backups live in a sibling
`skill-backups` directory, outside skill discovery. These options belong to this
repository's CLI, not the Skills CLI.

## How it works

1. `codex exec fork <source-id> --json` creates a persistent fork without a model turn.
2. `codex resume <fork-id> <message>` opens its interactive TUI with the initial message.
3. tmux tiles the parent and discussion panes to fit the window.
4. The parent collects visible discussion results and synthesizes them.

These are independent **forks**, not native Codex sub-agents. Each inherits the source
history and accepts direct human input. `/finish` sends an explicit completion notice;
ordinary replies do not automatically report back.
The tool preserves Codex's model and permission settings and never edits its database.
See the [Codex CLI reference](https://learn.chatgpt.com/docs/cli/reference).

With the default tmux bindings, use `Ctrl+B`, then an arrow to switch panes, and
`Ctrl+B`, then `Z` to zoom. There is no fixed fork count, but available screen space
limits how many panes can be displayed. Each active fork uses your Codex account's resources.

## Boundaries and recovery

### Finish a discussion asynchronously

![Finishing one README review closes its pane and the notified parent updates the README while another review remains open](https://raw.githubusercontent.com/LeoXing1996/codex-parallel-discussion/main/demo/finish.gif)

This separate demo starts with two reviews already open. The parent was asked to
apply each completed review to the README. Typing `/finish` in the installation
review triggers that work: the parent reads the saved result, adds the missing
prerequisites, and shows the diff. The other review stays open.

Once a fork has finished replying, type this directly in its Codex input box:

```text
/finish
```

Press Enter. The tool saves the visible result, queues a notice to the source thread
with the fork ID, topic, and result path, then exits the fork's TUI and removes its
pane. Other discussions stay open. The final fork restores the original layout.
The parent can collect results even after the fork's pane has closed.
[Download this terminal recording](demo/finish.cast).

`/finish` is supplied by a **tmux adapter**, not Codex's native slash-command registry.
Codex CLI 0.162.1 rejects user-defined bare slash commands, so the adapter recognizes
the exact single-line `/finish` input before forwarding Enter. It temporarily wraps
the tmux Enter binding and applies only to marked discussion panes. Other panes keep
their original action; normal messages and Shift+Enter are forwarded normally.
The original binding is restored when the last group closes, unless you changed it.

This uses Codex's supported `queue` command. It requires no model turn in the closing
fork, but the parent may process the notification as a new turn. It is tested against
the 0.162.1 composer layout. If the notification fails, or the fork is visibly busy,
the pane stays open. An uncertain delivery is not retried automatically: inspect the
parent, then use `close` if the notice was already received.

To have the parent continue work automatically, give it that instruction before
finishing a fork, for example: "When a discussion finishes, read its saved result
and update the relevant README section. Keep the other discussions open."

### What is preserved

- **Discussion first.** Forks share the working directory and are instructed not to
  edit files until asked. This is a prompt convention, not filesystem isolation.
- **Snapshots, not full transcripts.** Collection reads bounded tmux history; earlier
  or folded messages may be missing. Ask each fork for a summary when needed.
- **History is retained.** Closing exits the TUIs and panes; it does not delete or
  archive Codex threads. It is not a backend-turn cancellation mechanism.
- **Finish active work before closing.** Visible active-turn indicators block closure;
  this check is a best-effort screen check, not an authoritative backend status API.
- **Only owned panes are closed.** Server, window, pane markers, and process identities
  are checked. Existing unrelated panes and user-changed settings are preserved.
- **Partial failures are recoverable.** Keep the state path from the error, inspect it,
  then close the created panes. Do not rerun the entire input list blindly.

State files and snapshots contain your messages and conversation output. Keep them
private; they are not uploaded by this tool.

## Shift+Enter inside tmux

If Shift+Enter sends instead of inserting a newline, tmux may be discarding the
modifier. For a terminal supporting extended keys, such as Ghostty, use:

```tmux
# ~/.tmux.conf — tested with tmux 3.8 and Ghostty
set -s extended-keys always
set -s extended-keys-format csi-u
```

Reload with `tmux source-file ~/.tmux.conf`. Existing panes may still have their old
key mode until the application requests it again; exit and resume Codex if needed.
New panes use the new configuration. Check the selected pane with:

```bash
tmux display-message -p '#{pane_key_mode}'
```

`Ext 1` or `Ext 2` indicates extended reporting. The CLI does not automatically edit
your terminal configuration. See the [tmux manual](https://man.openbsd.org/tmux#extended-keys).

## Development

```bash
git clone https://github.com/LeoXing1996/codex-parallel-discussion.git
cd codex-parallel-discussion
npm test
npm pack --dry-run
node bin/cli.mjs --help
```

Tests use real tmux on isolated sockets and a Codex stand-in, so they require no
Codex login or model calls. They cover N = 1, 2, 5, and 8; literal message handling;
collection; partial failures; ownership checks; busy panes; layout restoration;
completion notification failures; and `/finish` using a real terminal Enter event.

The skill and Python helper live together under `skills/codex-parallel-discussion/`.
The Skills CLI discovers this directory directly. The optional Node entrypoint
provides shell commands without duplicating the runtime.
See [demo notes](demo/README.md) for the recording workflow.

MIT licensed. Independent community tool; not affiliated with OpenAI.
