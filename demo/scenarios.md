# Conversation demos

These are two independent demonstrations with fresh Codex conversations and
disposable workspaces. Both use the real Skill and real interactive forks.

## 1. Discuss a Todo CLI, then collect from the parent

Start on the main Codex input box and type:

> Use $codex-parallel-discussion to explore three topics for a minimal offline Todo CLI: commands, local storage, and first-run experience. Discuss only. Keep replies concise and leave the panes open; wait for my follow-up before collecting.

When the three forks are ready, focus the commands discussion and ask:

> Can we keep the MVP to just three commands?

Return to the main conversation:

> Collect the conclusions, summarize the MVP plan, and close all discussion panes.

End on the parent's concise plan with all three discussion panes closed.

## 2. Finish one review and let the parent update a README

Before recording, open two reviews with this instruction in the parent:

> Use $codex-parallel-discussion to open two README reviews: installation prerequisites and usage examples. Review README.md against REFERENCE.md. Forks discuss only. When a fork finishes, read its saved result and update the relevant README section, then show the diff. Do not edit before a completion notice arrives. Keep the other discussion open.

The disposable README deliberately omits Python and tmux from its requirements.
REFERENCE.md provides the verified prerequisites. Neither review fork edits files.

Start recording with the parent and both review panes visible. In the installation
review, type:

> Include Python 3.9+ and tmux as prerequisites. Give me your final recommendation.

After its reply finishes, type `/finish` and press Enter. Do not send another
message to the parent. End after the parent receives the completion notice, reads
the saved result, and adds the missing prerequisites to README.md. Show the diff
while the usage review remains open.

The parent acts on the instruction given before recording. `/finish` sends the
completion notice and closes its own pane; the parent performs the document edit.
