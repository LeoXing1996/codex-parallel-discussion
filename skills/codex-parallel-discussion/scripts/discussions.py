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
import select
import shlex
import shutil
import subprocess
import sys
import time
import uuid

MARKER = "@codex_discussion_group"
LABEL = "@codex_discussion_label"
BORDER = " #{pane_index} · #{@codex_discussion_label} "
STATE = "@codex_discussion_state"
ENTER = "@codex_discussion_enter_binding"
ENTER_BEFORE = "@codex_discussion_enter_before"
TITLES = "@codex_discussion_titles"
TITLE_FORMAT = "#{pane_title}#{?" + LABEL + ", | #{" + LABEL + "},}"


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
            self.run("set-option", *(["-" + scope] if scope else []), "-t", target, key, value)

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


def install_titles(tmux, session):
    # Session options are shared by groups in different windows of this session.
    if tmux.option("", session, TITLES):
        return
    options = {key: {"before": tmux.option("", session, key), "applied": value}
               for key, value in (("set-titles", "on"), ("set-titles-string", TITLE_FORMAT))}
    tmux.set_option("", session, TITLES, json.dumps(options))
    for key, values in options.items():
        tmux.set_option("", session, key, values["applied"])


def uninstall_titles(tmux, session):
    if tmux.run("list-windows", "-t", session, "-F", "#{" + MARKER + "}").strip():
        return
    saved = tmux.option("", session, TITLES)
    if saved:
        for key, values in json.loads(saved).items():
            if tmux.option("", session, key) == values["applied"]:
                tmux.set_option("", session, key, values["before"])
        tmux.set_option("", session, TITLES, None)


def preflight(args):
    """Read-only startup diagnostics; never infer a conversation's identity."""
    report = {"ready": False, "resolved": {}, "blockers": [], "pane_candidates": []}
    resolved = report["resolved"]

    def block(check, message, hint):
        report["blockers"].append({"check": check, "message": message, "hint": hint})

    source = args.source_thread or os.environ.get("CODEX_THREAD_ID")
    try:
        resolved["source_thread"] = str(uuid.UUID(source or ""))
    except ValueError:
        block("source_thread", "Current source thread UUID is missing or invalid.",
              "Supply the current CODEX_THREAD_ID or --source-thread <uuid> from this conversation. "
              "Do not use --last, the newest session, or a PID/database guess.")
    parent = args.parent_pane or os.environ.get("TMUX_PANE")
    valid_parent = bool(parent and re.fullmatch(r"%\d+", parent))
    if not valid_parent:
        block("parent_pane", "Verified parent pane is missing or invalid.",
              "Match the current conversation on a candidate's screen, then pass --parent-pane %N. "
              "A sole or active pane is not proof of identity.")
    else:
        resolved["parent_pane"] = parent
    cwd = Path(args.cwd).expanduser().resolve()
    resolved["cwd"] = str(cwd)
    if not cwd.is_dir():
        block("cwd", "Working directory does not exist.", "Pass --cwd <existing-directory>.")
    if args.state:
        state = Path(args.state).expanduser().resolve()
        resolved["state_file"] = str(state)
        if state.exists():
            block("state_file", "State file already exists.",
                  "Use status/collect/close with this --state path, or choose a new path for a new group.")
    codex = shutil.which(args.codex_bin)
    if not codex:
        block("codex", "Codex CLI was not found.", "Install Codex on PATH or pass --codex-bin <executable>.")
    else:
        resolved["codex_bin"] = str(Path(codex).resolve())
        try:
            help_text = execute([codex, "exec", "fork", "--help"])
            if "--json" not in help_text or "fork" not in help_text:
                raise DiscussionError("exec fork help does not advertise --json support.")
        except DiscussionError as exc:
            block("codex_fork", str(exc), "Use a Codex CLI supporting promptless exec fork --json.")
    if not shutil.which("tmux"):
        block("tmux", "tmux was not found on PATH.", "Install tmux and run Codex in a tmux pane.")
    else:
        tmux = Tmux(args.tmux_socket)
        fields = ("pane_id", "session_id", "window_id", "pane_current_command", "pane_title")
        try:
            raw = tmux.run("list-panes", "-a", "-F", "\t".join("#{" + f + "}" for f in fields))
            candidates = [dict(zip(fields, line.split("\t", len(fields) - 1))) for line in raw.splitlines()]
        except DiscussionError as exc:
            block("tmux_socket", str(exc),
                  "Check the tmux server/socket (use --tmux-socket if needed). For permission denied or "
                  "operation not permitted, use the host's allowed tmux access; do not change permissions automatically.")
        else:
            if not valid_parent or parent not in {p["pane_id"] for p in candidates}:
                report["pane_candidates"] = candidates
                report["capture_command"] = shlex.join(tmux.prefix + ["capture-pane", "-p", "-J", "-t", "%N", "-S", "-60"])
                if valid_parent:
                    block("parent_pane", "Parent pane is absent from this tmux server.",
                          "Verify a candidate's screen or supply the correct --tmux-socket; do not reuse a stale ID.")
            else:
                try:
                    keys = ("socket", "server_pid", "session", "window", "layout")
                    values = tmux.info(parent, "#{socket_path}\t#{pid}\t#{session_id}\t#{window_id}\t#{window_layout}").split("\t", 4)
                    resolved.update(zip(keys, values))
                    group = tmux.option("w", resolved["window"], MARKER)
                    if group:
                        paths = tmux.run("list-panes", "-t", resolved["window"], "-F", "#{" + STATE + "}")
                        resolved["existing_group"] = {"group_id": group, "state_files": sorted({p for p in paths.splitlines() if p})}
                        block("existing_group", "This window already has a discussion group.",
                              "Use status/collect with its state file; do not reopen the input list.")
                except DiscussionError as exc:
                    block("parent_pane", str(exc), "Recheck the parent pane and tmux access before opening.")
    report["ready"] = not report["blockers"]
    return report


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


