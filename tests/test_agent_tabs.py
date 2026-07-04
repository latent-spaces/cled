import json
import sqlite3
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from cled import agent_tabs


def write_jsonl(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(entry) for entry in entries) + "\n")


class TailReaderTests(unittest.TestCase):
    def test_tail_reader_skips_invalid_utf8_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            jsonl = Path(td) / "claude.jsonl"
            jsonl.write_bytes(
                b"\xa0\n"
                + json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn"}}).encode()
                + b"\n"
            )

            self.assertEqual(
                agent_tabs._read_tail_json(jsonl),
                [{"type": "assistant", "message": {"stop_reason": "end_turn"}}],
            )


class CodexParsingTests(unittest.TestCase):
    def test_codex_resume_command_exposes_thread_id(self) -> None:
        command = "codex resume ff2ffc17-778e-4266-9bf6-495dd4ca7042 --dangerously-bypass-approvals-and-sandbox"

        self.assertEqual(
            agent_tabs.parse_codex_thread_id(command),
            "ff2ffc17-778e-4266-9bf6-495dd4ca7042",
        )

    def test_plain_codex_command_has_no_thread_id(self) -> None:
        self.assertIsNone(agent_tabs.parse_codex_thread_id("codex"))

    def test_codex_resume_command_may_have_flags_before_resume(self) -> None:
        command = "codex --dangerously-bypass-approvals-and-sandbox resume 019e20d9-0a69-7190-9b0a-d477bcac85b6"

        self.assertEqual(
            agent_tabs.parse_codex_thread_id(command),
            "019e20d9-0a69-7190-9b0a-d477bcac85b6",
        )

    def test_codex_tail_is_busy_after_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rollout = Path(td) / "rollout.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
                ],
            )

            self.assertTrue(agent_tabs.is_codex_mid_turn(rollout))

    def test_codex_tail_is_busy_after_function_call(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rollout = Path(td) / "rollout.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
                    {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
                ],
            )

            self.assertTrue(agent_tabs.is_codex_mid_turn(rollout))

    def test_codex_tail_is_idle_after_assistant_message(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            rollout = Path(td) / "rollout.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
                ],
            )

            self.assertFalse(agent_tabs.is_codex_mid_turn(rollout))


class CodexThreadResolutionTests(unittest.TestCase):
    def test_resolves_thread_id_to_rollout_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.sqlite"
            rollout = Path(td) / "rollout.jsonl"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table threads (id text, rollout_path text, cwd text, source text, updated_at_ms integer)"
            )
            conn.execute(
                "insert into threads values (?, ?, ?, ?, ?)",
                ("019e20d9-0a69-7190-9b0a-d477bcac85b6", str(rollout), "/repo", "cli", 2000),
            )
            conn.commit()
            conn.close()

            self.assertEqual(
                agent_tabs.resolve_codex_rollout_for_thread(
                    db, "019e20d9-0a69-7190-9b0a-d477bcac85b6"
                ),
                rollout,
            )

    def test_resolves_single_recent_cli_thread_for_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.sqlite"
            rollout = Path(td) / "rollout.jsonl"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table threads (id text, rollout_path text, cwd text, source text, updated_at_ms integer)"
            )
            conn.execute(
                "insert into threads values (?, ?, ?, ?, ?)",
                ("thread-1", str(rollout), "/repo", "cli", 2000),
            )
            conn.commit()
            conn.close()

            self.assertEqual(agent_tabs.resolve_codex_rollout_for_cwd(db, "/repo"), rollout)

    def test_ambiguous_recent_cli_threads_for_cwd_resolve_to_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.sqlite"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table threads (id text, rollout_path text, cwd text, source text, updated_at_ms integer)"
            )
            conn.executemany(
                "insert into threads values (?, ?, ?, ?, ?)",
                [
                    ("thread-1", str(Path(td) / "one.jsonl"), "/repo", "cli", 2000),
                    ("thread-2", str(Path(td) / "two.jsonl"), "/repo", "cli", 3000),
                ],
            )
            conn.commit()
            conn.close()

            self.assertIsNone(agent_tabs.resolve_codex_rollout_for_cwd(db, "/repo"))

    def test_rollout_lookup_closes_sqlite_connection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db = Path(td) / "state.sqlite"
            rollout = Path(td) / "rollout.jsonl"
            conn = sqlite3.connect(db)
            conn.execute(
                "create table threads (id text, rollout_path text, cwd text, source text, updated_at_ms integer)"
            )
            conn.execute(
                "insert into threads values (?, ?, ?, ?, ?)",
                ("thread-1", str(rollout), "/repo", "cli", 2000),
            )
            conn.commit()
            conn.close()

            real_connect = sqlite3.connect
            opened = []

            class TrackingConnection:
                def __init__(self, connection):
                    self.connection = connection
                    self.close = mock.Mock(wraps=connection.close)

                def __enter__(self):
                    return self.connection.__enter__()

                def __exit__(self, *args):
                    return self.connection.__exit__(*args)

                def __getattr__(self, name):
                    return getattr(self.connection, name)

            def tracking_connect(*args, **kwargs):
                connection = real_connect(*args, **kwargs)
                opened.append(TrackingConnection(connection))
                return opened[-1]

            with mock.patch.object(agent_tabs.sqlite3, "connect", side_effect=tracking_connect):
                self.assertEqual(agent_tabs.resolve_codex_rollout_for_cwd(db, "/repo"), rollout)
                self.assertEqual(
                    agent_tabs.resolve_codex_rollout_for_thread(db, "thread-1"),
                    rollout,
                )

            self.assertEqual([conn.close.call_count for conn in opened], [1, 1])


