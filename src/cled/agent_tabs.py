"""
iTerm2 agent tab monitor.

Maps tabs in the focused iTerm2 window to a small status vocabulary used by
the keyboard renderer. Claude Code and Codex expose different local state, so
provider-specific logic is kept behind small status providers.

Layout: shared tab/process detection, then one section per provider (Claude,
Codex), then the monitor that polls the window and runs the providers.
"""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

NUM_SLOTS = 10
DEFAULT_POLL_INTERVAL = 1.0
IDLE_STALE_AFTER_S = 20 * 60
TAIL_READ_BYTES = 16 * 1024

STATUS_BUSY = "busy"
STATUS_IDLE = "idle"
STATUS_IDLE_STALE = "idle_stale"
STATUS_OTHER = "other"


# ---------------------------------------------------------------------------
# Shared tab detection — iTerm2 ttys, foreground processes, and file helpers.
# Both providers depend on everything in this section.
# ---------------------------------------------------------------------------

_FOCUSED_TTYS_SCRIPT = '''
tell application "iTerm2"
    set frontWin to missing value
    repeat with w in windows
        if frontmost of w then
            set frontWin to w
            exit repeat
        end if
    end repeat
    if frontWin is missing value then return ""
    set output to ""
    repeat with t in tabs of frontWin
        set output to output & (tty of current session of t) & linefeed
    end repeat
    return output
end tell
'''


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    tty: str
    command: str