def set_thread_name(codex, thread, name, cwd):
    """Use the supported metadata API; no turn/start, TUI input, or database edits."""
    try:
        process = subprocess.Popen([codex, "app-server", "--listen", "stdio://"], cwd=cwd,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise DiscussionError("Cannot start Codex for thread naming: " + str(exc)) from exc
    pending = b""

    def send(message):
        process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        process.stdin.flush()

    def request(identifier, method, params):
        nonlocal pending
        send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 15
        while True:
            if b"\n" not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                    raise DiscussionError("Timed out waiting for Codex " + method)
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise DiscussionError("Codex app-server closed before replying to " + method)
                pending += chunk
                continue
            line, pending = pending.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") != identifier:
                continue
            if "error" in response:
                raise DiscussionError(f"Codex {method}: {response['error']}")
            return

    try:
        request(1, "initialize", {"clientInfo": {"name": "codex_parallel_discussion", "version": "0.1.0"}})
        send({"method": "initialized"})
        request(2, "thread/name/set", {"threadId": thread, "name": name})
    except (OSError, ValueError) as exc:
        raise DiscussionError("Cannot name the fork: " + str(exc)) from exc
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        process.stdout.close()


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
    report = preflight(args)
    try:
        items = items_from(args.items)
    except DiscussionError as exc:
        report["blockers"].append({"check": "items", "message": str(exc), "hint": "Supply a nonempty JSON list of topics/messages."})
        report["ready"] = False
    if not report["ready"]:
        return report
    resolved = report["resolved"]
    source, parent, codex, cwd = (resolved[k] for k in ("source_thread", "parent_pane", "codex_bin", "cwd"))
    socket, server_pid, window, layout = (resolved[k] for k in ("socket", "server_pid", "window", "layout"))
    tmux = Tmux(socket)
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
            "parent_label_applied": "main", "session": resolved["session"],
        }
        for key, applied in (("pane-border-status", "top"), ("pane-border-format", BORDER)):
            state["options"][key] = {"before": tmux.option("w", window, key), "applied": applied}
        save(path, state)
        try:
            tmux.set_option("w", window, MARKER, group_id)
            install_enter(tmux, parent)
            install_titles(tmux, state["session"])
            tmux.set_option("p", parent, LABEL, state["parent_label_applied"])
            for key, values in state["options"].items():
                tmux.set_option("w", window, key, values["applied"])
            for item in items:
                item["thread_id"] = fork_thread(codex, source, cwd)
                save(path, state)
                name = " ".join(item["topic"].split())
                set_thread_name(codex, item["thread_id"], name, cwd)
                item["thread_name"] = name
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
                tmux.set_option("p", item["pane"], LABEL, f"Discussion-{item['index']}")
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
    if tmux.option("p", state["parent_pane"], LABEL) == state.get("parent_label_applied", "Main conversation"):
        tmux.set_option("p", state["parent_pane"], LABEL, state["parent_label"])
    tmux.set_option("w", state["window"], MARKER, None)
    uninstall_enter(tmux, state["parent_pane"])
    if state.get("session"):
        uninstall_titles(tmux, state["session"])
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
    for name, help_text in (("preflight", "Read-only startup checks; no forks or pane changes"),
                            ("open", "One list item creates one interactive fork")):
        opening = commands.add_parser(name, help=help_text)
        if name == "open":
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
        action = {"preflight": preflight, "open": open_group, "close": close_group}.get(args.command, inspect_group)
        result = action(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result.get("ready") is False:
            return 1
    except DiscussionError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
