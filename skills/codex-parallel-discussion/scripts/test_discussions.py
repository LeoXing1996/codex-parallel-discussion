#!/usr/bin/env python3
"""Focused end-to-end checks using real tmux and an isolated Codex stand-in."""

import json
import argparse
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid

import discussions

SCRIPT = Path(__file__).with_name("discussions.py")
STUB = '''#!/usr/bin/env python3
import json, signal, sys, uuid
from pathlib import Path
if sys.argv[1:] == ["exec", "fork", "--help"]:
    print("Usage: codex exec fork [OPTIONS] <SESSION_ID> [PROMPT] --json")
    sys.exit(0)
log = Path(LOG_PATH)
with log.open("a") as f:
    f.write(json.dumps(sys.argv[1:], ensure_ascii=False) + "\\n")
if sys.argv[1:3] == ["exec", "fork"]:
    count = sum(json.loads(x)[:2] == ["exec", "fork"] for x in log.read_text().splitlines())
    fail = Path(FAIL_PATH)
    if fail.exists() and count == int(fail.read_text()):
        print("planned fork failure", file=sys.stderr)
        sys.exit(2)
    print(json.dumps({"type": "thread.started", "thread_id": str(uuid.uuid4())}))
elif sys.argv[1] == "app-server":
    for line in sys.stdin:
        request = json.loads(line)
        with log.open("a") as f:
            f.write(json.dumps(["rpc", request], ensure_ascii=False) + "\\n")
        if request["method"] == "thread/name/set" and "RENAME_FAIL" in request["params"]["name"]:
            response = {"id": request["id"], "error": {"message": "planned rename failure"}}
        elif "id" in request:
            response = {"id": request["id"], "result": {}}
        else:
            continue
        print(json.dumps({"method": "fixture/notification"}), flush=True)
        print(json.dumps(response), flush=True)
elif sys.argv[1] == "resume":
    print("Codex fixture", flush=True)
    print(sys.argv[-1], flush=True)
    print("Reply: received", flush=True)
    if "BUSY_FIXTURE" in sys.argv[-1]:
        print("Working (esc to interrupt)", flush=True)
    print("› /finish" if "FINISH_FIXTURE" in sys.argv[-1] else "› Ask Codex to do anything", end="", flush=True)
    signal.pause()
elif sys.argv[1] == "queue":
    if "NOTIFY_FAIL" in sys.argv[-1]:
        print("notification delivery failed", file=sys.stderr)
        sys.exit(2)
    print("queued", flush=True)
else:
    sys.exit(2)
'''


