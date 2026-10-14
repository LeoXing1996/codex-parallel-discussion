#!/usr/bin/env python3
"""Create, inspect, collect and close N interactive Codex discussion forks."""

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import uuid

MARKER = "@codex_discussion_group"
LABEL = "@codex_discussion_label"
BORDER = " #{pane_index} · #{@codex_discussion_label} "
STATE = "@codex_discussion_state"
ENTER = "@codex_discussion_enter_binding"
ENTER_BEFORE = "@codex_discussion_enter_before"


class DiscussionError(Exception):
    pass


def execute(argv, **kwargs):
    try:
        result = subprocess.run(argv, text=True, capture_output=True, timeout=60, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DiscussionError(str(exc)) from exc
    if result.returncode:
        raise DiscussionError(result.stderr.strip() or result.stdout.strip() or str(argv))
    return result.stdout.rstrip("\n")


class Tmux:
    def __init__(self, socket=None):
        self.prefix = ["tmux"] + (["-S", socket] if socket else [])

    def run(self, *args):
        return execute(self.prefix + list(args))

    def info(self, target, fmt):
        return self.run("display-message", "-p", "-t", target, fmt)

    def option(self, scope, target, key):
        # Omit -A to distinguish local overrides from inherited values.
        if not self.run("show-options", "-" + scope + "q", "-t", target, key):
            return None
        return self.run("show-options", "-" + scope + "qv", "-t", target, key)

    def set_option(self, scope, target, key, value):
        if value is None:
            self.run("set-option", "-" + scope + "u", "-t", target, key)
        else:
            self.run("set-option", "-" + scope, "-t", target, key, value)

    def panes(self, window):
        fields = ["pane_id", "pane_pid", "pane_width", "pane_height", "pane_dead", "pane_current_command"]
        raw = self.run("list-panes", "-t", window, "-F", "\t".join("#{" + x + "}" for x in fields))
        rows = [dict(zip(fields, line.split("\t"))) for line in raw.splitlines()]
        return {row["pane_id"]: row for row in rows}

    def capture(self, pane, lines):
        raw = self.run("capture-pane", "-p", "-J", "-t", pane, "-S", str(-lines))
        return "\n".join(line.rstrip() for line in raw.splitlines())


def save(path, data):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


@contextlib.contextmanager
def lock(path, wait=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise DiscussionError("Another operation is running for this group; wait for it to finish.") from exc
        yield


def items_from(path):
    try:
        raw = json.loads(sys.stdin.read() if path == "-" else Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise DiscussionError("Cannot read items JSON: " + str(exc)) from exc
    if not isinstance(raw, list) or not raw:
        raise DiscussionError("Input must be a nonempty JSON array; each item creates one fork.")
    result = []
    for index, item in enumerate(raw, 1):
        if isinstance(item, str):
            topic, message = f"Discussion {index}", item
        elif isinstance(item, dict) and not set(item).difference({"topic", "message"}):
            topic = item.get("topic", f"Discussion {index}")
            message = item.get("message") if "message" in item else item.get("topic")
        else:
            raise DiscussionError(f"Item {index} must be a string or a topic/message object.")
        if not isinstance(topic, str) or not topic.strip() or not isinstance(message, str) or not message.strip():
            raise DiscussionError(f"Item {index}: topic and message must be nonempty strings.")
        result.append({"index": index, "topic": topic.strip(), "message": message})
    return result


def load(path):
    try:
        state = json.loads(path.read_text())
        if state.get("version") != 1 or not state.get("group_id"):
            raise DiscussionError("Unrecognized state file.")
        panes = [item["pane"] for item in state["items"] if item.get("pane")]
        if state["parent_pane"] in panes or len(panes) != len(set(panes)):
            raise DiscussionError("Pane mapping contains the parent or duplicate panes; refusing to continue.")
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise DiscussionError("Invalid state file: " + str(exc)) from exc


def check_server(tmux, state):
    identity = tmux.info(state["parent_pane"], "#{pid}\t#{window_id}")
    if identity != f"{state['server_pid']}\t{state['window']}":
        raise DiscussionError("tmux server/window identity changed; stopping.")


def owned(tmux, state, item, panes):
    pane = item.get("pane")
    if not pane or pane not in panes:
        return False
    if tmux.option("p", pane, MARKER) != state["group_id"] or panes[pane]["pane_pid"] != item.get("pane_pid"):
        raise DiscussionError(f"{pane} ownership or process identity does not match; refusing to read or close it.")
    return True


def enter_binding(tmux):
    result = subprocess.run(tmux.prefix + ["list-keys", "-T", "root", "Enter"], text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else ""


def install_enter(tmux, parent):
    current = enter_binding(tmux)
    installed = tmux.option("s", parent, ENTER)
    if installed:
        if current != installed:
            raise DiscussionError("Enter binding changed while a discussion group is open.")
        return
    # Keep the user's original Enter action as the fallback for unrelated panes.
    fallback = re.sub(r"^bind-key\s+(?:-r\s+)?-T\s+root\s+Enter\s+", "", current) if current else "send-keys Enter"
    if current and fallback == current:
        raise DiscussionError("Cannot preserve the existing Enter binding; refusing to replace it.")
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), "_enter", "--tmux-socket", tmux.prefix[2], "--pane", "#{pane_id}"])
    tmux.set_option("s", parent, ENTER_BEFORE, current)
    tmux.run("bind-key", "-T", "root", "Enter", "if-shell", "-F", "#{" + STATE + "}", "run-shell -b " + shlex.quote(command), fallback)
    tmux.set_option("s", parent, ENTER, enter_binding(tmux))


def uninstall_enter(tmux, parent):
    if tmux.run("list-panes", "-a", "-F", "#{" + STATE + "}").strip():
        return
    installed = tmux.option("s", parent, ENTER)
    if not installed:
        return
    if enter_binding(tmux) == installed:
        before = tmux.option("s", parent, ENTER_BEFORE)
        tmux.run("unbind-key", "-T", "root", "Enter")
        if before:
            execute(tmux.prefix + ["source-file", "-"], input=before + "\n")
    tmux.set_option("s", parent, ENTER, None)
    tmux.set_option("s", parent, ENTER_BEFORE, None)


def busy(tmux, pane):
    footer = "\n".join(tmux.capture(pane, 0).splitlines()[-12:])
    return bool(re.search(r"esc\s+to\s+interrupt|按\s*esc\s*.*中断", footer, re.I))


def snapshot(tmux, state, lines):
    check_server(tmux, state)
    panes = tmux.panes(state["window"])
    rows = []
    for item in state["items"]:
        present = owned(tmux, state, item, panes)
        row = {key: item.get(key) for key in ("index", "topic", "thread_id", "pane")}
        row["pane_present"] = present
        if present:
            row.update(panes[item["pane"]])
            row["screen"] = tmux.capture(item["pane"], lines).strip()
        elif item.get("finished_screen"):
            row["screen"] = item["finished_screen"]
            row["finished_at"] = item.get("finished_at")
        row["notification"] = item.get("notification")
        rows.append(row)
    return {"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(), "kind": "bounded_terminal_snapshot", "items": rows}


def fork_thread(codex, source, cwd):
    output = execute([codex, "exec", "fork", source, "--json"], cwd=cwd)
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            try:
                return str(uuid.UUID(event["thread_id"]))
            except (ValueError, KeyError) as exc:
                raise DiscussionError("Codex returned an invalid thread ID.") from exc
    raise DiscussionError("Codex did not return thread.started; check support for exec fork without a prompt.")


def initial_message(item):
    return (
        f"You are an independent discussion fork. Topic: {item['topic']}.\n"
        "Use the source conversation as background; do not continue its unfinished actions. "
        "Discuss the message below. Do not edit repository files unless the user later asks you to implement changes.\n\n"
        "When the human is ready to finish, they can type /finish on its own in this pane. "
        "The tmux adapter will notify the source thread and close this pane.\n\n"
        "Initial message for this fork:\n" + item["message"]
    )


def open_group(args):
    items = items_from(args.items)
    source = args.source_thread or os.environ.get("CODEX_THREAD_ID")
    try:
        source = str(uuid.UUID(source or ""))
    except ValueError as exc:
        raise DiscussionError("Provide an explicit --source-thread UUID or the current CODEX_THREAD_ID.") from exc
    parent = args.parent_pane or os.environ.get("TMUX_PANE")
    if not parent or not re.fullmatch(r"%\d+", parent):
        raise DiscussionError("Provide a verified --parent-pane %N.")
    codex = shutil.which(args.codex_bin)
    if not codex:
        raise DiscussionError("Codex CLI was not found on PATH.")
    cwd = str(Path(args.cwd).resolve())
    if not Path(cwd).is_dir():
        raise DiscussionError("Working directory does not exist.")
    tmux = Tmux(args.tmux_socket)
    socket, server_pid, window, layout = tmux.info(parent, "#{socket_path}\t#{pid}\t#{window_id}\t#{window_layout}").split("\t", 3)
    tmux = Tmux(socket)
    if tmux.option("w", window, MARKER):
        raise DiscussionError("This window already has a discussion group; manage it before opening another.")
    group_id = str(uuid.uuid4())
    root = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    path = Path(args.state).expanduser().resolve() if args.state else root / "parallel-discussions" / group_id / "state.json"
    with lock(path):
        if path.exists():
            raise DiscussionError("State file already exists; use status/close to manage that group.")
        state = {
            "version": 1, "group_id": group_id, "status": "opening", "socket": socket,
            "server_pid": server_pid, "window": window, "parent_pane": parent,
            "source_thread": source, "codex_bin": codex, "cwd": cwd, "original_panes": list(tmux.panes(window)),
            "original_layout": layout, "options": {}, "items": items,
            "parent_label": tmux.option("p", parent, LABEL),
        }
        for key, applied in (("pane-border-status", "top"), ("pane-border-format", BORDER)):
            state["options"][key] = {"before": tmux.option("w", window, key), "applied": applied}
        save(path, state)
        try:
            tmux.set_option("w", window, MARKER, group_id)
            install_enter(tmux, parent)
            tmux.set_option("p", parent, LABEL, "Main conversation")
            for key, values in state["options"].items():
                tmux.set_option("w", window, key, values["applied"])
            for item in items:
                item["thread_id"] = fork_thread(codex, source, cwd)
                save(path, state)
                panes = tmux.panes(window)
                targets = [p for p in panes.values() if p["pane_id"] == parent or tmux.option("p", p["pane_id"], MARKER) == group_id]
                target = max(targets, key=lambda p: int(p["pane_width"]) * int(p["pane_height"]))
                direction = "-h" if int(target["pane_width"]) > 3 * int(target["pane_height"]) else "-v"
                command = "exec " + shlex.join([codex, "resume", item["thread_id"], "-C", cwd, initial_message(item)])
                item["pane"] = tmux.run("split-window", direction, "-d", "-t", target["pane_id"], "-c", cwd, "-P", "-F", "#{pane_id}", command)
                item["pane_pid"] = tmux.info(item["pane"], "#{pane_pid}")
                save(path, state)
                tmux.set_option("p", item["pane"], MARKER, group_id)
                tmux.set_option("p", item["pane"], STATE, str(path))
                tmux.set_option("p", item["pane"], LABEL, f"Discussion {item['index']}")
                tmux.run("select-layout", "-t", window, "tiled")
                print(f"Created {item['index']}/{len(items)}: {item['topic']} ({item['pane']})", file=sys.stderr)
            state["status"] = "open"
            save(path, state)
        except (DiscussionError, KeyboardInterrupt) as exc:
            state["status"] = "partial"
            save(path, state)
            raise DiscussionError(f"Opening was incomplete; inspect/close existing panes with this state file instead of retrying the list: {path}\n{exc}") from exc
    return {"state_file": str(path), "status": state["status"], "count": len(items), "items": items}


def inspect_group(args):
    path = Path(args.state).expanduser().resolve()
    state = load(path)
    if state["status"] == "closed":
        return {"state_file": str(path), "status": "closed", "items": state["items"]}
    result = snapshot(Tmux(state["socket"]), state, args.lines)
    result.update(state_file=str(path), status=state["status"])
    if args.command == "collect":
        output = path.parent / (path.stem + ".last-collection.json")
        save(output, result)
        result["collection_file"] = str(output)
    return result


def close_group(args):
    path = Path(args.state).expanduser().resolve()
    with lock(path):
        state = load(path)
        if state["status"] == "closed":
            return {"state_file": str(path), "status": "closed", "already_closed": True}
        tmux = Tmux(state["socket"])
        capture = snapshot(tmux, state, 120)
        for row in capture["items"]:
            if row["pane_present"] and busy(tmux, row["pane"]):
                raise DiscussionError(f"{row['pane']} still shows an active turn; handle it before closing.")
        if tmux.option("w", state["window"], MARKER) != state["group_id"]:
            raise DiscussionError("Window ownership marker changed; refusing to close.")
        output = path.parent / (path.stem + ".before-close.json")
        save(output, capture)
        closed = []
        for item in state["items"]:
            if owned(tmux, state, item, tmux.panes(state["window"])):
                tmux.run("kill-pane", "-t", item["pane"])
                closed.append(item["pane"])
        restore_group(tmux, state)
        save(path, state)
        return {"state_file": str(path), "status": "closed", "closed_panes": closed, "capture_file": str(output), "history_preserved": True}


def restore_group(tmux, state):
    if set(tmux.panes(state["window"])) == set(state["original_panes"]):
        tmux.run("select-layout", "-t", state["window"], state["original_layout"])
    for key, values in state["options"].items():
        if tmux.option("w", state["window"], key) == values["applied"]:
            tmux.set_option("w", state["window"], key, values["before"])
    if tmux.option("p", state["parent_pane"], LABEL) == "Main conversation":
        tmux.set_option("p", state["parent_pane"], LABEL, state["parent_label"])
    tmux.set_option("w", state["window"], MARKER, None)
    uninstall_enter(tmux, state["parent_pane"])
    state["status"] = "closed"


def finish_fork(path, thread):
    with lock(path, wait=True):
        state = load(path)
        item = next((x for x in state["items"] if x.get("thread_id") == thread), None)
        if item is None:
            raise DiscussionError("This thread is not a fork owned by this group.")
        if item.get("finished_at"):
            return {"thread_id": thread, "status": "finished", "already_finished": True}
        tmux = Tmux(state["socket"])
        check_server(tmux, state)
        if tmux.option("w", state["window"], MARKER) != state["group_id"]:
            raise DiscussionError("Window ownership changed; refusing to finish.")
        if not owned(tmux, state, item, tmux.panes(state["window"])):
            raise DiscussionError("Fork pane is no longer present.")
        if busy(tmux, item["pane"]):
            raise DiscussionError("This fork is still working. Wait for its turn to finish, then type /finish again.")
        capture = tmux.capture(item["pane"], 120).strip()
        result_file = path.parent / (path.stem + ".finished-" + thread + ".json")
        save(result_file, {"thread_id": thread, "topic": item["topic"], "kind": "bounded_terminal_snapshot", "screen": capture})
        if item.get("notification") == "sending":
            raise DiscussionError("Previous notification delivery is uncertain. Check the parent before closing manually; no automatic resend.")
        if item.get("notification") != "sent":
            item["notification"] = "sending"
            save(path, state)
            message = (f"[Discussion finished] Thread {thread} has finished.\n"
                       f"Topic: {item['topic']}\nResult snapshot: {result_file}\nGroup state: {path}\n"
                       "This is a completion notice. Collect the saved result when useful; do not reopen the fork.")
            execute([state.get("codex_bin", "codex"), "queue", "--thread", state["source_thread"], "--message", message], cwd=state["cwd"])
            item["notification"] = "sent"
            save(path, state)
        # Delivery must succeed before closing; recheck after the external command.
        if not owned(tmux, state, item, tmux.panes(state["window"])) or busy(tmux, item["pane"]):
            raise DiscussionError("Notification sent, but the pane changed or became busy; it was left open.")
        item["finished_screen"] = capture
        item["result_file"] = str(result_file)
        save(path, state)
        tmux.run("kill-pane", "-t", item["pane"])
        item["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        remaining = tmux.panes(state["window"])
        if not any(x.get("pane") in remaining for x in state["items"]):
            restore_group(tmux, state)
        save(path, state)
        return {"thread_id": thread, "status": "finished", "notified_thread": state["source_thread"], "result_file": str(result_file)}


def handle_enter(args):
    tmux = Tmux(args.tmux_socket)
    try:
        path = tmux.option("p", args.pane, STATE)
        # Keep physical rows: cursor_y does not index capture-pane's joined lines.
        screen = tmux.run("capture-pane", "-p", "-t", args.pane, "-S", "0").splitlines()
        cursor = int(tmux.info(args.pane, "#{cursor_y}"))
        if not path or cursor >= len(screen) or screen[cursor].strip() != "› /finish":
            tmux.run("send-keys", "-t", args.pane, "Enter")
            return {}
        state = load(Path(path))
        item = next((x for x in state["items"] if x.get("pane") == args.pane), None)
        if item is None:
            raise DiscussionError("Pane is not registered in this discussion group.")
        return finish_fork(Path(path), item["thread_id"])
    except (DiscussionError, ValueError) as exc:
        tmux.run("display-message", "-d", "7000", "-t", args.pane, "/finish: " + str(exc))
        raise DiscussionError(str(exc)) from exc


def main():
    if sys.argv[1:2] == ["_enter"]:
        internal = argparse.ArgumentParser()
        internal.add_argument("--tmux-socket", required=True)
        internal.add_argument("--pane", required=True)
        try:
            handle_enter(internal.parse_args(sys.argv[2:]))
            return 0
        except DiscussionError:
            return 1
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    opening = commands.add_parser("open", help="One list item creates one interactive fork")
    opening.add_argument("--items", required=True, help="JSON file, or - for stdin")
    opening.add_argument("--parent-pane")
    opening.add_argument("--source-thread")
    opening.add_argument("--cwd", default=os.getcwd())
    opening.add_argument("--state")
    opening.add_argument("--tmux-socket")
    opening.add_argument("--codex-bin", default="codex")
    for name in ("status", "collect", "close"):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)
        if name != "close":
            command.add_argument("--lines", type=int, default=80 if name == "status" else 120)
    args = parser.parse_args()
    if hasattr(args, "lines") and not 0 <= args.lines <= 2000:
        parser.error("--lines must be between 0 and 2000")
    try:
        action = {"open": open_group, "close": close_group}.get(args.command, inspect_group)
        print(json.dumps(action(args), ensure_ascii=False, indent=2))
    except DiscussionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