class CodexStatusTests(unittest.TestCase):
    def test_running_codex_with_no_rollout_is_idle(self) -> None:
        # A codex with no resolvable rollout yet is idle (green), not "other".
        process = agent_tabs.ProcessInfo(pid=1, tty="ttys000", command="codex")
        with mock.patch.object(agent_tabs, "_resolve_codex_rollout", return_value=None):
            self.assertEqual(agent_tabs.codex_status(process), agent_tabs.STATUS_IDLE)

    def test_plain_codex_process_uses_open_rollout_file_before_ambiguous_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = root / "state.sqlite"
            rollout = root / ".codex" / "sessions" / "2026" / "05" / "28" / "rollout-test.jsonl"
            other_rollout = root / "other.jsonl"
            write_jsonl(
                rollout,
                [
                    {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
                    {"type": "response_item", "payload": {"type": "message", "role": "assistant"}},
                ],
            )
            conn = sqlite3.connect(db)
            conn.execute(
                "create table threads (id text, rollout_path text, cwd text, source text, updated_at_ms integer)"
            )
            conn.executemany(
                "insert into threads values (?, ?, ?, ?, ?)",
                [
                    ("thread-1", str(rollout), "/repo", "cli", 2000),
                    ("thread-2", str(other_rollout), "/repo", "cli", 3000),
                ],
            )
            conn.commit()
            conn.close()

            def fake_run(command, **_kwargs):
                result = mock.Mock(returncode=0, stdout="")
                if command[:2] == ["lsof", "-p"]:
                    result.stdout = f"n{rollout}\n"
                elif command[:5] == ["lsof", "-a", "-p", "123", "-d"]:
                    result.stdout = "n/repo\n"
                return result

            process = agent_tabs.ProcessInfo(
                pid=123,
                tty="ttys001",
                command="/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox",
            )
            with mock.patch.object(agent_tabs.subprocess, "run", side_effect=fake_run):
                status = agent_tabs.codex_status(process, state_db=db)

            self.assertEqual(status, agent_tabs.STATUS_IDLE)

    def test_status_refreshes_when_pid_switches_rollout(self) -> None:
        # Same staleness reasoning as claude_status: don't pin a stale rollout
        # for a pid; re-resolve so a now-busy session isn't masked.
        idle_rollout = Path("/r/old-idle.jsonl")
        busy_rollout = Path("/r/new-busy.jsonl")
        current = {"path": idle_rollout}
        process = agent_tabs.ProcessInfo(pid=42, tty="ttys002", command="codex")

        with mock.patch.object(
            agent_tabs, "_resolve_codex_rollout", side_effect=lambda process, state_db: current["path"]
        ), mock.patch.object(
            agent_tabs, "_stale_status", return_value=None
        ), mock.patch.object(
            agent_tabs, "is_codex_mid_turn", side_effect=lambda p: p == busy_rollout
        ):
            self.assertEqual(agent_tabs.codex_status(process), agent_tabs.STATUS_IDLE)
            current["path"] = busy_rollout  # same pid, new (busy) rollout
            self.assertEqual(agent_tabs.codex_status(process), agent_tabs.STATUS_BUSY)


class ClaudeStatusTests(unittest.TestCase):
    def _status(self, session):
        process = agent_tabs.ProcessInfo(pid=1, tty="ttys000", command="claude")
        with mock.patch.object(agent_tabs, "_read_claude_session", return_value=session):
            return agent_tabs.claude_status(process)

    def test_no_session_record_is_idle(self) -> None:
        # A freshly-opened claude with no session record yet is idle (green).
        self.assertEqual(self._status(None), agent_tabs.STATUS_IDLE)

    def test_busy_status_is_busy(self) -> None:
        now = int(time.time() * 1000)
        self.assertEqual(self._status({"status": "busy", "statusUpdatedAt": now}), agent_tabs.STATUS_BUSY)

    def test_idle_status_is_idle(self) -> None:
        now = int(time.time() * 1000)
        self.assertEqual(self._status({"status": "idle", "statusUpdatedAt": now}), agent_tabs.STATUS_IDLE)

    def test_waiting_status_is_idle(self) -> None:
        # Waiting on the user (permission prompt / open dialog) -> green, not red.
        now = int(time.time() * 1000)
        self.assertEqual(self._status({"status": "waiting", "statusUpdatedAt": now}), agent_tabs.STATUS_IDLE)

    def test_unknown_status_is_idle_never_busy(self) -> None:
        # Any non-"busy" value ("shell" or a future status) reads green, never red.
        now = int(time.time() * 1000)
        self.assertEqual(self._status({"status": "shell", "statusUpdatedAt": now}), agent_tabs.STATUS_IDLE)

    def test_idle_too_long_is_stale(self) -> None:
        old = int(time.time() * 1000) - (agent_tabs.IDLE_STALE_AFTER_S + 60) * 1000
        self.assertEqual(self._status({"status": "idle", "statusUpdatedAt": old}), agent_tabs.STATUS_IDLE_STALE)

    def test_waiting_too_long_is_still_idle_not_stale(self) -> None:
        # An active "needs you" wait stays green no matter how long -- only plain
        # idle decays to stale.
        old = int(time.time() * 1000) - (agent_tabs.IDLE_STALE_AFTER_S + 60) * 1000
        self.assertEqual(self._status({"status": "waiting", "statusUpdatedAt": old}), agent_tabs.STATUS_IDLE)


if __name__ == "__main__":
    unittest.main()