class PreflightTest(unittest.TestCase):
    def args(self, **overrides):
        return argparse.Namespace(**dict(dict(source_thread=None, parent_pane=None, cwd=os.getcwd(),
                                              state=None, codex_bin="codex", tmux_socket=None), **overrides))

    def test_missing_identity_and_dependencies_are_reported_together(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("discussions.shutil.which", return_value=None):
            result = discussions.preflight(self.args())
        self.assertFalse(result["ready"])
        self.assertEqual({b["check"] for b in result["blockers"]}, {"source_thread", "parent_pane", "codex", "tmux"})

    def test_socket_permission_failure_is_actionable_without_retry_or_mutation(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("discussions.shutil.which", return_value="/bin/tool"), \
                mock.patch("discussions.execute", side_effect=["exec fork --json", discussions.DiscussionError("Operation not permitted")]) as run:
            result = discussions.preflight(self.args())
        self.assertEqual({b["check"] for b in result["blockers"]}, {"source_thread", "parent_pane", "tmux_socket"})
        self.assertIn("allowed tmux access", result["blockers"][-1]["hint"])
        self.assertEqual([c.args[0][1] for c in run.call_args_list], ["exec", "list-panes"])


class DiscussionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-discussion-test-", dir="/private/tmp" if Path("/private/tmp").exists() else None)
        self.root = Path(self.temp.name)
        self.socket = str(self.root / "tmux.sock")
        self.state = self.root / "state.json"
        self.log = self.root / "calls.jsonl"
        self.fail_path = self.root / "fail-at"
        self.codex = self.root / "fake codex"
        self.codex.write_text(STUB.replace("LOG_PATH", repr(str(self.log))).replace("FAIL_PATH", repr(str(self.fail_path))))
        self.codex.chmod(0o755)
        self.parent = self.tmux("-f", "/dev/null", "new-session", "-d", "-s", "isolated", "-x", "240", "-y", "70", "-P", "-F", "#{pane_id}", "sleep 3600")
        self.window = self.tmux("display-message", "-p", "-t", self.parent, "#{window_id}")

    def tearDown(self):
        subprocess.run(["tmux", "-S", self.socket, "kill-server"], capture_output=True)
        self.temp.cleanup()

    def tmux(self, *args):
        return subprocess.check_output(["tmux", "-S", self.socket, *args], text=True).strip()

    def call(self, command, *extra, items=None, ok=True, env=None):
        args = [sys.executable, "-B", str(SCRIPT), command, "--state", str(self.state)]
        if command in ("open", "preflight"):
            args += ["--parent-pane", self.parent, "--source-thread", str(uuid.UUID(int=1)), "--cwd", str(self.root), "--tmux-socket", self.socket, "--codex-bin", str(self.codex)]
            if command == "open":
                args += ["--items", "-"]
        result = subprocess.run(args + list(extra), input=json.dumps(items, ensure_ascii=False) if items is not None else None, text=True, capture_output=True, timeout=30, env=env)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def panes(self):
        return self.tmux("list-panes", "-t", self.window, "-F", "#{pane_id}").splitlines()

    def ready(self, count):
        for _ in range(40):
            calls = [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []
            resumes = [c for c in calls if c[0] == "resume"]
            if len(resumes) == count:
                return resumes
            time.sleep(0.05)
        self.fail("initial messages were not received")

    def test_preflight_is_read_only_and_explicit_arguments_override_environment(self):
        before = self.tmux("show-options", "-t", self.parent)
        result = self.call("preflight", env=dict(os.environ, CODEX_THREAD_ID="invalid", TMUX_PANE="%9999"))
        self.assertTrue(result["ready"])
        self.assertEqual(result["resolved"]["source_thread"], str(uuid.UUID(int=1)))
        self.assertEqual(result["resolved"]["parent_pane"], self.parent)
        self.assertEqual(self.panes(), [self.parent])
        self.assertEqual(self.tmux("show-options", "-t", self.parent), before)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.state.with_suffix(".lock").exists())
        self.assertFalse(self.log.exists(), "preflight must not fork, resume, or queue")

    def test_preflight_uses_current_environment_identity(self):
        result = self.call("preflight", "--source-thread", "", "--parent-pane", "",
                           env=dict(os.environ, CODEX_THREAD_ID=str(uuid.UUID(int=2)), TMUX_PANE=self.parent))
        self.assertEqual(result["resolved"]["source_thread"], str(uuid.UUID(int=2)))
        self.assertEqual(result["resolved"]["parent_pane"], self.parent)

    def test_open_reports_all_blockers_and_candidates_without_creating_resources(self):
        result = self.call("open", "--source-thread", "invalid", "--parent-pane", "%9999",
                           "--cwd", str(self.root / "missing"), items=[], ok=False)
        report = json.loads(result.stdout)
        self.assertEqual({b["check"] for b in report["blockers"]}, {"source_thread", "parent_pane", "cwd", "items"})
        self.assertEqual([p["pane_id"] for p in report["pane_candidates"]], [self.parent])
        self.assertIn("capture-pane", report["capture_command"])
        self.assertEqual(self.panes(), [self.parent])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.log.exists())

    def test_preflight_rejects_unsupported_codex_and_unreachable_socket(self):
        self.codex.write_text("#!/bin/sh\nprintf 'unsupported command' >&2\nexit 2\n")
        result = self.call("preflight", "--tmux-socket", str(self.root / "missing.sock"), ok=False)
        self.assertEqual({b["check"] for b in json.loads(result.stdout)["blockers"]}, {"codex_fork", "tmux_socket"})

    def test_preflight_reports_existing_group_and_state_for_recovery(self):
        self.state = self.root / "state with spaces.json"
        self.call("open", items=["one"])
        result = json.loads(self.call("preflight", ok=False).stdout)
        self.assertEqual({b["check"] for b in result["blockers"]}, {"existing_group", "state_file"})
        self.assertEqual(result["resolved"]["existing_group"]["state_files"], [str(self.state)])
        self.assertEqual(len(self.panes()), 2)

    def test_terminal_title_follows_selected_pane_and_codex_title_then_restores(self):
        before = self.tmux("show-options", "-t", self.parent)
        result = self.call("open", items=["one"])
        self.ready(1)
        pane = result["items"][0]["pane"]
        self.assertEqual(self.tmux("show-options", "-v", "-t", self.parent, "set-titles"), "on")
        title = self.tmux("show-options", "-v", "-t", self.parent, "set-titles-string")
        for target, name, role in ((self.parent, "Memory design", "main"), (pane, "Claude research", "Discussion-1"),
                                   (pane, "Renamed discussion", "Discussion-1")):
            self.tmux("select-pane", "-t", target, "-T", name)
            self.tmux("select-pane", "-t", target)
            self.assertEqual(self.tmux("display-message", "-p", "-t", self.window, title), name + " | " + role)
        self.call("close")
        self.assertEqual(self.tmux("show-options", "-t", self.parent), before)

    def test_titles_restore_after_last_group_and_preserve_user_changes(self):
        self.tmux("set-option", "-t", self.parent, "set-titles", "off")
        self.tmux("set-option", "-t", self.parent, "set-titles-string", "original title")
        self.call("open", items=["one"])
        self.ready(1)
        first_state = self.state
        other = self.tmux("new-window", "-d", "-t", "isolated", "-P", "-F", "#{pane_id}", "sleep 3600")
        self.state = self.root / "second.json"
        self.call("open", "--parent-pane", other, items=["two"])
        self.ready(2)
        self.call("close", "--state", str(first_state))
        self.assertEqual(self.tmux("show-options", "-v", "-t", other, "set-titles"), "on")
        self.tmux("set-option", "-t", other, "set-titles-string", "user changed title")
        self.call("close")
        self.assertEqual(self.tmux("show-options", "-v", "-t", other, "set-titles"), "off")
        self.assertEqual(self.tmux("show-options", "-v", "-t", other, "set-titles-string"), "user changed title")
        self.assertEqual(self.tmux("show-options", "-qv", "-t", other, discussions.TITLES), "")

    def test_forks_are_named_through_metadata_api_before_resume(self):
        topic = 'Memory "隔离" `literal` $(literal)'
        result = self.call("open", items=[{"topic": topic, "message": "Discuss only"}])
        self.ready(1)
        item = result["items"][0]
        self.assertEqual(item["thread_name"], topic)
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        requests = [c[1] for c in calls if c[0] == "rpc"]
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized", "thread/name/set"])
        self.assertEqual(requests[-1]["params"], {"threadId": item["thread_id"], "name": topic})
        self.assertLess(next(i for i, c in enumerate(calls) if c[0] == "rpc" and c[1]["method"] == "thread/name/set"),
                        next(i for i, c in enumerate(calls) if c[0] == "resume"))

    def test_rename_failure_preserves_fork_id_for_recovery_without_opening_pane(self):
        self.call("open", items=[{"topic": "RENAME_FAIL", "message": "one"}, "two"], ok=False)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "partial")
        self.assertTrue(state["items"][0]["thread_id"])
        self.assertNotIn("thread_id", state["items"][1])
        self.assertEqual(self.panes(), [self.parent])
        self.call("close")

    def test_variable_counts_message_integrity_collect_close(self):
        for count in (1, 2, 5, 8):
            with self.subTest(count=count):
                self.state = self.root / f"group-{count}.json"
                self.log.write_text("")
                message = '原文 "引号"\n第二行 `touch SHOULD_NOT_EXIST` $(touch ALSO_NOT_EXIST) $HOME'
                inputs = [{"topic": f"方向 {i}", "message": message + str(i)} for i in range(count)]
                original_layout = self.tmux("display-message", "-p", "-t", self.parent, "#{window_layout}")
                result = self.call("open", items=inputs)
                self.assertEqual(result["count"], count)
                self.assertEqual(len(self.panes()), count + 1)
                received = self.ready(count)
                self.assertEqual({r[-1].split("Initial message for this fork:\n", 1)[1] for r in received}, {x["message"] for x in inputs})
                self.assertFalse((self.root / "SHOULD_NOT_EXIST").exists())
                self.assertFalse((self.root / "ALSO_NOT_EXIST").exists())
                collected = self.call("collect")
                self.assertEqual([r["topic"] for r in collected["items"]], [x["topic"] for x in inputs])
                self.assertTrue(Path(collected["collection_file"]).exists())
                self.assertTrue(all(r["pane_present"] for r in collected["items"]))
                self.call("close")
                self.assertEqual(self.panes(), [self.parent])
                self.assertEqual(self.tmux("display-message", "-p", "-t", self.parent, "#{window_layout}"), original_layout)
                self.assertTrue(self.call("close")["already_closed"])

    def test_existing_panes_and_changed_options_survive(self):
        other = self.tmux("split-window", "-h", "-d", "-t", self.parent, "-P", "-F", "#{pane_id}", "sleep 3600")
        self.tmux("set-option", "-w", "-t", self.window, "pane-border-status", "bottom")
        self.call("open", items=["message 1", {"topic": "topic only"}])
        self.ready(2)
        self.tmux("set-option", "-w", "-t", self.window, "pane-border-format", "user changed this")
        self.call("close")
        self.assertEqual(set(self.panes()), {self.parent, other})
        self.assertEqual(self.tmux("show-options", "-wv", "-t", self.window, "pane-border-status"), "bottom")
        self.assertEqual(self.tmux("show-options", "-wv", "-t", self.window, "pane-border-format"), "user changed this")

    def test_partial_failure_can_be_closed_without_losing_parent(self):
        self.fail_path.write_text("2")
        self.call("open", items=["one", "two", "three"], ok=False)
        state = json.loads(self.state.read_text())
        self.assertEqual(state["status"], "partial")
        self.assertEqual(len(self.panes()), 2)
        self.call("close")
        self.assertEqual(self.panes(), [self.parent])

    def test_wrong_marker_refuses_all_closure(self):
        result = self.call("open", items=["one", "two"])
        self.ready(2)
        pane = result["items"][1]["pane"]
        self.tmux("set-option", "-p", "-t", pane, "@codex_discussion_group", "someone-else")
        self.call("close", ok=False)
        self.assertEqual(len(self.panes()), 3)

    def test_modified_mapping_cannot_close_parent(self):
        self.call("open", items=["one"])
        state = json.loads(self.state.read_text())
        state["items"][0]["pane"] = self.parent
        self.state.write_text(json.dumps(state))
        self.call("close", ok=False)
        self.assertEqual(len(self.panes()), 2)

    def test_busy_branch_and_replaced_process_are_not_closed(self):
        result = self.call("open", items=["BUSY_FIXTURE"])
        self.ready(1)
        self.call("close", ok=False)
        self.assertEqual(len(self.panes()), 2)
        self.tmux("respawn-pane", "-k", "-t", result["items"][0]["pane"], "sleep 3600")
        self.call("close", ok=False)
        self.assertEqual(len(self.panes()), 2)

    def test_invalid_input_creates_no_resources(self):
        for value in ([], {}, [{"topic": ""}], [{"message": 7}], [{"mesage": "typo"}]):
            with self.subTest(value=value):
                self.call("open", items=value, ok=False)
                self.assertEqual(self.panes(), [self.parent])
                self.assertFalse(self.state.exists())

    def enter(self, pane, ok=True):
        result = subprocess.run([sys.executable, "-B", str(SCRIPT), "_enter", "--tmux-socket", self.socket, "--pane", pane], text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        self.assertEqual(result.stdout, "", "tmux key callbacks must not print into the terminal")
        return result

    def test_normal_enter_is_forwarded_and_does_not_notify(self):
        result = self.call("open", items=["ordinary discussion"])
        self.ready(1)
        pane = result["items"][0]["pane"]
        before = self.tmux("display-message", "-p", "-t", pane, "#{cursor_y}")
        self.enter(pane)
        for _ in range(20):
            if self.tmux("display-message", "-p", "-t", pane, "#{cursor_y}") != before:
                break
            time.sleep(.05)
        self.assertNotEqual(self.tmux("display-message", "-p", "-t", pane, "#{cursor_y}"), before)
        self.assertEqual(len(self.panes()), 2)
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertFalse(any(x[0] == "queue" for x in calls))

    def test_finish_notifies_once_collects_closed_fork_and_restores_enter(self):
        self.tmux("bind-key", "-T", "root", "Enter", "display-message", "custom enter")
        before = self.tmux("list-keys", "-T", "root", "Enter")
        result = self.call("open", items=["FINISH_FIXTURE", "second fork"])
        self.ready(2)
        pane = result["items"][0]["pane"]
        self.enter(pane)
        self.assertEqual(len(self.panes()), 2)
        collected = self.call("collect")
        self.assertFalse(collected["items"][0]["pane_present"])
        self.assertEqual(collected["items"][0]["notification"], "sent")
        self.assertIn("Reply: received", collected["items"][0]["screen"])
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        notifications = [x for x in calls if x[0] == "queue"]
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][1:3], ["--thread", str(uuid.UUID(int=1))])
        self.assertIn(result["items"][0]["thread_id"], notifications[0][-1])
        self.call("close")
        self.assertEqual(self.tmux("list-keys", "-T", "root", "Enter"), before)

    def test_finish_failed_notification_or_busy_turn_keeps_pane(self):
        result = self.call("open", items=[{"topic": "NOTIFY_FAIL", "message": "FINISH_FIXTURE"}, "BUSY_FIXTURE FINISH_FIXTURE"])
        self.ready(2)
        self.enter(result["items"][0]["pane"], ok=False)
        self.enter(result["items"][0]["pane"], ok=False)
        self.enter(result["items"][1]["pane"], ok=False)
        self.assertEqual(len(self.panes()), 3)
        calls = [json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertEqual(sum(x[0] == "queue" for x in calls), 1)

    def test_real_enter_key_triggers_finish_and_closes_last_fork(self):
        result = self.call("open", items=["FINISH_FIXTURE"])
        self.ready(1)
        pane = result["items"][0]["pane"]
        self.tmux("select-pane", "-t", pane)
        master, slave = pty.openpty()
        env = dict(os.environ, TERM="xterm-256color")
        env.pop("TMUX", None)
        client = subprocess.Popen(["tmux", "-S", self.socket, "attach", "-t", "isolated"], stdin=slave, stdout=slave, stderr=slave, env=env, start_new_session=True)
        os.close(slave)
        try:
            time.sleep(.3)
            os.write(master, b"\r")
            for _ in range(100):
                if select.select([master], [], [], .05)[0]:
                    os.read(master, 65536)
                if json.loads(self.state.read_text())["status"] == "closed":
                    break
            self.assertEqual(self.panes(), [self.parent])
            state = json.loads(self.state.read_text())
            self.assertEqual(state["status"], "closed")
            self.assertEqual(state["items"][0]["notification"], "sent")
            self.assertTrue(Path(state["items"][0]["result_file"]).exists())
            binding = subprocess.run(["tmux", "-S", self.socket, "list-keys", "-T", "root", "Enter"], capture_output=True)
            self.assertNotEqual(binding.returncode, 0)
        finally:
            client.terminate()
            client.wait(timeout=5)
            os.close(master)


if __name__ == "__main__":
    unittest.main(verbosity=2)
