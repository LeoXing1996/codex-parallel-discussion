# Terminal recordings

The GIFs and asciicast files capture **real Codex CLI 0.162.1 sessions** in an
isolated tmux 3.8 server. The demo uses a fresh source conversation about generic
software design, not a private repository or the author's working conversation.
Prompts request short replies so the results fit in the panes.

- `demo.cast` / `demo.gif`: open three forks, receive replies, collect their visible
  results, zoom a pane, and close the group.
- `finish.cast` / `finish.gif`: type `/finish` in each fork, notify the source thread,
  and return to the parent pane as each discussion closes.

The recordings contain actual terminal output, including startup and model latency.
GIF playback is accelerated and long idle intervals are capped. The automated tests
use a stand-in for Codex; these recordings do not.

To record your own demonstration, use a fresh source thread without private context
and a separate tmux server. For example, with [asciinema](https://asciinema.org/):

```bash
asciinema rec demo.cast -c 'tmux -L discussion-demo new-session'
```

Inside that shell, run the `open`, `collect`, and `close` commands from the main
README with a dedicated state file. To demonstrate `/finish`, resume the source
Codex thread in the parent pane, switch to an idle fork, and type `/finish` plus Enter.
Stop recording after verifying the parent received the notice and the fork closed.

Render the asciicast with [agg](https://docs.asciinema.org/manual/agg/):

```bash
agg --font-size 13 --line-height 1.15 --theme github-dark \
  --speed 2 --fps-cap 10 --idle-time-limit 2 demo.cast demo.gif
```

Inspect recordings before sharing: terminal output can include local paths,
conversation text, and account details. Do not include the discussion state files or
full result snapshots in a public recording repository.
