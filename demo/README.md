# Terminal recordings

The GIFs and asciicast files capture **real Codex CLI 0.162.1 sessions** in an
isolated tmux 3.8 servers. Each demo uses a fresh conversation and a disposable
workspace. Prompts request short replies so the results fit in the panes.

- `demo.cast` / `demo.gif`: type a natural-language request in Codex to invoke the
  skill and open three Todo CLI discussions; follow up in the commands fork, then
  ask the parent to collect the conclusions and close all three panes.
- `finish.cast` / `finish.gif`: start with two README reviews already open; finish
  the installation review with `/finish`, then watch the notified parent update
  README.md and show the diff while the usage review stays open. No further message
  is sent to the parent after `/finish`.

The recordings contain actual terminal output. Human input consists of ordinary
chat messages and `/finish`; the Skill handles its own JSON input internally.
Waiting and progress-animation time are shortened while retaining the terminal
output. These are terminal recordings, not desktop screen captures. The automated
tests use a stand-in for Codex; these recordings do not.

See [the two scenarios](scenarios.md) for the exact prompts and expected outcomes.

To record your own demonstration, use a fresh source thread without private context
and a separate tmux server. Start Codex in that server's `discussion-demo` session
first, then attach a recorder such as [asciinema](https://asciinema.org/):

```bash
asciinema rec demo.cast -c 'tmux -L discussion-demo attach-session -t discussion-demo'
```

Launch Codex inside tmux and prepare the initial input screen before recording.
Follow the conversation prompts in the scenario notes. The first recording ends
after the parent collects and closes the group. For the second, instruct the parent
beforehand to apply completed reviews, then begin recording with the panes open.
Stop after verifying the parent updated the document and the other fork remains open.
Clean up the remaining demo sessions after recording.

Render the asciicast with [agg](https://docs.asciinema.org/manual/agg/):

```bash
agg --font-size 15 --line-height 1.2 --theme github-dark \
  --fps-cap 10 --idle-time-limit 3 demo.cast demo.gif
```

The first GIF also uses `--speed 1.5`; the second keeps normal typing speed after
waiting time has been shortened.

Inspect the first frame, transitions, and final frame before sharing. Prepare titles
before recording, or trim the setup commands from the asciicast before rendering.
Terminal output can include local paths,
conversation text, and account details. Do not include the discussion state files or
full result snapshots in a public recording repository.
