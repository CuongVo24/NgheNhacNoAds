"""Spike S2: a full-screen prompt_toolkit TUI served by an embedded asyncssh server.

    serve   run the SSH server so a phone can connect (Ctrl+C to stop)
    local   run the same TUI directly in this terminal
    check   automated end-to-end check: asyncssh client + pyte terminal emulator

    uv run --group spikes python spikes/s2_ssh_tui.py serve --host 0.0.0.0 --pair
    uv run --group spikes python spikes/s2_ssh_tui.py check

Playback here is fake (a timer); the point is SSH, pairing, layout, keys, touch
and keeping several sessions in sync. Keys and state live in spikes/out/s2/.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import ipaddress
import itertools
import os
import secrets
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import asyncssh
from _common import OUT_DIR, Stopwatch, save_json, setup_console
from prompt_toolkit.application import Application, get_app
from prompt_toolkit.contrib.ssh import PromptToolkitSSHServer, PromptToolkitSSHSession
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.shortcuts import PromptSession, print_formatted_text
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

NARROW_COLS = 60
SIDE_WIDTH = 31


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    duration: int


TRACKS = [
    Track("Chúng Ta Của Tương Lai", "Sơn Tùng M-TP", 252),
    Track("Nơi Này Có Anh", "Sơn Tùng M-TP", 260),
    Track("Lạc Trôi", "Sơn Tùng M-TP", 233),
    Track("Bài Này Chill Phết", "Đen, MIN", 277),
    Track("Nàng Thơ", "Hoàng Dũng", 254),
    Track("Tháng Tư Là Lời Nói Dối Của Em", "Hà Anh Tuấn", 300),
    Track("夜に駆ける", "YOASOBI", 261),
    Track("Shape of You 🎵", "Ed Sheeran", 233),
]


# ---------------------------------------------------------------- shared state


class SharedPlayer:
    """Stands in for the daemon: one playback state shared by every session."""

    def __init__(self) -> None:
        self.index = 1
        self.position = 83
        self.playing = True
        self.volume = 70
        self.notice = ""
        self.sessions: dict[int, str] = {}
        self._session_ids = itertools.count(1)
        self._listeners: set[Callable[[], None]] = set()

    @property
    def track(self) -> Track:
        return TRACKS[self.index]

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def join(self, who: str) -> int:
        session_id = next(self._session_ids)
        self.sessions[session_id] = who
        self.changed(f"{who}: đã kết nối")
        return session_id

    def leave(self, session_id: int) -> None:
        who = self.sessions.pop(session_id, "?")
        self.changed(f"{who}: đã ngắt kết nối")

    def changed(self, notice: str | None = None) -> None:
        if notice is not None:
            self.notice = notice
        for listener in list(self._listeners):
            listener()

    def play(self, index: int, who: str) -> None:
        self.index, self.position, self.playing = index % len(TRACKS), 0, True
        self.changed(f"{who}: phát '{self.track.title}'")

    def toggle(self, who: str) -> None:
        self.playing = not self.playing
        self.changed(f"{who}: {'phát tiếp' if self.playing else 'tạm dừng'}")

    def seek(self, delta: int, who: str) -> None:
        self.position = max(0, min(self.track.duration - 1, self.position + delta))
        self.changed(f"{who}: tua {delta:+d}s")

    def seek_to(self, fraction: float, who: str) -> None:
        self.position = int(self.track.duration * max(0.0, min(fraction, 0.99)))
        self.changed(f"{who}: tua tới {fmt_time(self.position)}")

    def volume_by(self, delta: int, who: str) -> None:
        self.volume = max(0, min(100, self.volume + delta))
        self.changed(f"{who}: âm lượng {self.volume}")

    async def run(self) -> None:
        while True:
            await asyncio.sleep(1)
            if self.playing:
                self.position += 1
                if self.position >= self.track.duration:
                    self.index, self.position = (self.index + 1) % len(TRACKS), 0
                self.changed()


# --------------------------------------------------------------------- the TUI


def fmt_time(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def fit(text: str, width: int) -> str:
    """Cut or pad ``text`` to exactly ``width`` terminal cells."""
    if width <= 0:
        return ""
    used = get_cwidth(text)
    if used <= width:
        return text + " " * (width - used)
    out, used = [], 0
    for char in text:
        char_width = get_cwidth(char)
        if used + char_width > width - 1:
            break
        out.append(char)
        used += char_width
    return "".join(out) + "…" + " " * (width - 1 - used)


def color_depth_for(term: str) -> ColorDepth | None:
    term = term.lower()
    if any(tag in term for tag in ("truecolor", "24bit", "direct")):
        return ColorDepth.DEPTH_24_BIT
    if "256" in term:
        return ColorDepth.DEPTH_8_BIT
    if term in ("", "unknown", "dumb"):
        return ColorDepth.DEPTH_1_BIT
    return ColorDepth.DEPTH_4_BIT


@dataclass
class UiState:
    tab: int = 0
    selected: int = 0
    last_key: str = "-"
    clicks: int = 0


TABS = ["Tìm kiếm", "Hàng đợi", "Cài đặt"]
SHORT_TABS = ["Tìm", "Đợi", "Cài"]
STYLE = Style.from_dict(
    {
        "header": "bg:#1f2937 #e5e7eb",
        "tab.active": "bold reverse",
        "dim": "#9ca3af",
        "selected": "reverse",
        "np": "bold",
        "bar": "#22c55e",
        "button": "bold",
        "notice": "#fbbf24",
        "line": "#4b5563",
    }
)


def build_app(shared: SharedPlayer, who: str, term: str) -> Application[None]:
    ui = UiState()

    def cols() -> int:
        return get_app().output.get_size().columns

    def narrow() -> bool:
        return cols() < NARROW_COLS

    def clickable(action: Callable[[], None]) -> Callable[[MouseEvent], Any]:
        def handler(event: MouseEvent) -> Any:
            if event.event_type != MouseEventType.MOUSE_UP:
                return NotImplemented
            ui.clicks += 1
            action()
            return None

        return handler

    def set_tab(index: int) -> None:
        ui.tab = index % len(TABS)

    def click_track(index: int) -> None:
        if ui.selected == index:
            shared.play(index, who)
        else:
            ui.selected = index

    def header() -> StyleAndTextTuples:
        names = SHORT_TABS if narrow() else [f"{i + 1} {n}" for i, n in enumerate(TABS)]
        fragments: StyleAndTextTuples = []
        for i, name in enumerate(names):
            label = f"[{name}]" if i == ui.tab else f" {name} "
            style = "class:tab.active" if i == ui.tab else ""
            fragments.append((style, label, clickable(lambda i=i: set_tab(i))))
        right = f" {len(shared.sessions)} phiên "
        used = sum(get_cwidth(fragment[1]) for fragment in fragments)
        fragments.append(("", " " * max(1, cols() - used - get_cwidth(right))))
        fragments.append(("class:dim", right))
        return fragments

    def track_lines() -> StyleAndTextTuples:
        width = cols() if narrow() else cols() - SIDE_WIDTH - 1
        fragments: StyleAndTextTuples = []
        for i, track in enumerate(TRACKS):
            marker = "▸" if i == ui.selected else " "
            playing = "♪" if i == shared.index else " "  # ▶/‖ only appear in the now-playing bar
            style = "class:selected" if i == ui.selected else ""
            handler = clickable(lambda i=i: click_track(i))
            if narrow():
                fragments.append((style, fit(f" {marker}{playing} {track.title}", width), handler))
                fragments.append(("", "\n"))
                meta = f"     {track.artist} · {fmt_time(track.duration)}"
                fragments.append(("class:dim", fit(meta, width), handler))
            else:
                title_width = max(10, width - 5 - 22 - 6)
                line = (
                    f" {marker}{playing} {fit(track.title, title_width)} "
                    f"{fit(track.artist, 21)} {fmt_time(track.duration):>5}"
                )
                fragments.append((style, fit(line, width), handler))
            fragments.append(("", "\n"))
        return fragments

    def side_panel() -> StyleAndTextTuples:
        upcoming = [TRACKS[(shared.index + k) % len(TRACKS)] for k in (1, 2, 3)]
        lines = [" Tiếp theo", *(f"  {k} {t.title}" for k, t in enumerate(upcoming, 1))]
        lines += ["", " Phiên đang kết nối:", *(f"  • {s}" for s in shared.sessions.values())]
        return [("", "\n".join(fit(line, SIDE_WIDTH) for line in lines))]

    def now_playing() -> StyleAndTextTuples:
        track, width = shared.track, cols()
        icon = "▶" if shared.playing else "‖"
        if narrow():
            line = fit(f" {icon} {track.title}", width)
        else:
            right = f"vol {shared.volume}  "
            line = fit(f" {icon} {track.title} — {track.artist}", width - len(right)) + right
        current, total = fmt_time(shared.position), fmt_time(track.duration)
        bar_start = len(current) + 2
        bar_width = max(5, width - bar_start - len(total) - 2)
        filled = min(bar_width - 1, bar_width * shared.position // track.duration)
        bar = "━" * filled + "●" + "─" * (bar_width - filled - 1)

        def seek(event: MouseEvent) -> Any:
            if event.event_type != MouseEventType.MOUSE_UP:
                return NotImplemented
            ui.clicks += 1
            shared.seek_to((event.position.x - bar_start) / bar_width, who)
            return None

        return [
            ("class:np", line),
            ("", "\n"),
            ("", f" {current} "),
            ("class:bar", bar, seek),
            ("", f" {total}"),
        ]

    def controls() -> StyleAndTextTuples:
        buttons: list[tuple[str, Callable[[], None]]] = [
            ("[|<]", lambda: shared.play(shared.index - 1, who)),
            ("[ || ]" if shared.playing else "[ ▶  ]", lambda: shared.toggle(who)),
            ("[>|]", lambda: shared.play(shared.index + 1, who)),
            ("[-]", lambda: shared.volume_by(-5, who)),
            ("[+]", lambda: shared.volume_by(5, who)),
        ]
        fragments: StyleAndTextTuples = [("", "  ")]
        for label, action in buttons:
            fragments += [("class:button", label, clickable(action)), ("", "   ")]
        return fragments

    def status() -> StyleAndTextTuples:
        app = get_app()
        size = app.output.get_size()
        info = (
            f" {size.columns}x{size.rows} · TERM={term} · {app.color_depth.value}"
            f" · phím: {ui.last_key} · chạm: {ui.clicks}"
        )
        return [
            ("class:dim", fit(info, cols())),
            ("", "\n"),
            ("class:notice", fit(" " + shared.notice, cols())),
        ]

    list_window = Window(
        FormattedTextControl(
            track_lines,
            focusable=True,
            # The window scrolls to keep this "cursor" (the selected track) visible.
            get_cursor_position=lambda: Point(0, ui.selected * (2 if narrow() else 1)),
        )
    )
    wide_body = VSplit(
        [
            list_window,
            Window(width=1, char="│", style="class:line"),
            Window(FormattedTextControl(side_panel), width=SIDE_WIDTH),
        ]
    )
    root = HSplit(
        [
            Window(FormattedTextControl(header), height=1, style="class:header"),
            Window(height=1, char="─", style="class:line"),
            DynamicContainer(lambda: list_window if narrow() else wide_body),
            Window(height=1, char="─", style="class:line"),
            Window(FormattedTextControl(now_playing), height=2),
            Window(FormattedTextControl(controls), height=1),
            Window(FormattedTextControl(status), height=2),
        ]
    )

    kb = KeyBindings()

    Action = Callable[[KeyPressEvent], None]

    def bind(*keys: str | Keys) -> Callable[[Action], Action]:
        def decorator(action: Action) -> Action:
            @kb.add(*keys)
            def _(event: KeyPressEvent) -> None:
                key = event.key_sequence[0].key
                ui.last_key = str(getattr(key, "value", key))
                action(event)

            return action

        return decorator

    @bind("q")
    @bind("c-c")
    def _quit(event: KeyPressEvent) -> None:
        event.app.exit()

    bind(" ")(lambda e: shared.toggle(who))
    bind("n")(lambda e: shared.play(shared.index + 1, who))
    bind("p")(lambda e: shared.play(shared.index - 1, who))
    bind("left")(lambda e: shared.seek(-5, who))
    bind("right")(lambda e: shared.seek(5, who))
    bind(",")(lambda e: shared.seek(-30, who))
    bind(".")(lambda e: shared.seek(30, who))
    bind("+")(lambda e: shared.volume_by(5, who))
    bind("=")(lambda e: shared.volume_by(5, who))
    bind("-")(lambda e: shared.volume_by(-5, who))
    bind("enter")(lambda e: shared.play(ui.selected, who))
    bind("tab")(lambda e: set_tab(ui.tab + 1))
    for number in ("1", "2", "3"):
        bind(number)(lambda e, number=number: set_tab(int(number) - 1))

    def move(delta: int) -> None:
        ui.selected = (ui.selected + delta) % len(TRACKS)

    bind("up")(lambda e: move(-1))
    bind("k")(lambda e: move(-1))
    bind("down")(lambda e: move(1))
    bind("j")(lambda e: move(1))
    bind(Keys.Any)(lambda e: None)  # only records the key, to see what phone keyboards send

    return Application(
        layout=Layout(root, focused_element=list_window),
        key_bindings=kb,
        style=STYLE,
        full_screen=True,
        mouse_support=True,
        color_depth=color_depth_for(term),
    )


async def run_tui(shared: SharedPlayer, who: str, term: str) -> None:
    app = build_app(shared, who, term)
    unsubscribe = shared.subscribe(app.invalidate)
    session_id = shared.join(who)
    try:
        await app.run_async()
    finally:
        unsubscribe()
        shared.leave(session_id)


# ------------------------------------------------------------------ SSH server


def is_lan_address(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_private or ip.is_loopback or ip.is_link_local


class ServerState:
    """Host key, trusted keys and the pairing window (files in ``keys_dir``)."""

    def __init__(self, keys_dir: Path) -> None:
        self.keys_dir = keys_dir
        keys_dir.mkdir(parents=True, exist_ok=True)
        self.authorized_path = keys_dir / "authorized_keys"
        self.pairing_code: str | None = None
        self.pairing_until = 0.0
        self.pairing_busy = False
        self.log: list[str] = []

    def host_key(self) -> asyncssh.SSHKey:
        path = self.keys_dir / "host_ed25519_key"
        if not path.exists():
            asyncssh.generate_private_key("ssh-ed25519").write_private_key(path)
        return asyncssh.read_private_key(path)

    def trusted(self) -> dict[str, str]:
        """Map key fingerprint -> device name."""
        result: dict[str, str] = {}
        if not self.authorized_path.exists():
            return result
        for line in self.authorized_path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            # asyncssh only parses ASCII, and the comment holds a (Vietnamese) device name.
            key = asyncssh.import_public_key(" ".join(line.split()[:2]))
            parts = line.split(maxsplit=2)
            name = parts[2].split(":")[1] if len(parts) > 2 and ":" in parts[2] else "?"
            result[key.get_fingerprint("sha256")] = name
        return result

    def add_key(self, key: asyncssh.SSHKey, device: str) -> None:
        kind, blob = key.export_public_key("openssh").decode().split()[:2]
        device = "_".join(device.split()) or "device"
        stamp = dt.date.today().isoformat()
        with self.authorized_path.open("a", encoding="utf-8") as f:
            f.write(f"{kind} {blob} nghenhacnoads:{device}:{stamp}\n")

    def open_pairing(self, seconds: float) -> str:
        self.pairing_code = f"{secrets.randbelow(1_000_000):06d}"
        self.pairing_until = time.monotonic() + seconds
        return self.pairing_code

    def pairing_open(self) -> bool:
        return self.pairing_code is not None and time.monotonic() < self.pairing_until

    def close_pairing(self) -> None:
        self.pairing_code = None


class SpikeSSHServer(PromptToolkitSSHServer):
    """One instance per connection: public-key auth, LAN-only, pairing, then the TUI."""

    def __init__(self, state: ServerState, shared: SharedPlayer) -> None:
        super().__init__(self._interact, enable_cpr=False)
        self.state, self.shared = state, shared
        self.peer = "?"
        self.mode: str | None = None  # "trusted" | "pairing"
        self.key: asyncssh.SSHKey | None = None
        self.device: str | None = None

    def connection_made(self, conn: asyncssh.SSHServerConnection) -> None:
        peer = conn.get_extra_info("peername")
        self.peer = str(peer[0]) if peer else "?"

    def begin_auth(self, username: str) -> bool:
        return True  # the default in PromptToolkitSSHServer is False: no auth at all

    def password_auth_supported(self) -> bool:
        return False

    def kbdint_auth_supported(self) -> bool:
        return False

    def public_key_auth_supported(self) -> bool:
        return True

    def validate_public_key(self, username: str, key: asyncssh.SSHKey) -> bool:
        if not is_lan_address(self.peer):
            self.state.log.append(f"từ chối IP ngoài LAN {self.peer}")
            return False
        fingerprint = key.get_fingerprint("sha256")
        trusted = self.state.trusted()
        if fingerprint in trusted:
            self.mode, self.key, self.device = "trusted", key, trusted[fingerprint]
            return True
        if self.state.pairing_open():
            self.mode, self.key = "pairing", key
            return True
        self.state.log.append(f"từ chối khóa lạ {fingerprint} từ {self.peer}")
        return False

    async def _interact(self, session: PromptToolkitSSHSession) -> None:
        term = session._chan.get_terminal_type() or "unknown"  # type: ignore[union-attr]
        if self.mode == "pairing" and not await self._pair():
            return
        await run_tui(self.shared, f"{self.device} ({self.peer})", term)

    async def _pair(self) -> bool:
        state = self.state
        if state.pairing_busy:
            print_formatted_text("Đang có thiết bị khác ghép đôi, hãy thử lại sau.")
            return False
        state.pairing_busy = True
        try:
            assert self.key is not None
            print_formatted_text("Thiết bị này chưa được ghép đôi với laptop.")
            print_formatted_text(f"Khóa của bạn: {self.key.get_fingerprint('sha256')}")
            prompt: PromptSession[str] = PromptSession()
            for _attempt in range(3):
                code = await prompt.prompt_async("Nhập mã ghép đôi hiện trên laptop: ")
                if not state.pairing_open():
                    print_formatted_text("Cửa sổ ghép đôi đã hết hạn.")
                    return False
                expected = (state.pairing_code or "").encode()
                if secrets.compare_digest(code.strip().encode(), expected):
                    name = (await prompt.prompt_async("Đặt tên cho thiết bị này: ")).strip()
                    self.device = name or "Điện thoại"
                    state.add_key(self.key, self.device)
                    state.close_pairing()
                    state.log.append(f"đã ghép đôi {self.device} từ {self.peer}")
                    return True
                print_formatted_text("Sai mã.")
            state.close_pairing()
            print_formatted_text("Sai quá 3 lần, đã đóng cửa sổ ghép đôi.")
            return False
        finally:
            state.pairing_busy = False


async def start_server(
    state: ServerState, shared: SharedPlayer, host: str, port: int
) -> asyncssh.SSHAcceptor:
    return await asyncssh.create_server(
        lambda: SpikeSSHServer(state, shared),
        host,
        port,
        server_host_keys=[state.host_key()],
        login_timeout=30,
        keepalive_interval=30,
        keepalive_count_max=3,
        agent_forwarding=False,
        allow_scp=False,
    )


def lan_ipv4_addresses() -> list[str]:
    found = {info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the default route
        found.add(s.getsockname()[0])
    return sorted(ip for ip in found if is_lan_address(ip) and not ip.startswith("127."))


async def serve(args: argparse.Namespace) -> None:
    state = ServerState(OUT_DIR / "s2")
    shared = SharedPlayer()
    server = await start_server(state, shared, args.host, args.port)
    ticker = asyncio.create_task(shared.run())
    print(f"SSH server đang nghe {args.host}:{args.port}")
    print(f"Host key: {state.host_key().get_fingerprint('sha256')}")
    for ip in lan_ipv4_addresses() if args.host in ("0.0.0.0", "::") else [args.host]:
        print(f"  ssh -p {args.port} yt@{ip}")
    if args.pair:
        code = state.open_pairing(args.pair_seconds)
        print(f"Mã ghép đôi: {code} (hết hạn sau {args.pair_seconds:.0f} giây)")
    print("Nhấn Ctrl+C để dừng.")
    try:
        await server.wait_closed()
    finally:
        ticker.cancel()


async def local() -> None:
    shared = SharedPlayer()
    ticker = asyncio.create_task(shared.run())
    try:
        await run_tui(shared, "laptop (local)", os.environ.get("TERM", "windows-console"))
    finally:
        ticker.cancel()


# -------------------------------------------------------------- automated check


class Terminal:
    """An SSH session with a PTY, rendered through the pyte terminal emulator."""

    def __init__(self, conn: asyncssh.SSHClientConnection, cols: int, rows: int) -> None:
        import pyte

        self.conn, self.cols, self.rows = conn, cols, rows
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.updated = asyncio.Event()
        self.proc: asyncssh.SSHClientProcess[bytes] | None = None
        self._pump_task: asyncio.Task[None] | None = None

    async def open(self, term: str = "xterm-256color") -> None:
        self.proc = await self.conn.create_process(
            term_type=term, term_size=(self.cols, self.rows), encoding=None
        )
        self._pump_task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        assert self.proc is not None
        while data := await self.proc.stdout.read(65536):
            self.stream.feed(data)
            self.updated.set()
        self.updated.set()

    def lines(self) -> list[str]:
        return [line.rstrip() for line in self.screen.display]

    def text(self) -> str:
        return "\n".join(self.lines())

    def send(self, data: str) -> None:
        assert self.proc is not None
        self.proc.stdin.write(data.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        assert self.proc is not None
        self.cols, self.rows = cols, rows
        self.proc.change_terminal_size(cols, rows)
        self.screen.resize(rows, cols)

    def find(self, needle: str) -> tuple[int, int] | None:
        """Return the (column, row) cell where ``needle`` starts, 0-based."""
        for row in range(self.rows):
            columns, chars = [], []
            for col in range(self.cols):
                data = self.screen.buffer[row][col].data
                if data:  # an empty cell follows a double-width character
                    columns.append(col)
                    chars.append(data)
            index = "".join(chars).find(needle)
            if index >= 0:
                return columns[index], row
        return None

    def click(self, col: int, row: int) -> None:
        x, y = col + 1, row + 1  # SGR mouse coordinates are 1-based
        self.send(f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m")

    async def wait_for(self, condition: str | Callable[[str], bool], timeout: float = 8) -> int:
        """Wait until the screen matches; return how long it took in ms."""
        check = (lambda t: condition in t) if isinstance(condition, str) else condition
        sw = Stopwatch()
        deadline = time.monotonic() + timeout
        while not check(self.text()):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for {condition!r}; screen:\n{self.text()}")
            self.updated.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.updated.wait(), remaining)
        return sw.ms()

    async def closed(self, timeout: float = 5) -> bool:
        assert self.proc is not None
        try:
            await asyncio.wait_for(self.proc.wait_closed(), timeout)
        except TimeoutError:
            return False
        return True


def selected_title(term: Terminal) -> str | None:
    for line in term.lines():
        if "▸" in line:
            for track in TRACKS:
                if track.title in line:
                    return track.title
    return None


async def check() -> dict[str, Any]:
    keys_dir = OUT_DIR / "s2-check"
    shutil.rmtree(keys_dir, ignore_errors=True)
    state, shared = ServerState(keys_dir), SharedPlayer()
    server = await start_server(state, shared, "127.0.0.1", 0)
    port = server.get_port()
    ticker = asyncio.create_task(shared.run())
    phone_key = asyncssh.generate_private_key("ssh-ed25519")
    results: dict[str, Any] = {}
    connections: list[asyncssh.SSHClientConnection] = []

    async def connect(key: asyncssh.SSHKey) -> asyncssh.SSHClientConnection:
        conn = await asyncssh.connect(
            "127.0.0.1",
            port,
            username="yt",
            client_keys=[key],
            known_hosts=None,
            agent_path=None,
            preferred_auth="publickey",
        )
        connections.append(conn)
        return conn

    def step(name: str, value: Any) -> None:
        results[name] = value
        print(f"  {name}: {value}")

    try:
        # 1. Unknown key while pairing is closed.
        try:
            await connect(phone_key)
            step("khóa lạ bị từ chối khi chưa mở ghép đôi", False)
        except asyncssh.PermissionDenied:
            step("khóa lạ bị từ chối khi chưa mở ghép đôi", True)

        # 2. Pairing: one wrong code, then the right one.
        code = state.open_pairing(60)
        conn = await connect(phone_key)
        phone = Terminal(conn, 100, 30)
        await phone.open()
        await phone.wait_for("Nhập mã ghép đôi")
        phone.send(("111111" if code != "111111" else "222222") + "\r")
        await phone.wait_for("Sai mã")
        phone.send(code + "\r")
        await phone.wait_for("Đặt tên cho thiết bị")
        phone.send("Điện thoại test\r")
        ms = await phone.wait_for(lambda t: "Nơi Này Có Anh" in t and "1 Tìm kiếm" in t)
        step("ghép đôi thành công, vào TUI sau (ms)", ms)
        step("số khóa trong authorized_keys", len(state.trusted()))
        results["screen_wide"] = phone.text()

        # 3. Keyboard: move selection, play, pause, volume.
        phone.send("jj")
        await phone.wait_for(lambda t: selected_title(phone) == TRACKS[2].title)
        phone.send("\r")
        await phone.wait_for(f"▶ {TRACKS[2].title}")
        phone.send(" ")
        await phone.wait_for(f"‖ {TRACKS[2].title}")
        phone.send("+")
        await phone.wait_for("vol 75")
        step("phím j/Enter/Space/+ hoạt động", True)

        # 4. A second session (laptop) sees changes made on the phone.
        laptop = Terminal(await connect(phone_key), 100, 30)
        await laptop.open()
        await laptop.wait_for("2 phiên")
        phone.send(" ")
        step("đồng bộ sang phiên thứ hai (ms)", await laptop.wait_for(f"▶ {TRACKS[2].title}"))

        # 5. Resize to a phone portrait width.
        phone.resize(42, 20)
        await phone.wait_for("[Tìm]")
        results["screen_narrow"] = phone.text()
        step("đổi sang bố cục điện thoại khi còn 42 cột", True)

        # 6. Touch: tap a track to select it, tap again to play it.
        target = TRACKS[3].title
        position = phone.find(target)
        assert position is not None, "track not visible on the narrow screen"
        phone.click(*position)
        await phone.wait_for(lambda t: selected_title(phone) == target)
        phone.click(*position)
        await phone.wait_for(f"▶ {target}")
        button = phone.find("[>|]")
        assert button is not None
        phone.click(*button)
        await phone.wait_for(f"▶ {TRACKS[4].title}")
        step("chạm chọn bài, chạm lần nữa để phát, nút [>|]", True)

        # 7. Non-interactive commands are refused.
        try:
            result = await conn.run("dir", check=False, timeout=5)
            step("lệnh exec bị từ chối", f"exit={result.exit_status}")
        except Exception as exc:
            step("lệnh exec bị từ chối", type(exc).__name__)

        # 8. q closes only that session.
        phone.send("q")
        step("q đóng phiên", await phone.closed())
        await laptop.wait_for("1 phiên")
        step("phiên còn lại vẫn chạy", True)
        laptop.send("q")
        await laptop.closed()
        conn.close()

        # 9. Reconnect with the paired key: straight into the TUI.
        again = Terminal(await connect(phone_key), 80, 24)
        await again.open()
        step("kết nối lại không cần ghép đôi (ms)", await again.wait_for("Nơi Này Có Anh"))
        again.send("q")
        await again.closed()

        step(
            "lọc địa chỉ LAN",
            {ip: is_lan_address(ip) for ip in ("192.168.1.5", "10.0.0.2", "127.0.0.1", "8.8.8.8")},
        )
        results["server_log"] = state.log
    except Exception as exc:
        results["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  LỖI: {results['error']}")
    finally:
        ticker.cancel()
        for conn in connections:
            conn.close()
        server.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), 5)
    return results


def main() -> None:
    setup_console()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p_serve = sub.add_parser("serve", help="run the SSH server")
    p_serve.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to accept LAN connections")
    p_serve.add_argument("--port", type=int, default=2222)
    p_serve.add_argument("--pair", action="store_true", help="open a pairing window at start")
    p_serve.add_argument("--pair-seconds", type=float, default=120)
    sub.add_parser("local", help="run the TUI in this terminal")
    sub.add_parser("check", help="automated end-to-end check")
    args = parser.parse_args()

    if args.command == "serve":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(serve(args))
    elif args.command == "local":
        asyncio.run(local())
    else:
        print("S2 check:")
        results = asyncio.run(check())
        path = save_json("s2_check.json", results)
        screens = OUT_DIR / "s2_screens.txt"
        screens.write_text(
            "=== 100x30 ===\n"
            + results.get("screen_wide", "(không có)")
            + "\n\n=== 42x20 ===\n"
            + results.get("screen_narrow", "(không có)")
            + "\n",
            encoding="utf-8",
        )
        print(f"Đã lưu: {path} và {screens}")
        if "error" in results:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