def _focused_window_ttys() -> list[str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", _FOCUSED_TTYS_SCRIPT],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode != 0:
        return []
    return [line.strip().removeprefix("/dev/") for line in result.stdout.splitlines() if line.strip()]


def _foreground_processes() -> dict[str, ProcessInfo]:
    try:
        result = subprocess.run(
            ["ps", "-A", "-o", "pid=", "-o", "tty=", "-o", "tpgid=", "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {}
    if result.returncode != 0:
        return {}

    out: dict[str, ProcessInfo] = {}
    for line in result.stdout.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) != 4:
            continue
        pid_s, tty, tpgid_s, command = parts
        if not pid_s.isdigit() or not tpgid_s.isdigit() or tty == "??":
            continue
        pid, tpgid = int(pid_s), int(tpgid_s)
        if pid == tpgid:
            out[tty] = ProcessInfo(pid=pid, tty=tty, command=command)
    return out


def _process_cwd(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def _read_tail_json(path: Path) -> list[dict]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            f.seek(max(0, size - TAIL_READ_BYTES))
            tail = f.read()
    except OSError:
        return []

    entries = []
    for raw in tail.split(b"\n"):
        if not raw:
            continue
        try:
            entries.append(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    return entries


def _stale_status(path: Path) -> str | None:
    try:
        if time.time() - path.stat().st_mtime >= IDLE_STALE_AFTER_S:
            return STATUS_IDLE_STALE
    except FileNotFoundError:
        return STATUS_OTHER
    return None


def _tokenize(command: str) -> list[str]:
    """Split a command line into argv tokens, falling back to a plain
    whitespace split when it isn't well-formed for shlex."""
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _command_name(command: str) -> str:
    parts = _tokenize(command)
    if not parts:
        return ""
    return Path(parts[0]).name


# ---------------------------------------------------------------------------
# Claude provider — reads the per-session status Claude Code maintains in
# ~/.claude/sessions/<pid>.json (busy / idle / waiting / shell / ...).
# ---------------------------------------------------------------------------

CLAUDE_HOME = Path.home() / ".claude"
CLAUDE_SESSIONS_DIR = CLAUDE_HOME / "sessions"


def _read_claude_session(pid: int) -> dict | None:
    """Read ~/.claude/sessions/<pid>.json -- Claude Code's own record of the
    session, including its live `status` and `statusUpdatedAt` (epoch ms)."""
    try:
        return json.loads((CLAUDE_SESSIONS_DIR / f"{pid}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def claude_status(process: ProcessInfo) -> str | None:
    if _command_name(process.command) != "claude":
        return None
    # Claude Code reports the session's live state itself; trust it over parsing
    # the transcript. Only "busy" is working. "idle" is done -- and goes stale if
    # left untouched too long. "waiting" (blocked on the user), "shell", and any
    # other value read green -- an unknown status can never wrongly show red, and
    # an active "needs you" wait never decays to stale.
    session = _read_claude_session(process.pid)
    if session is None:
        # A recognized claude with no session record yet (freshly opened) is idle
        # and waiting -- green, not "other" (blue is for non-agent tabs).
        return STATUS_IDLE
    status = session.get("status")
    if status == "busy":
        return STATUS_BUSY
    if status == "idle":
        updated_ms = session.get("statusUpdatedAt")
        if updated_ms and time.time() - updated_ms / 1000 >= IDLE_STALE_AFTER_S:
            return STATUS_IDLE_STALE
    return STATUS_IDLE


# ---------------------------------------------------------------------------
# Codex provider — resolves the active rollout jsonl (by thread id, open file,
# or cwd via the ~/.codex state db) and reads it to tell busy from idle.
# ---------------------------------------------------------------------------

CODEX_HOME = Path.home() / ".codex"
CODEX_STATE_DB = CODEX_HOME / "state_5.sqlite"

_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def parse_codex_thread_id(command: str) -> str | None:
    parts = _tokenize(command)
    if not parts or Path(parts[0]).name != "codex":
        return None
    for idx, part in enumerate(parts[:-1]):
        if part == "resume":
            candidate = parts[idx + 1]
            return candidate if _UUID.match(candidate) else None
    return None


def resolve_codex_rollout_for_thread(db_path: Path, thread_id: str) -> Path | None:
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            row = conn.execute("select rollout_path from threads where id = ?", (thread_id,)).fetchone()
    except sqlite3.Error:
        return None
    return Path(row[0]) if row and row[0] else None


def resolve_codex_rollout_for_cwd(db_path: Path, cwd: str) -> Path | None:
    try:
        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
            rows = conn.execute(
                """
                select rollout_path
                from threads
                where source = 'cli' and cwd = ?
                order by updated_at_ms desc
                limit 2
                """,
                (cwd,),
            ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) != 1 or not rows[0][0]:
        return None
    return Path(rows[0][0])


def _codex_open_rollout(pid: int) -> Path | None:
    try:
        result = subprocess.run(
            ["lsof", "-p", str(pid), "-Fn"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if not line.startswith("n"):
            continue
        path = Path(line[1:])
        if ".codex/sessions/" in str(path) and path.name.startswith("rollout-") and path.suffix == ".jsonl":
            return path
    return None


def is_codex_mid_turn(rollout: Path) -> bool:
    for entry in reversed(_read_tail_json(rollout)):
        typ = entry.get("type")
        payload = entry.get("payload") or {}
        payload_type = payload.get("type")
        if typ == "event_msg" and payload_type == "user_message":
            return True
        if typ != "response_item":
            continue
        if payload_type == "message" and payload.get("role") == "assistant":
            return False
        if payload_type in ("function_call", "function_call_output", "reasoning"):
            return True
    return False


def _resolve_codex_rollout(process: ProcessInfo, state_db: Path) -> Path | None:
    thread_id = parse_codex_thread_id(process.command)
    if thread_id:
        return resolve_codex_rollout_for_thread(state_db, thread_id)
    rollout = _codex_open_rollout(process.pid)
    if rollout is not None:
        return rollout
    cwd = _process_cwd(process.pid)
    if cwd is None:
        return None
    return resolve_codex_rollout_for_cwd(state_db, cwd)


def codex_status(process: ProcessInfo, state_db: Path = CODEX_STATE_DB) -> str | None:
    if _command_name(process.command) != "codex":
        return None
    # Re-resolve every poll (same reason as Claude): don't pin a stale rollout
    # for a pid that may move to a new session.
    rollout = _resolve_codex_rollout(process, state_db)
    if rollout is None:
        # Same as claude: a recognized codex with no readable rollout yet is
        # idle and waiting -- green, not "other".
        return STATUS_IDLE
    stale = _stale_status(rollout)
    if stale is not None:
        return stale
    return STATUS_BUSY if is_codex_mid_turn(rollout) else STATUS_IDLE


# ---------------------------------------------------------------------------
# Monitor — polls the focused window on a background thread and maps each tab
# to a status via the providers, in order. Provider-agnostic.
# ---------------------------------------------------------------------------

class AgentTabMonitor:
    def __init__(self, poll_interval: float = DEFAULT_POLL_INTERVAL):
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._slots: list[str | None] = [None] * NUM_SLOTS
        self._providers = [claude_status, codex_status]
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def slots(self) -> list[str | None]:
        with self._lock:
            return list(self._slots)

    def _loop(self) -> None:
        self._refresh()
        while True:
            time.sleep(self._poll_interval)
            self._refresh()

    def _slot_status(self, process: ProcessInfo) -> str:
        for provider in self._providers:
            status = provider(process)
            if status is not None:
                return status
        return STATUS_OTHER

    def _refresh(self) -> None:
        ttys = _focused_window_ttys()
        if not ttys:
            with self._lock:
                self._slots = [None] * NUM_SLOTS
            return
        foreground = _foreground_processes()
        new_slots: list[str | None] = [None] * NUM_SLOTS
        for idx, tty in enumerate(ttys[:NUM_SLOTS]):
            process = foreground.get(tty)
            if process is not None:
                new_slots[idx] = self._slot_status(process)
        with self._lock:
            self._slots = new_slots
