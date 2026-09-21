#!/usr/bin/env python3
"""Local, read-only terminal monitor.

The ``host`` process owns a normal child shell in a PTY.  Bytes typed by the
operator are sent only to that child shell.  Bytes written by the child are
shown unchanged on the operator's terminal and are also parsed into a VT
screen model before an event is sent to local ``view`` clients.

The monitor protocol deliberately has no INPUT message.  A viewer can read,
but it cannot write to the hosted terminal.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import pty
import selectors
import signal
import socket
import stat
import struct
import sys
import termios
import time
import tty
from typing import Any, Iterable

try:
    import pyte
except ImportError as exc:  # pragma: no cover - exercised by installation error
    raise SystemExit(
        "缺少 pyte。请先运行：sudo apt-get install -y python3-pyte"
    ) from exc


PROTOCOL_VERSION = 1
DEFAULT_SETTLE_MS = 250
PROMPT_MARKER_BEL = b"\x1b]133;A\x07"
PROMPT_MARKER_ST = b"\x1b]133;A\x1b\\"
MARKER_TAIL_SIZE = max(len(PROMPT_MARKER_BEL), len(PROMPT_MARKER_ST)) - 1
SESSION_RE = r"^[A-Za-z0-9._-]+$"


def fail(message: str, code: int = 1) -> "NoReturn":
    print(f"pty-mirror: {message}", file=sys.stderr)
    raise SystemExit(code)


def validate_session(name: str) -> str:
    import re

    if not re.fullmatch(SESSION_RE, name):
        fail("会话名只能包含字母、数字、点、下划线和短横线")
    return name


def private_runtime_dir() -> Path:
    """Return a per-user directory suitable for a local Unix socket."""

    configured = os.environ.get("XDG_RUNTIME_DIR")
    if configured:
        candidate = Path(configured)
        try:
            info = candidate.stat()
            if info.st_uid == os.getuid() and stat.S_ISDIR(info.st_mode):
                return candidate
        except OSError:
            pass

    candidate = Path(f"/tmp/pty-mirror-{os.getuid()}")
    candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(candidate, 0o700)
    return candidate


def socket_path(session: str) -> Path:
    return private_runtime_dir() / f"{validate_session(session)}.sock"


def terminal_size(fd: int) -> tuple[int, int]:
    try:
        raw = fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
        rows, columns, _, _ = struct.unpack("HHHH", raw)
        if rows > 0 and columns > 0:
            return rows, columns
    except OSError:
        pass
    return 24, 80


def set_pty_size(fd: int, rows: int, columns: int) -> None:
    payload = struct.pack("HHHH", rows, columns, 0, 0)
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, payload)
    except OSError:
        pass


def write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        try:
            written = os.write(fd, view)
            view = view[written:]
        except InterruptedError:
            continue


def json_bytes(message: dict[str, Any]) -> bytes:
    return (
        json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def strip_leading_command_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def current_shell_argv(command: list[str], script_dir: Path) -> tuple[list[str], bool]:
    """Build the hosted command and say whether the Bash prompt hook is used."""

    command = strip_leading_command_separator(command)
    if command:
        return command, False

    shell = os.environ.get("SHELL") or "/bin/bash"
    if Path(shell).name == "bash":
        rcfile = script_dir / "pty_mirror_bashrc"
        return [shell, "--rcfile", str(rcfile), "-i"], True
    return [shell, "-i"], False


def screen_lines(screen: Any) -> list[str]:
    # pyte returns a fresh list, but making that explicit keeps protocol state
    # independent of later terminal updates.
    return list(screen.display)


def cursor_info(screen: Any) -> dict[str, int]:
    return {"row": int(screen.cursor.y), "col": int(screen.cursor.x)}


def screen_delta(
    previous: list[str] | None,
    current: list[str],
    cursor: dict[str, int],
) -> dict[str, Any]:
    """Return the changed visible tail of two screen snapshots.

    For an ordinary shell, a command replaces the old prompt line and then
    adds output below it.  Returning the first changed row through the last
    changed/cursor row preserves that command as an append block.  The full
    current screen is included in every event as the authoritative state, so
    cursor-moving/full-screen programs remain recoverable even when their
    changes are not naturally append-only.
    """

    if previous is None:
        first = 0
    else:
        first = next(
            (index for index, (old, new) in enumerate(zip(previous, current)) if old != new),
            None,
        )
        if first is None:
            if len(previous) != len(current):
                first = min(len(previous), len(current))
            else:
                return {"from_row": None, "lines": [], "text": ""}

    changed_rows = [
        index
        for index in range(first, len(current))
        if previous is None
        or index >= len(previous)
        or previous[index] != current[index]
    ]
    last = max(max(changed_rows or [first]), cursor["row"])
    last = min(last, len(current) - 1)
    lines = current[first : last + 1]
    return {"from_row": first, "lines": lines, "text": "\n".join(lines)}


class Host:
    def __init__(
        self,
        session: str,
        command: list[str],
        cwd: str,
        settle_ms: int,
        script_dir: Path,
    ) -> None:
        self.session = validate_session(session)
        self.cwd = os.path.abspath(cwd)
        if not os.path.isdir(self.cwd):
            fail(f"工作目录不存在：{self.cwd}")
        self.settle_ms = max(20, settle_ms)
        self.argv, self.uses_prompt_marker = current_shell_argv(command, script_dir)

        self.selector = selectors.DefaultSelector()
        self.server: socket.socket | None = None
        self.clients: set[socket.socket] = set()
        self.master_fd: int | None = None
        self.child_pid: int | None = None
        self.child_status: int | None = None
        self.pty_eof = False
        self.running = True
        self.exit_sent = False
        self.resize_pending = True
        self.old_winch_handler: Any = None
        self.old_terminal_attributes: list[Any] | None = None

        self.rows = 24
        self.columns = 80
        self.screen: Any = None
        self.stream: Any = None
        self.marker_tail = b""
        self.display_marker_tail = b""
        self.marker_seen = False
        self.completion_ready = False
        self.pending_command = False
        self.first_output_seen = False
        self.last_output_at: float | None = None
        self.last_input_at: float | None = None

        self.seq = 0
        self.base_event: dict[str, Any] | None = None
        self.update_events: list[dict[str, Any]] = []
        self.previous_screen: list[str] | None = None

        self.sock_path = socket_path(self.session)

    def setup_socket(self) -> None:
        self.sock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.sock_path.parent, 0o700)
        if self.sock_path.exists():
            # A live socket means that the requested session name is in use.
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.15)
                probe.connect(str(self.sock_path))
            except (OSError, socket.timeout):
                try:
                    self.sock_path.unlink()
                except OSError as exc:
                    fail(f"无法清理旧 socket {self.sock_path}: {exc}")
            else:
                fail(f"会话已在运行：{self.session}")
            finally:
                probe.close()

        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.sock_path))
        os.chmod(self.sock_path, 0o600)
        self.server.listen(8)
        self.server.setblocking(False)
        self.selector.register(self.server, selectors.EVENT_READ, ("server", None))

    def spawn_child(self) -> None:
        try:
            self.rows, self.columns = terminal_size(sys.stdin.fileno())
        except OSError:
            self.rows, self.columns = 24, 80

        pid, master_fd = pty.fork()
        if pid == 0:  # child
            os.chdir(self.cwd)
            child_env = os.environ.copy()
            child_env["PTY_MIRROR_SESSION"] = self.session
            child_env["PTY_MIRROR_SOCKET"] = str(self.sock_path)
            try:
                os.execvpe(self.argv[0], self.argv, child_env)
            except OSError as exc:
                os.write(2, f"pty-mirror: 无法启动 {self.argv!r}: {exc}\n".encode())
                os._exit(127)

        self.child_pid = pid
        self.master_fd = master_fd
        os.set_blocking(master_fd, False)
        set_pty_size(master_fd, self.rows, self.columns)
        self.screen = pyte.HistoryScreen(
            self.columns,
            self.rows,
            history=max(1000, self.rows * 20),
        )
        self.stream = pyte.ByteStream(self.screen)
        self.selector.register(master_fd, selectors.EVENT_READ, ("pty", None))

    def set_operator_terminal_raw(self) -> None:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
        self.old_terminal_attributes = termios.tcgetattr(fd)
        tty.setraw(fd)

    def restore_operator_terminal(self) -> None:
        if self.old_terminal_attributes is not None:
            try:
                termios.tcsetattr(
                    sys.stdin.fileno(), termios.TCSADRAIN, self.old_terminal_attributes
                )
            except OSError:
                pass
            self.old_terminal_attributes = None

    def install_signal_handler(self) -> None:
        self.old_winch_handler = signal.getsignal(signal.SIGWINCH)
        signal.signal(signal.SIGWINCH, self._on_winch)

    def restore_signal_handler(self) -> None:
        if self.old_winch_handler is not None:
            signal.signal(signal.SIGWINCH, self.old_winch_handler)
            self.old_winch_handler = None

    def _on_winch(self, _signum: int, _frame: Any) -> None:
        self.resize_pending = True

    def snapshot(self) -> dict[str, Any]:
        lines = screen_lines(self.screen)
        return {
            "rows": self.rows,
            "cols": self.columns,
            "screen": lines,
            "cursor": cursor_info(self.screen),
        }

    def _event(self, event_type: str, reason: str) -> dict[str, Any]:
        self.seq += 1
        payload = self.snapshot()
        return {
            "v": PROTOCOL_VERSION,
            "type": event_type,
            "seq": self.seq,
            "session": self.session,
            "reason": reason,
            **payload,
        }

    def _send(self, client: socket.socket, message: dict[str, Any]) -> bool:
        try:
            client.settimeout(0.5)
            client.sendall(json_bytes(message))
            client.settimeout(None)
            return True
        except (BrokenPipeError, ConnectionResetError, OSError, socket.timeout):
            return False

    def _drop_dead_clients(self, dead: Iterable[socket.socket]) -> None:
        for client in dead:
            self.clients.discard(client)
            try:
                client.close()
            except OSError:
                pass

    def broadcast(self, message: dict[str, Any]) -> None:
        dead: list[socket.socket] = []
        for client in self.clients:
            if not self._send(client, message):
                dead.append(client)
        self._drop_dead_clients(dead)

    def resync_message(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "type": "resync",
            "seq": self.seq,
            "session": self.session,
            "base": self.base_event,
            "updates": self.update_events,
            **self.snapshot(),
        }

    def accept_clients(self) -> None:
        if self.server is None:
            return
        while True:
            try:
                client, _ = self.server.accept()
            except BlockingIOError:
                return
            except OSError:
                return
            client.setblocking(True)
            self.clients.add(client)
            hello = {
                "v": PROTOCOL_VERSION,
                "type": "hello",
                "session": self.session,
                "read_only": True,
                "input_supported": False,
            }
            if not self._send(client, hello):
                self._drop_dead_clients([client])
                continue
            if self.base_event is not None:
                if not self._send(client, self.resync_message()):
                    self._drop_dead_clients([client])

    def emit_base(self, reason: str) -> None:
        if self.base_event is not None:
            return
        event = self._event("base", reason)
        event["append"] = {
            "from_row": 0,
            "lines": list(event["screen"]),
            "text": "\n".join(event["screen"]),
        }
        self.base_event = event
        self.previous_screen = list(event["screen"])
        self.broadcast(event)

    def emit_update(self, reason: str) -> None:
        if self.screen is None:
            return
        if self.base_event is None:
            self.emit_base("implicit-before-update")
            return
        event = self._event("append", reason)
        event["append"] = screen_delta(
            self.previous_screen,
            event["screen"],
            event["cursor"],
        )
        self.previous_screen = list(event["screen"])
        self.update_events.append(event)
        self.broadcast(event)

    def process_output(self, data: bytes) -> None:
        if not data:
            return
        display_data = self.filter_prompt_markers(data)
        try:
            if display_data:
                write_all(sys.stdout.fileno(), display_data)
        except OSError:
            self.running = False
            return

        combined = self.marker_tail + data
        marker_found = (
            PROMPT_MARKER_BEL in combined or PROMPT_MARKER_ST in combined
        )
        self.marker_tail = combined[-MARKER_TAIL_SIZE:]
        if marker_found:
            self.marker_seen = True
            self.completion_ready = True

        try:
            self.stream.feed(data)
        except (UnicodeError, ValueError):
            # The child can emit a malformed byte sequence.  pyte's decoder
            # normally replaces it; keeping the host alive is more useful
            # than losing the operator's shell.
            pass

        self.first_output_seen = True
        self.last_output_at = time.monotonic()

    def filter_prompt_markers(self, data: bytes) -> bytes:
        """Hide only the markers injected by the Bash integration hook.

        The parser receives the markers, but the operator's terminal should
        remain visually indistinguishable from an ordinary Bash terminal.
        Prefixes split across PTY reads are held until the next read.
        """

        combined = self.display_marker_tail + data
        clean = combined.replace(PROMPT_MARKER_BEL, b"").replace(PROMPT_MARKER_ST, b"")
        hold = b""
        for marker in (PROMPT_MARKER_BEL, PROMPT_MARKER_ST):
            for length in range(1, len(marker)):
                prefix = marker[:length]
                if combined.endswith(prefix) and len(prefix) > len(hold):
                    hold = prefix
        if hold:
            clean = clean[:-len(hold)]
        self.display_marker_tail = hold
        return clean

    def process_operator_input(self) -> None:
        if self.master_fd is None:
            return
        try:
            data = os.read(sys.stdin.fileno(), 4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self.running = False
            return
        if not data:
            self.running = False
            return

        try:
            write_all(self.master_fd, data)
        except OSError:
            self.running = False
            return

        # In raw mode Enter arrives as CR or LF.  Ctrl-C also completes an
        # interactive command from the monitor's point of view.
        if b"\r" in data or b"\n" in data or b"\x03" in data:
            self.pending_command = True
            self.completion_ready = False
            self.last_input_at = time.monotonic()

    def apply_resize(self) -> None:
        if not self.resize_pending or self.master_fd is None or self.screen is None:
            return
        self.resize_pending = False
        try:
            rows, columns = terminal_size(sys.stdin.fileno())
        except OSError:
            return
        if rows == self.rows and columns == self.columns:
            return
        self.rows, self.columns = rows, columns
        set_pty_size(self.master_fd, rows, columns)
        self.screen.resize(lines=rows, columns=columns)
        if self.base_event is not None:
            # A resize is a screen replacement rather than an append.  The
            # authoritative full screen is still sent to every viewer.
            event = self._event("screen", "resize")
            event["append"] = {
                "from_row": None,
                "lines": [],
                "text": "",
            }
            self.previous_screen = list(event["screen"])
            self.update_events.append(event)
            self.broadcast(event)

    def maybe_emit(self, force: bool = False) -> None:
        now = time.monotonic()
        if self.base_event is None:
            if force or (
                self.first_output_seen
                and self.last_output_at is not None
                and now - self.last_output_at >= self.settle_ms / 1000
            ) or (
                self.completion_ready
                and self.first_output_seen
                and self.last_output_at is not None
                and now - self.last_output_at >= self.settle_ms / 1000
            ):
                self.emit_base("initial")
                self.completion_ready = False
            return

        if not self.pending_command:
            return

        # Bash launched by the default host command emits an invisible OSC
        # prompt marker only after the command is actually complete.  For an
        # explicitly supplied shell/program there may be no marker, so use a
        # quiet-period fallback.
        marker_done = (
            self.marker_seen
            and self.completion_ready
            and self.last_output_at is not None
            and now - self.last_output_at >= self.settle_ms / 1000
        )
        quiet_done = (
            not self.marker_seen
            and self.last_input_at is not None
            and now - self.last_input_at >= self.settle_ms / 1000
            and (
                self.last_output_at is None
                or now - self.last_output_at >= self.settle_ms / 1000
            )
        )
        if force or marker_done or quiet_done:
            self.emit_update("command-complete")
            self.pending_command = False
            self.completion_ready = False

    def poll_child(self) -> None:
        if self.child_pid is None or self.child_status is not None:
            return
        try:
            pid, status = os.waitpid(self.child_pid, os.WNOHANG)
        except ChildProcessError:
            self.child_status = 0
            return
        if pid == self.child_pid:
            self.child_status = status

    def finish(self) -> None:
        self.maybe_emit(force=True)
        if not self.exit_sent:
            code = 0
            if self.child_status is not None:
                if os.WIFEXITED(self.child_status):
                    code = os.WEXITSTATUS(self.child_status)
                elif os.WIFSIGNALED(self.child_status):
                    code = 128 + os.WTERMSIG(self.child_status)
            self.broadcast(
                {
                    "v": PROTOCOL_VERSION,
                    "type": "exit",
                    "session": self.session,
                    "code": code,
                }
            )
            self.exit_sent = True

        for client in list(self.clients):
            try:
                client.close()
            except OSError:
                pass
        self.clients.clear()
        try:
            self.selector.close()
        except OSError:
            pass
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass
        try:
            self.sock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
        self.restore_signal_handler()
        self.restore_operator_terminal()

    def run(self) -> int:
        self.setup_socket()
        self.spawn_child()
        self.set_operator_terminal_raw()
        if os.isatty(sys.stdin.fileno()):
            self.selector.register(sys.stdin, selectors.EVENT_READ, ("stdin", None))
        self.install_signal_handler()
        try:
            while self.running:
                self.apply_resize()
                self.poll_child()
                if self.pty_eof:
                    self.running = False
                    break

                timeout = 0.05
                if self.base_event is None and self.last_output_at is not None:
                    timeout = min(
                        timeout,
                        max(0.0, self.settle_ms / 1000 - (time.monotonic() - self.last_output_at)),
                    )
                try:
                    events = self.selector.select(timeout)
                except InterruptedError:
                    events = []
                for key, _mask in events:
                    kind, _value = key.data
                    if kind == "server":
                        self.accept_clients()
                    elif kind == "pty":
                        try:
                            data = os.read(self.master_fd, 65536)  # type: ignore[arg-type]
                        except (BlockingIOError, InterruptedError):
                            continue
                        except OSError as exc:
                            if exc.errno in (errno.EIO, errno.EBADF):
                                self.pty_eof = True
                                break
                            raise
                        if not data:
                            self.pty_eof = True
                            break
                        self.process_output(data)
                    elif kind == "stdin":
                        self.process_operator_input()
                self.maybe_emit()
                if self.child_status is not None and self.pty_eof:
                    self.running = False
        finally:
            self.finish()
        return 0


def compact_screen(lines: list[str], cursor: dict[str, int] | None = None) -> list[str]:
    """Remove only the empty rows around a screen for human-readable view."""

    last = -1
    for index, line in enumerate(lines):
        if line.rstrip(" "):
            last = index
    if cursor is not None:
        last = max(last, int(cursor.get("row", 0)))
    if last < 0:
        return []
    first = next((i for i, line in enumerate(lines[: last + 1]) if line.rstrip(" ")), 0)
    return lines[first : last + 1]


def human_lines(lines: list[str]) -> list[str]:
    # The JSON protocol keeps the complete fixed-width grid.  Human output
    # drops only terminal-cell padding so a viewer remains readable.
    return [line.rstrip(" ") for line in lines]


def print_append(message: dict[str, Any], compact: bool) -> None:
    append = message.get("append") or {}
    lines = list(append.get("lines") or [])
    if not lines:
        return
    if compact:
        lines = compact_screen(lines, message.get("cursor"))
    lines = human_lines(lines)
    if lines:
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()


def print_human(message: dict[str, Any], compact: bool) -> None:
    message_type = message.get("type")
    if message_type == "base":
        lines = list(message.get("screen") or [])
        if compact:
            lines = compact_screen(lines, message.get("cursor"))
        lines = human_lines(lines)
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
    elif message_type == "append":
        print_append(message, compact)
    elif message_type == "screen":
        lines = list(message.get("screen") or [])
        if compact:
            lines = compact_screen(lines, message.get("cursor"))
        lines = human_lines(lines)
        if lines:
            sys.stdout.write("\n".join(lines) + "\n")
            sys.stdout.flush()
    elif message_type == "resync":
        base = message.get("base")
        if isinstance(base, dict):
            print_human(base, compact)
        for update in message.get("updates") or []:
            if isinstance(update, dict):
                print_human(update, compact)
    elif message_type == "exit":
        sys.stdout.flush()


def run_view(session: str, json_mode: bool, once: bool, compact: bool) -> int:
    path = socket_path(session)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
    except OSError as exc:
        client.close()
        fail(f"连接会话 {session!r} 失败（{path}）：{exc}")

    got_state = False
    try:
        with client.makefile("rb") as stream:
            for raw in stream:
                try:
                    message = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if json_mode:
                    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)
                else:
                    print_human(message, compact)
                if message.get("type") in ("base", "resync"):
                    got_state = True
                    if once:
                        return 0
                if message.get("type") == "exit":
                    return int(message.get("code", 0) or 0)
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()
    return 0 if got_state else 1


def run_list() -> int:
    directory = private_runtime_dir()
    found = sorted(directory.glob("*.sock"))
    for path in found:
        print(path.stem)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="本机 PTY 终端只读监控：host 负责转发，view 只读接收。"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    host = sub.add_parser("host", help="在当前终端启动可操作的托管 shell")
    host.add_argument("--name", required=True, help="会话名，例如 main")
    host.add_argument("--cwd", default=os.getcwd(), help="子 shell 的工作目录")
    host.add_argument(
        "--settle-ms",
        type=int,
        default=DEFAULT_SETTLE_MS,
        help="无 OSC 终端的输出静默判定时间，默认 250ms",
    )
    host.add_argument("command", nargs=argparse.REMAINDER, help="可选：要托管的命令")

    view = sub.add_parser("view", help="只读查看 host 发来的内容")
    view.add_argument("--name", required=True, help="会话名，例如 main")
    view.add_argument("--json", action="store_true", help="输出原始 JSON 事件")
    view.add_argument("--once", action="store_true", help="收到当前完整状态后退出")
    view.add_argument(
        "--full-screen",
        action="store_true",
        help="保留屏幕上下所有空白行；默认只裁掉空白边缘",
    )

    sub.add_parser("list", help="列出当前用户的本地会话")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.mode == "host":
        script_dir = Path(__file__).resolve().parent
        return Host(
            args.name,
            args.command,
            args.cwd,
            args.settle_ms,
            script_dir,
        ).run()
    if args.mode == "view":
        return run_view(args.name, args.json, args.once, not args.full_screen)
    if args.mode == "list":
        return run_list()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
