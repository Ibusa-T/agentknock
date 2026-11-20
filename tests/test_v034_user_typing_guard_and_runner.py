# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
v0.3.4 Phase 1-B Tests: User-Typing Collision Guard (CONIN$ injection skip
when the human owner is typing).

Contract under test (Phase 1-B, long-pause-edge revision 2026-11-19):

L0 `agentknock.adapters._win_console_ctypes`:
    `inject_console_input_verified(
        target_pid, expected_start_time, text="進めて",
        send_enter=True, submit_delay_sec=0.08,
        *, allowed_images=None, respect_user_typing=True
    ) -> (ok: bool, reason: str, win32_last_error: int)`

    `respect_user_typing: bool = True` kwarg activates a two-phase check
    between `CreateFileW(CONIN$)` (Step 5 open) and the first `WriteConsoleInputW`:

      Check 1 (OR trigger): `GetNumberOfConsoleInputEvents(h_conin)` > 0 and
        `PeekConsoleInputW` observes any `EventType == KEY_EVENT` with
        `bKeyDown == True`.
      Check 2 (B-ONLY trigger as of the 2026-11-19 long-pause revision — see
      `agent_docs/think/agentknock_v034_typing_guard_longpause_spec.md` §9):
        `ReadConsoleOutputCharacterW` over the current cursor-line from column
        0 to current X contains at least one "visible" character, where visible
        means `ord(ch) > 0x20 and ord(ch) != 0x7F`. This covers both ASCII
        printable (0x21-0x7E) AND non-ASCII code points ≥ 0x80 — hiragana
        (e.g. 「あ」 U+3042), CJK ideographs, accented Latin (é), emojis, etc.
        Space (0x20), C0 controls (< 0x20) and DEL (0x7F) are NOT content.
        Cursor movement across the 150ms sample window is NOT required — a
        user pausing mid-sentence (stationary cursor, non-empty buffer) now
        defers too, and this works equally for ASCII and non-ASCII buffers.
        True idle (X==0, or whitespace/control-only line contents) must still
        inject normally; otherwise wakes would be permanently locked out.

    Guard outcomes:
      - typing/pausing detected → retry up to 2× (0.4s interval); if still
        typing → return `(False, "deferred_user_typing", 0)` WITHOUT calling
        `WriteConsoleInputW` (handles & console attach released cleanly).
      - detection API returns Win32 failure → return
        `(False, "user_typing_check_failed", GetLastError())`.
      - `respect_user_typing=False` → guard disabled; NONE of the detection
        APIs are called; v0.3.3 behavior preserved.

Adapter `agentknock.adapters.agy.AgyDeliveryAdapter`:
    `__init__(..., respect_user_typing: bool = True)` is wired to L0 through
    `_inject`. `wake()` result sets `last_wake_detail["reason"]` to the L0
    reason (`"deferred_user_typing"` / `"user_typing_check_failed"` / etc.).

Non-regression: 1-C locale work is OUT OF SCOPE here — `DEFAULT_LOCALE == "en"`
and `WAKE_NUDGE_CATALOG` are not inspected by this module.
"""
from __future__ import annotations

import ctypes
import inspect
import sys
from ctypes import wintypes
from typing import Any, Optional

import pytest

from agentknock.adapters import _win_console_ctypes as wc
from agentknock.adapters.agy import AgyDeliveryAdapter


# ---------------------------------------------------------------------------
# Local struct definitions for the Win32 console screen buffer. These match
# the SDK layouts (COORD / SMALL_RECT / CONSOLE_SCREEN_BUFFER_INFO) exactly,
# so `ctypes.cast` of whatever pointer the production code hands to the fake
# overlays correctly regardless of which private type name the production
# module uses for the same layout.
# ---------------------------------------------------------------------------
class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", ctypes.c_short),
        ("Top", ctypes.c_short),
        ("Right", ctypes.c_short),
        ("Bottom", ctypes.c_short),
    ]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


_VALID_FT: int = (0x01DC0000 << 32) | 0x12345678
_CONIN_HANDLE: int = 8001
_CONOUT_HANDLE: int = 8002
_PROC_HANDLE: int = 7001


# ---------------------------------------------------------------------------
# Programmable fake kernel32 with all the APIs the guard touches.
#
# Behavior is driven by a per-attempt script so we can simulate "typing on the
# first probe, idle on the retry" and other time-dependent paths without
# actually sleeping.
# ---------------------------------------------------------------------------
class _FakeKernel32Typing:
    """
    Fake `_kernel32` exposing every Win32 method the Phase 1-B guard touches.

    The user-typing probes (`GetNumberOfConsoleInputEvents`, `PeekConsoleInputW`,
    `GetConsoleScreenBufferInfo`, `ReadConsoleOutputCharacterW`) are driven by
    scripted per-probe outcomes so tests can stage a "typing → idle" sequence
    across retries.
    """

    def __init__(
        self,
        *,
        # Static L0 path (identical to v0.3.3 test doubles)
        creation_ft: int = _VALID_FT,
        image_path: str = r"C:\Program Files\Antigravity\agy.exe",
        # Per-probe scripts: each list element is one probe outcome.
        # Check 1:
        key_event_queue_sizes: Optional[list[int]] = None,  # value for GetNumberOfConsoleInputEvents
        peeked_key_down: Optional[list[bool]] = None,  # whether PeekConsoleInputW reports a KEY_EVENT down
        peek_fail_on_probe: Optional[int] = None,  # 1-indexed probe number that fails
        peek_fail_err: int = 1455,
        # Check 2A (cursor-move pair per probe): list of 2-tuples
        # Each probe reads CSBI twice (150ms apart); value is
        # ((x1,y1),(x2,y2)) -- different → moved.
        cursor_pairs: Optional[list[tuple[tuple[int, int], tuple[int, int]]]] = None,
        # Check 2B: non-empty line content per probe
        line_contents: Optional[list[str]] = None,
        gcsbi_fail_on_probe: Optional[int] = None,
        gcsbi_fail_err: int = 6,
    ) -> None:
        self.events: list[str] = []
        self.creation_ft = creation_ft
        self.image_path = image_path

        self._key_event_queue_sizes = key_event_queue_sizes or [0]
        self._peeked_key_down = peeked_key_down or [False]
        self._peek_fail_on_probe = peek_fail_on_probe
        self._peek_fail_err = peek_fail_err

        self._cursor_pairs = cursor_pairs or [((0, 0), (0, 0))]
        self._line_contents = line_contents or [""]
        self._gcsbi_fail_on_probe = gcsbi_fail_on_probe
        self._gcsbi_fail_err = gcsbi_fail_err

        self.write_calls = 0
        # Probe counters are 1-indexed to match human-readable "the 2nd probe".
        self._probe_check1 = 0
        self._probe_check2a = 0  # Each probe increments this by 1; two CSBI reads share the probe index.
        self._csbi_reads_in_probe = 0
        self._probe_check2b = 0

    # --------- core v0.3.3 methods (unchanged) ---------

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        assert access == (wc.PROCESS_QUERY_LIMITED_INFORMATION | wc.SYNCHRONIZE)
        self.events.append(f"OpenProcess:{pid}")
        return _PROC_HANDLE

    def WaitForSingleObject(self, h: int, timeout: int) -> int:
        self.events.append(f"WaitForSingleObject:{h}")
        return wc.WAIT_TIMEOUT

    def GetProcessTimes(self, h: int, c_ptr, e_ptr, k_ptr, u_ptr) -> int:
        self.events.append(f"GetProcessTimes:{h}")
        c_ft = ctypes.cast(c_ptr, ctypes.POINTER(wc.FILETIME)).contents
        c_ft.dwHighDateTime = (self.creation_ft >> 32) & 0xFFFFFFFF
        c_ft.dwLowDateTime = self.creation_ft & 0xFFFFFFFF
        return 1

    def QueryFullProcessImageNameW(self, h: int, flags: int, buf, size_ptr) -> int:
        self.events.append(f"QueryFullProcessImageNameW:{h}")
        for idx, ch in enumerate(self.image_path):
            buf[idx] = ch
        buf[len(self.image_path)] = "\0"
        ctypes.cast(size_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(self.image_path)
        return 1

    def FreeConsole(self) -> int:
        self.events.append("FreeConsole")
        return 1

    def AttachConsole(self, pid: int) -> int:
        if pid == wc.ATTACH_PARENT_PROCESS:
            self.events.append("AttachConsole:PARENT")
        else:
            self.events.append(f"AttachConsole:{pid}")
        return 1

    def CreateFileW(self, name: str, access: int, share: int, sec, disp: int, flags: int, tmpl) -> int:
        self.events.append(f"CreateFileW:{name}")
        if name == "CONIN$":
            return _CONIN_HANDLE
        if name == "CONOUT$":
            return _CONOUT_HANDLE
        return _CONIN_HANDLE  # fallback

    def WriteConsoleInputW(self, h: int, recs, length: int, written_ptr) -> int:
        self.write_calls += 1
        self.events.append(f"WriteConsoleInputW:{length}")
        ctypes.cast(written_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = length
        return 1

    def CloseHandle(self, h: int) -> int:
        self.events.append(f"CloseHandle:{h}")
        return 1

    # --------- new Phase 1-B detection seams ---------

    def GetNumberOfConsoleInputEvents(self, h: int, num_ptr) -> int:
        self.events.append(f"GetNumberOfConsoleInputEvents:{h}")
        idx = self._probe_check1  # 0-indexed access after increment below
        sz = self._key_event_queue_sizes[min(idx, len(self._key_event_queue_sizes) - 1)]
        ctypes.cast(num_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = int(sz)
        return 1

    def PeekConsoleInputW(self, h: int, buf, n: int, read_ptr) -> int:
        self._probe_check1 += 1
        probe = self._probe_check1
        self.events.append(f"PeekConsoleInputW:{h}:{n}")
        if self._peek_fail_on_probe is not None and probe == self._peek_fail_on_probe:
            ctypes.set_last_error(self._peek_fail_err)
            return 0
        has_key = self._peeked_key_down[min(probe - 1, len(self._peeked_key_down) - 1)]
        if has_key and n >= 1:
            # Populate one INPUT_RECORD with a KEY_EVENT down. ctypes delivers
            # `buf` as an INPUT_RECORD array; direct field assignment is
            # equivalent to what the production code's cast would do.
            try:
                buf[0].EventType = wc.KEY_EVENT
                buf[0].Event.KeyEvent.bKeyDown = 1
                buf[0].Event.KeyEvent.wRepeatCount = 1
                buf[0].Event.KeyEvent.wVirtualKeyCode = 0x41  # 'A'
                buf[0].Event.KeyEvent.wVirtualScanCode = 0
                buf[0].Event.KeyEvent.uChar.UnicodeChar = "A"
                buf[0].Event.KeyEvent.dwControlKeyState = 0
            except Exception:
                pass
            ctypes.cast(read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = 1
        else:
            ctypes.cast(read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = 0
        return 1

    def GetConsoleScreenBufferInfo(self, h: int, csbi_ptr) -> int:
        # Each probe reads CSBI twice (before/after 150ms). Group two calls per
        # probe index.
        if self._csbi_reads_in_probe == 0:
            self._probe_check2a += 1
            probe = self._probe_check2a
            self.events.append(f"GetConsoleScreenBufferInfo:{h}:probe{probe}:sample1")
        else:
            probe = self._probe_check2a
            self.events.append(f"GetConsoleScreenBufferInfo:{h}:probe{probe}:sample2")

        if self._gcsbi_fail_on_probe is not None and probe == self._gcsbi_fail_on_probe:
            ctypes.set_last_error(self._gcsbi_fail_err)
            self._csbi_reads_in_probe = (self._csbi_reads_in_probe + 1) % 2
            return 0

        pair = self._cursor_pairs[min(probe - 1, len(self._cursor_pairs) - 1)]
        pos = pair[self._csbi_reads_in_probe]
        csbi = ctypes.cast(csbi_ptr, ctypes.POINTER(_CONSOLE_SCREEN_BUFFER_INFO)).contents
        csbi.dwSize.X = 120
        csbi.dwSize.Y = 30
        csbi.dwCursorPosition.X = pos[0]
        csbi.dwCursorPosition.Y = pos[1]
        csbi.wAttributes = 0x07
        csbi.srWindow.Left = 0
        csbi.srWindow.Top = 0
        csbi.srWindow.Right = 119
        csbi.srWindow.Bottom = 29
        csbi.dwMaximumWindowSize.X = 120
        csbi.dwMaximumWindowSize.Y = 30

        self._csbi_reads_in_probe = (self._csbi_reads_in_probe + 1) % 2
        return 1

    def ReadConsoleOutputCharacterW(self, h: int, buf, n: int, coord, read_ptr) -> int:
        self._probe_check2b += 1
        probe = self._probe_check2b
        # `coord` arrives either as a struct or as a tuple depending on how the
        # production code set up the ctypes argtype; we only log membership.
        self.events.append(f"ReadConsoleOutputCharacterW:{h}:probe{probe}:n{n}")
        line = self._line_contents[min(probe - 1, len(self._line_contents) - 1)]
        line = line[:n]
        for i, ch in enumerate(line):
            buf[i] = ch
        if len(line) < n:
            try:
                buf[len(line)] = "\0"
            except Exception:
                pass
        ctypes.cast(read_ptr, ctypes.POINTER(wintypes.DWORD)).contents.value = len(line)
        return 1


class _FakeUser32:
    def MapVirtualKeyW(self, vk_code: int, map_type: int) -> int:
        return 0x1C


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make retry/CSBI-gap sleeps instant so tests stay fast."""
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)


# ===========================================================================
# § A — Contract: new kwarg exists, is keyword-only, and defaults True
# ===========================================================================

def test_inject_console_input_verified_has_respect_user_typing_kwarg_defaulting_true():
    sig = inspect.signature(wc.inject_console_input_verified)
    assert "respect_user_typing" in sig.parameters, (
        "Phase 1-B contract: inject_console_input_verified must expose a "
        "`respect_user_typing` kwarg."
    )
    param = sig.parameters["respect_user_typing"]
    assert param.default is True, (
        "Phase 1-B contract: `respect_user_typing` default must be True "
        "(guard is on by default)."
    )
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "Phase 1-B contract: `respect_user_typing` must be keyword-only so "
        "positional call-sites remain forward-compatible."
    )


def test_agy_delivery_adapter_accepts_respect_user_typing_ctor_kwarg():
    adapter = AgyDeliveryAdapter(respect_user_typing=False)
    assert getattr(adapter, "respect_user_typing", True) is False, (
        "Phase 1-B contract: AgyDeliveryAdapter must store "
        "`respect_user_typing` on the instance so the adapter-layer can "
        "plumb it to L0 and so tests can disable the guard for regression "
        "checks."
    )


# ===========================================================================
# § B — respect_user_typing=False: no detection APIs called, legacy path
# ===========================================================================

def test_respect_user_typing_false_never_calls_detection_apis(monkeypatch, no_sleep):
    """
    When `respect_user_typing=False`, the guard is OFF and NONE of the four
    detection APIs may appear in the event trace. v0.3.3 call pattern must be
    byte-identical to today.
    """
    fake = _FakeKernel32Typing()
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
        respect_user_typing=False,
    )
    assert (ok, reason, err) == (True, "ok", 0)

    detection_apis = {
        "GetNumberOfConsoleInputEvents",
        "PeekConsoleInputW",
        "GetConsoleScreenBufferInfo",
        "ReadConsoleOutputCharacterW",
    }
    for ev in fake.events:
        head = ev.split(":", 1)[0]
        assert head not in detection_apis, (
            f"guard-off path must not touch {head}; got event {ev!r} in "
            f"{fake.events!r}"
        )
    # And WriteConsoleInputW must still have been invoked (text + Enter).
    assert fake.write_calls == 2


# ===========================================================================
# § C — Check 1: unprocessed KEY_EVENT → defer
# ===========================================================================

def test_unprocessed_key_event_defers_without_writing(monkeypatch, no_sleep):
    """
    Check 1 (`GetNumberOfConsoleInputEvents > 0` ∧ PeekConsoleInputW observes
    a key-down event) must defer. `WriteConsoleInputW` is NEVER called, handles
    and attach are restored, return is `(False, "deferred_user_typing", 0)`.
    """
    # Script: every probe reports 1 queued key event and PeekConsoleInputW
    # consistently returns a key-down record. Retries do not clear it.
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[1, 1, 1],
        peeked_key_down=[True, True, True],
        cursor_pairs=[((0, 0), (0, 0)), ((0, 0), (0, 0)), ((0, 0), (0, 0))],
        line_contents=["", "", ""],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
        # respect_user_typing default (True)
    )
    assert (ok, reason, err) == (False, "deferred_user_typing", 0)
    assert fake.write_calls == 0, (
        "deferred_user_typing must NEVER call WriteConsoleInputW; events="
        f"{fake.events!r}"
    )
    # Housekeeping: process handle MUST still be closed and we MUST have
    # re-attached to the parent console.
    assert any(e.startswith("AttachConsole:PARENT") for e in fake.events)
    assert f"CloseHandle:{_PROC_HANDLE}" in fake.events


# ===========================================================================
# § D — Cursor movement alone (empty line) must NOT defer (B-only policy)
# ===========================================================================

def test_cursor_move_alone_without_nonempty_line_does_not_defer(monkeypatch, no_sleep):
    """
    Under the 2026-11-19 B-only revision, cursor movement by itself is not a
    defer trigger at all. The regression it protects: a moving cursor on an
    empty line (e.g. prompt redraw, scrollback shift) must still INJECT so
    that wakes do not get locked out by harmless console UI motion.
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0],
        peeked_key_down=[False],
        cursor_pairs=[((5, 0), (7, 0))],  # cursor moved, irrelevant under B-only
        line_contents=[""],  # B false → INJECT regardless of cursor
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (True, "ok", 0)
    assert fake.write_calls == 2


# ===========================================================================
# § E — Non-empty line with cursor also moving → defer (B alone is sufficient)
# ===========================================================================

def test_cursor_move_and_nonempty_line_defer(monkeypatch, no_sleep):
    """
    Active typing: non-empty line AND cursor moving across the sample window.
    Under the B-only revision the cursor-movement half is no longer required,
    but the paired signal must still defer — this is the common "user is
    typing right now" case that §P's stationary-cursor variant complements.
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0, 0, 0],
        peeked_key_down=[False, False, False],
        cursor_pairs=[
            ((3, 0), (5, 0)),
            ((5, 0), (7, 0)),
            ((7, 0), (9, 0)),
        ],
        line_contents=["> he", "> hel", "> hell"],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (False, "deferred_user_typing", 0)
    assert fake.write_calls == 0


# ===========================================================================
# § F — Retry loop: initial typing, then clears → wake proceeds
# ===========================================================================

def test_initial_typing_but_retry_sees_idle_proceeds(monkeypatch, no_sleep):
    """
    Initial probe: typing. Retry 1: typing. Retry 2: clear → wake must
    proceed (WriteConsoleInputW IS called).
    """
    fake = _FakeKernel32Typing(
        # 3 probes total (initial + 2 retries). Last probe clears.
        key_event_queue_sizes=[1, 1, 0],
        peeked_key_down=[True, True, False],
        cursor_pairs=[
            ((5, 0), (7, 0)),
            ((7, 0), (9, 0)),
            ((9, 0), (9, 0)),  # no move
        ],
        line_contents=["> he", "> hel", ""],  # line empties by probe 3
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (True, "ok", 0)
    assert fake.write_calls == 2


# ===========================================================================
# § G — Retry loop: persistent typing across all retries → defer
# ===========================================================================

def test_persistent_typing_across_retries_defers(monkeypatch, no_sleep):
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[1, 1, 1],
        peeked_key_down=[True, True, True],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (False, "deferred_user_typing", 0)
    assert fake.write_calls == 0
    # The guard must have exercised the retry budget: Check 1 should have been
    # probed more than once (initial + at least one retry). We do not pin the
    # exact retry count so coder-b retains tuning headroom.
    check1_probes = sum(1 for e in fake.events if e.startswith("PeekConsoleInputW"))
    assert check1_probes >= 2, (
        f"expected at least one retry of Check 1; events={fake.events!r}"
    )


# ===========================================================================
# § H — Detection API failure (Win32 error) → user_typing_check_failed
# ===========================================================================

def test_peek_console_input_failure_reports_user_typing_check_failed(monkeypatch, no_sleep):
    """
    When the detection API itself returns a Win32 error, the guard must
    fail with `(False, "user_typing_check_failed", err)` and NOT fall through
    to a silent injection. This is fail-closed: an unreadable console input
    state is treated as "do not inject".
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[1],
        peek_fail_on_probe=1,
        peek_fail_err=1455,  # ERROR_COMMITMENT_LIMIT (any nonzero works)
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    try:
        ok, reason, err = wc.inject_console_input_verified(
            4242,
            _VALID_FT,
            "wake",
            send_enter=True,
            submit_delay_sec=0.0,
            allowed_images=("agy.exe",),
        )
        assert ok is False
        assert reason == "user_typing_check_failed"
        assert err == 1455
        assert fake.write_calls == 0
    finally:
        ctypes.set_last_error(0)


# ===========================================================================
# § I — Adapter layer: deferred_user_typing surfaces in last_wake_detail
# ===========================================================================

def test_adapter_last_wake_detail_reports_deferred_user_typing(monkeypatch, no_sleep):
    """
    When L0 returns `deferred_user_typing`, `AgyDeliveryAdapter._inject` must
    propagate that verbatim into `last_wake_detail["reason"]` and return False.
    This is what the Watcher's audit.jsonl (and `last_wake_detail`) rely on
    to distinguish a *decision to defer* from a real injection error.
    """
    adapter = AgyDeliveryAdapter()

    def fake_verified(pid, exp_st, text, send_enter, delay, *, allowed_images=None, respect_user_typing=True):
        return (False, "deferred_user_typing", 0)

    monkeypatch.setattr(wc, "inject_console_input_verified", fake_verified)
    # The adapter resolves `_win_console` at import time; make sure the test
    # swap reaches it too.
    import agentknock.adapters.agy as agy_mod
    monkeypatch.setattr(agy_mod._win_console, "inject_console_input_verified", fake_verified)

    ok = adapter._inject(4242, "nudge", expected_start_time=_VALID_FT)
    assert ok is False
    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "deferred_user_typing"
    assert adapter.last_wake_detail["win32_error"] == 0
    assert adapter.last_wake_detail["pid"] == 4242
    assert adapter.last_wake_detail["verified_start_time"] == _VALID_FT


# ===========================================================================
# § J — Adapter layer: ctor kwarg plumbs through to L0
# ===========================================================================

def test_adapter_respect_user_typing_false_passed_to_l0(monkeypatch, no_sleep):
    """
    `AgyDeliveryAdapter(respect_user_typing=False)` must forward that value
    to `inject_console_input_verified` so a caller can disable the guard
    adapter-wide (e.g. in tooling that is known to run while the user is away).
    """
    adapter = AgyDeliveryAdapter(respect_user_typing=False)
    captured: dict[str, Any] = {}

    def fake_verified(pid, exp_st, text, send_enter, delay, *, allowed_images=None, respect_user_typing=True):
        captured["respect_user_typing"] = respect_user_typing
        return (True, "ok", 0)

    import agentknock.adapters.agy as agy_mod
    monkeypatch.setattr(agy_mod._win_console, "inject_console_input_verified", fake_verified)

    ok = adapter._inject(4242, "nudge", expected_start_time=_VALID_FT)
    assert ok is True
    assert captured.get("respect_user_typing") is False, (
        "AgyDeliveryAdapter.respect_user_typing must flow through _inject "
        "into inject_console_input_verified; got "
        f"{captured!r}"
    )

    # Default path (no ctor kwarg) must forward True.
    captured.clear()
    adapter_default = AgyDeliveryAdapter()
    ok2 = adapter_default._inject(4242, "nudge", expected_start_time=_VALID_FT)
    assert ok2 is True
    assert captured.get("respect_user_typing") is True


# ===========================================================================
# § K — Non-regression: idle state traverses guard and still writes nudge
# ===========================================================================

def test_idle_state_passes_guard_and_writes_both_batches(monkeypatch, no_sleep):
    """
    Idle console (0 queued events, cursor stationary, empty line) must pass
    the guard transparently and still produce the two WriteConsoleInputW
    calls (text batch + Enter batch) of the v0.3.3 happy path.
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0],
        peeked_key_down=[False],
        cursor_pairs=[((10, 0), (10, 0))],
        line_contents=[""],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "hi",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (True, "ok", 0)
    assert fake.write_calls == 2
    # Guard still runs when respect_user_typing is left at default True.
    assert any(e.startswith("PeekConsoleInputW") for e in fake.events)


# ===========================================================================
# § L — Non-regression: inject_console_input (bool entrypoint) forwards kwarg
# ===========================================================================

def test_inject_console_input_bool_entry_forwards_respect_user_typing(monkeypatch, no_sleep):
    """
    The boolean-returning `inject_console_input` convenience must accept
    `respect_user_typing` and forward it to the verified entrypoint. This
    keeps the two L0 entrypoints in signature parity.
    """
    sig = inspect.signature(wc.inject_console_input)
    assert "respect_user_typing" in sig.parameters, (
        "Phase 1-B contract: inject_console_input must also accept "
        "`respect_user_typing` so both L0 entrypoints are in parity."
    )

    captured: dict[str, Any] = {}

    def fake_verified(pid, exp_st, text, send_enter, delay, *, allowed_images=None, respect_user_typing=True):
        captured["respect_user_typing"] = respect_user_typing
        return (True, "ok", 0)

    monkeypatch.setattr(wc, "inject_console_input_verified", fake_verified)

    ok = wc.inject_console_input(
        4242,
        "wake",
        expected_start_time=_VALID_FT,
        respect_user_typing=False,
    )
    assert ok is True
    assert captured.get("respect_user_typing") is False


# ===========================================================================
# § M-O — E2E: AgyDeliveryAdapter.wake() public API plumbs ctor kwarg to L0
#
# The §A-L tests prove L0 honors `respect_user_typing` and that the adapter
# `_inject` seam forwards it. These three tests close the loop from the
# public `wake(target_id)` entrypoint: a caller instantiating the adapter
# with the ctor kwarg must get the matching L0 behavior end-to-end, with
# `last_wake_detail["reason"]` reflecting the actual outcome.
# ===========================================================================

_FAKE_CID: str = "11111111-2222-3333-4444-555555555555"


def test_wake_default_guard_typing_defers_and_records_last_wake_detail(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
) -> None:
    """
    `AgyDeliveryAdapter()` (guard ON by default) + persistent typing →
    `wake(cid)` returns False, `last_wake_detail["reason"] ==
    "deferred_user_typing"` and no `WriteConsoleInputW` call.
    """
    adapter = AgyDeliveryAdapter()
    assert getattr(adapter, "respect_user_typing", True) is True

    monkeypatch.setattr(
        adapter, "_find_process_identity", lambda cid: (4242, _VALID_FT)
    )

    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[1, 1, 1],
        peeked_key_down=[True, True, True],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok = adapter.wake(_FAKE_CID)
    assert ok is False
    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "deferred_user_typing"
    assert adapter.last_wake_detail["win32_error"] == 0
    assert adapter.last_wake_detail["pid"] == 4242
    assert adapter.last_wake_detail["verified_start_time"] == _VALID_FT
    assert fake.write_calls == 0, (
        f"deferred wake must not call WriteConsoleInputW; events={fake.events!r}"
    )


def test_wake_guard_off_bypasses_detection_even_when_typing(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
) -> None:
    """
    `AgyDeliveryAdapter(respect_user_typing=False)` + a strongly-typing
    console state → guard is disabled end-to-end, so `wake(cid)` injects
    the nudge (two `WriteConsoleInputW` calls, `last_wake_detail["reason"]
    == "ok"`), and NONE of the four detection APIs appear in the trace.
    This is the regression knob that lets operators run known-unattended
    sessions without the guard getting in the way.
    """
    adapter = AgyDeliveryAdapter(respect_user_typing=False)
    assert getattr(adapter, "respect_user_typing", True) is False

    monkeypatch.setattr(
        adapter, "_find_process_identity", lambda cid: (4242, _VALID_FT)
    )

    # Stage every signal the guard would react to — none of them should matter.
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[9, 9, 9],
        peeked_key_down=[True, True, True],
        cursor_pairs=[((3, 0), (9, 0)), ((9, 0), (14, 0)), ((14, 0), (20, 0))],
        line_contents=["> hel", "> hell", "> hello"],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok = adapter.wake(_FAKE_CID)
    assert ok is True
    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "ok"
    assert fake.write_calls == 2, (
        "guard-off path through wake() must still deliver text + Enter; "
        f"events={fake.events!r}"
    )

    detection_apis = {
        "GetNumberOfConsoleInputEvents",
        "PeekConsoleInputW",
        "GetConsoleScreenBufferInfo",
        "ReadConsoleOutputCharacterW",
    }
    for ev in fake.events:
        head = ev.split(":", 1)[0]
        assert head not in detection_apis, (
            f"wake() with ctor respect_user_typing=False must not touch "
            f"{head}; got event {ev!r} in {fake.events!r}"
        )


def test_wake_default_guard_idle_console_passes_and_delivers_nudge(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
) -> None:
    """
    `AgyDeliveryAdapter()` (guard ON) + idle console (no queued keys,
    stationary cursor, empty line) → `wake(cid)` traverses the guard
    transparently, both `WriteConsoleInputW` batches fire, and
    `last_wake_detail["reason"] == "ok"`. Also confirms Check 1 was
    actually probed at least once (`PeekConsoleInputW` in trace) so a
    passing wake isn't silently bypassing the guard when the default
    says it should run.
    """
    adapter = AgyDeliveryAdapter()
    monkeypatch.setattr(
        adapter, "_find_process_identity", lambda cid: (4242, _VALID_FT)
    )

    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0],
        peeked_key_down=[False],
        cursor_pairs=[((12, 0), (12, 0))],
        line_contents=[""],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok = adapter.wake(_FAKE_CID)
    assert ok is True
    assert adapter.last_wake_detail is not None
    assert adapter.last_wake_detail["reason"] == "ok"
    assert fake.write_calls == 2
    assert any(e.startswith("PeekConsoleInputW") for e in fake.events), (
        "guard-on idle wake must still probe Check 1 (PeekConsoleInputW); "
        f"events={fake.events!r}"
    )


# ===========================================================================
# § P — Paused typing (Check 2-B standalone) & True Idle regression
# ===========================================================================

def test_paused_typing_stationary_cursor_with_nonempty_line_defers(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
) -> None:
    """
    Long-pause edge case: no pending key events and cursor is stationary across
    the 150ms sample window, yet non-whitespace ASCII text sits before the cursor
    (e.g. user paused mid-sentence to think) -> must defer without writing.
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0, 0, 0],
        peeked_key_down=[False, False, False],
        cursor_pairs=[((8, 5), (8, 5)), ((8, 5), (8, 5)), ((8, 5), (8, 5))],
        line_contents=["> wip...", "> wip...", "> wip..."],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (False, "deferred_user_typing", 0)
    assert fake.write_calls == 0
    assert any(e.startswith("AttachConsole:PARENT") for e in fake.events)
    assert f"CloseHandle:{_PROC_HANDLE}" in fake.events


@pytest.mark.parametrize(
    ("cursor_pos", "line_text"),
    [
        ((0, 5), ""),              # X == 0 (start of line)
        ((4, 5), "    "),          # spaces 0x20 only
        ((4, 5), ""),              # empty buffer returned before cursor
        ((4, 5), "\x00\x1f\x7f "), # control / DEL / space only (all non-content)
    ],
)
def test_true_idle_line_allows_inject(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
    cursor_pos: tuple[int, int],
    line_text: str,
) -> None:
    """True idle (empty line, X==0, spaces, or control/DEL only) must still inject normally."""
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0],
        peeked_key_down=[False],
        cursor_pairs=[(cursor_pos, cursor_pos)],
        line_contents=[line_text],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "hi",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (True, "ok", 0)
    assert fake.write_calls == 2


@pytest.mark.parametrize(
    ("cursor_pos", "line_text"),
    [
        ((2, 5), "あ"),
        ((10, 5), "日本語入力中"),
        ((12, 5), "café★テスト"),
    ],
)
def test_paused_typing_non_ascii_wide_char_defers(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
    cursor_pos: tuple[int, int],
    line_text: str,
) -> None:
    """Non-ASCII / full-width Japanese / emoji before stationary cursor must defer without writing."""
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0, 0, 0],
        peeked_key_down=[False, False, False],
        cursor_pairs=[(cursor_pos, cursor_pos)] * 3,
        line_contents=[line_text, line_text, line_text],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (False, "deferred_user_typing", 0)
    assert fake.write_calls == 0


# ===========================================================================
# § Q — Leading agy prompt (`> `) exclusion & non-lockout regression
# ===========================================================================

@pytest.mark.parametrize(
    ("cursor_pos", "line_text", "expected_ok", "expected_reason"),
    [
        # Agy idle prompt variations -> leading `>` + contiguous 0x20 skipped -> INJECT
        ((2, 5), "> ", True, "ok"),
        ((1, 5), ">", True, "ok"),
        ((4, 5), ">   ", True, "ok"),
        # User typed after agy prompt -> content remains after skip -> DEFER
        ((3, 5), "> あ", False, "deferred_user_typing"),
        ((8, 5), "> 日本語", False, "deferred_user_typing"),
        ((7, 5), "> > foo", False, "deferred_user_typing"),
        # Not starting at col 0 with `>` -> NOT skipped -> DEFER
        ((3, 5), " > ", False, "deferred_user_typing"),
        ((1, 5), "a", False, "deferred_user_typing"),
    ],
)
def test_leading_prompt_exclusion_and_lockout_prevention(
    monkeypatch: pytest.MonkeyPatch,
    no_sleep: None,
    cursor_pos: tuple[int, int],
    line_text: str,
    expected_ok: bool,
    expected_reason: str,
) -> None:
    """
    Agy's idle prompt `> ` at col 0 must be excluded so idle wakes succeed (INJECT),
    while content after `> ` (e.g. `> あ`) or non-col0 `>` still defers.
    """
    fake = _FakeKernel32Typing(
        key_event_queue_sizes=[0, 0, 0],
        peeked_key_down=[False, False, False],
        cursor_pairs=[(cursor_pos, cursor_pos)] * 3,
        line_contents=[line_text, line_text, line_text],
    )
    monkeypatch.setattr(wc, "_kernel32", fake)
    monkeypatch.setattr(wc, "_user32", _FakeUser32())

    ok, reason, err = wc.inject_console_input_verified(
        4242,
        _VALID_FT,
        "wake",
        send_enter=True,
        submit_delay_sec=0.0,
        allowed_images=("agy.exe",),
    )
    assert (ok, reason, err) == (expected_ok, expected_reason, 0)
    assert fake.write_calls == (2 if expected_ok else 0)


# ===========================================================================
# § S — Phase 1-A / 1-C: fixed CLI runner `run_op.py` (status / send / ack)
# ===========================================================================
# Spec: agent_docs/think/agentknock_v04_sandbox_and_nudge_issues.md §3.1 + §3.3.
# Target module: `run_op` (lives at `agentknock/run_op.py`, i.e. the
# `_TOOLS_DIR` that `tests/conftest.py` already puts on sys.path).
#
# Tests use a lazy fixture so that a missing `run_op.py` does NOT break
# collection of the existing 90 typing-guard tests — each run_op test fails
# red individually (TDD-friendly) until coder-a lands the file.
# ===========================================================================
import importlib
import json as _json
import uuid as _uuid
from pathlib import Path as _Path
from typing import Any as _Any


_RUN_OP_IMPORT_MSG = (
    "Phase 1-A: `run_op.py` must exist at "
    "`agent_docs/tools/agentknock/run_op.py` per spec "
    "`agentknock_v04_sandbox_and_nudge_issues.md` §3.1 (3) and expose "
    "`CLAUDE_IDS`, `check_target_status`, `run_status`, `run_send`, `run_ack`."
)


@pytest.fixture
def run_op_module():
    """
    Lazily import the `run_op` module. If missing, fail THIS test red (not
    skip, not collection-error) with a message that points coder-a at the
    spec. This keeps TDD red→green signal clean while not blocking the 90
    existing typing-guard tests.
    """
    try:
        return importlib.import_module("run_op")
    except ModuleNotFoundError as e:  # pragma: no cover - driven by file state
        pytest.fail(f"{_RUN_OP_IMPORT_MSG} (ModuleNotFoundError: {e})")


_SAMPLE_AGY_UUID = "0c2d2810-bafd-4e0c-8ed6-a3c6c06cf9c3"  # QA-style target
_SAMPLE_6D_UUID = "36e46c8a-59ad-4e5b-9df3-9eec25ad352b"   # 6d (Claude session)
_SAMPLE_D4_UUID = "44d8d80f-0394-4a07-ad6a-19acc09a3ec4"   # d4 (Claude session)


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """
    Redirect `Path.home()` to a per-test tmp dir so `run_op`'s absolute
    `Path.home() / '.gemini/antigravity-cli/...'` paths land in isolated
    scratch without touching the real presence/brain dirs.
    """
    monkeypatch.setattr(_Path, "home", classmethod(lambda cls: tmp_path))  # type: ignore[arg-type]
    (tmp_path / ".gemini" / "antigravity-cli" / "presence").mkdir(
        parents=True, exist_ok=True
    )
    return tmp_path


# --- § S.1 contract: public API surface the spec names ---------------------


def test_run_op_exposes_required_public_surface(run_op_module):
    """
    Spec §3.1 (3) names CLAUDE_IDS, check_target_status, run_status, run_send,
    run_ack. If any one is missing, run_op is not spec-compliant and all
    downstream tests would short-circuit anyway — surface it directly.
    """
    required = ["CLAUDE_IDS", "check_target_status", "run_status", "run_send", "run_ack"]
    missing = [name for name in required if not hasattr(run_op_module, name)]
    assert not missing, (
        f"run_op.py is missing spec-required symbols: {missing}. "
        f"See spec §3.1 (3)."
    )
    # CLAUDE_IDS must at least include d4 (44d8d80f) and 6d (36e46c8a), the
    # two Claude-session UUIDs the chat layer must route mailbox-only.
    claude_ids = run_op_module.CLAUDE_IDS
    assert isinstance(claude_ids, (set, frozenset, tuple, list)), (
        "CLAUDE_IDS must be an iterable set-like container (spec §3.1 (3))."
    )
    assert _SAMPLE_D4_UUID in set(claude_ids), (
        "CLAUDE_IDS must include the d4 session id."
    )
    assert _SAMPLE_6D_UUID in set(claude_ids), (
        "CLAUDE_IDS must include the 6d session id (this test module's own "
        "conversation id)."
    )


# --- § S.2 status action --------------------------------------------------


def test_status_lock_missing_reports_alive_false_and_reason(run_op_module, fake_home):
    """
    Spec §3.1 (3): if `presence/<target_id>.lock` does not exist, status must
    report `alive=False, reason="lock_missing", pid=None, start_time=None`.
    """
    payload = {
        "action": "status",
        "targets": [
            {"alias": "qa", "target_id": _SAMPLE_AGY_UUID, "role": "QA"},
        ],
    }
    result = run_op_module.run_status(payload)
    assert result["ok"] is True
    assert result["action"] == "status"
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["alias"] == "qa"
    assert row["target_id"] == _SAMPLE_AGY_UUID
    assert row["role"] == "QA"
    assert row["alive"] is False
    assert row["reason"] == "lock_missing"
    assert row["pid"] is None
    assert row["start_time"] is None


def test_status_lock_present_but_process_not_found_reports_ambiguous(
    monkeypatch, run_op_module, fake_home
):
    """
    Spec §3.1 (3): if the lock exists but `find_process_by_lock_path` returns
    None (not found or ambiguous), status must report
    `alive=False, reason="process_not_found_or_ambiguous"`.
    """
    lock_path = fake_home / ".gemini" / "antigravity-cli" / "presence" / f"{_SAMPLE_AGY_UUID}.lock"
    lock_path.write_text("", encoding="utf-8")

    seen_kwargs: dict[str, _Any] = {}

    def fake_find_process_by_lock_path(path, *, allowed_images=None, same_session_only=True):
        seen_kwargs["path"] = path
        seen_kwargs["allowed_images"] = allowed_images
        seen_kwargs["same_session_only"] = same_session_only
        return None

    from agentknock.adapters import _win_console_ctypes as _wc_mod
    monkeypatch.setattr(_wc_mod, "find_process_by_lock_path", fake_find_process_by_lock_path)

    payload = {"action": "status", "targets": [{"alias": "qa", "target_id": _SAMPLE_AGY_UUID, "role": "QA"}]}
    result = run_op_module.run_status(payload)
    row = result["results"][0]
    assert row["alive"] is False
    assert row["reason"] == "process_not_found_or_ambiguous"
    assert row["pid"] is None
    assert row["start_time"] is None
    # Spec §3.1 (3): status must use same_session_only=False (no AttachConsole
    # is performed in status, so cross-session verification is appropriate).
    assert seen_kwargs.get("same_session_only") is False, (
        "status must call find_process_by_lock_path with same_session_only=False "
        f"(got {seen_kwargs!r})"
    )
    # And allowed_images must be ("agy.exe",).
    assert seen_kwargs.get("allowed_images") == ("agy.exe",), (
        "status must constrain allowed_images to ('agy.exe',) for safety; "
        f"got {seen_kwargs.get('allowed_images')!r}"
    )


def test_status_lock_present_and_process_found_reports_alive_ok(
    monkeypatch, run_op_module, fake_home
):
    """
    Spec §3.1 (3): if the lock exists and find_process_by_lock_path returns
    (pid, start_time), status must report
    `alive=True, reason="ok", pid=<int>, start_time=<int>`.
    """
    lock_path = fake_home / ".gemini" / "antigravity-cli" / "presence" / f"{_SAMPLE_AGY_UUID}.lock"
    lock_path.write_text("", encoding="utf-8")

    fake_identity = (9908, (0x01DC0000 << 32) | 0xABCDEF01)

    from agentknock.adapters import _win_console_ctypes as _wc_mod
    monkeypatch.setattr(
        _wc_mod,
        "find_process_by_lock_path",
        lambda *_args, **_kwargs: fake_identity,
    )

    payload = {"action": "status", "targets": [{"alias": "qa", "target_id": _SAMPLE_AGY_UUID, "role": "QA"}]}
    result = run_op_module.run_status(payload)
    row = result["results"][0]
    assert row["alive"] is True
    assert row["reason"] == "ok"
    assert row["pid"] == 9908
    assert row["start_time"] == fake_identity[1]


# --- § S.3 send action: Claude mailbox-only vs. agy wake --------------------


class _FakeAgyAdapter:
    """
    Stand-in for AgyDeliveryAdapter capturing the ctor kwargs, wake() calls
    and last_wake_detail state. run_op is expected to instantiate exactly
    ONE of these per `run_send` invocation.
    """

    instances: list["_FakeAgyAdapter"] = []

    def __init__(self, *, wake_locale=None, output_locale=None, **extra):
        self.wake_locale = wake_locale
        self.output_locale = output_locale
        self.extra_ctor_kwargs = extra
        self.wake_calls: list[str] = []
        self.last_wake_detail: dict[str, _Any] = {
            "reason": "ok",
            "win32_error": 0,
            "pid": 9908,
            "verified_start_time": 1,
            "backend": "fake",
        }
        _FakeAgyAdapter.instances.append(self)

    def wake(self, target_id: str) -> bool:
        self.wake_calls.append(target_id)
        return True


@pytest.fixture
def fake_adapter_factory(monkeypatch, run_op_module):
    """Replace AgyDeliveryAdapter in run_op with _FakeAgyAdapter and reset state."""
    _FakeAgyAdapter.instances = []
    monkeypatch.setattr(run_op_module, "AgyDeliveryAdapter", _FakeAgyAdapter)
    return _FakeAgyAdapter


class _FakeAgentKnock:
    """
    Minimal AgentKnock stand-in: captures ctor DI kwargs plus register_self /
    send calls, returns synthetic msg_ids. Lets run_op tests exercise routing
    (Claude vs agy) without going through the real Mailbox / FileSystemAlias-
    Registry / FileLockLivenessProbe stack (which would hit Windows MAX_PATH
    with the nested `.gemini/antigravity-cli/brain/<uuid>/.system_generated/
    messages/undelivered/.<mid>.<hex>.tmp` paths under pytest's tmp_path).
    """

    instances: list["_FakeAgentKnock"] = []

    def __init__(self, *, mailbox=None, registry=None, probe=None):
        self.mailbox = mailbox
        self.registry = registry
        self.probe = probe
        self.register_calls: list[dict[str, _Any]] = []
        self.send_calls: list[dict[str, _Any]] = []
        _FakeAgentKnock.instances.append(self)

    def register_self(self, alias, target_id, *, role="", capabilities=None, ttl_sec=86400.0):
        self.register_calls.append(
            {"alias": alias, "target_id": target_id, "role": role, "ttl_sec": ttl_sec}
        )

    def send(self, recipient, content, *, sender):
        msg_id = str(_uuid.uuid4())
        self.send_calls.append(
            {"recipient": recipient, "content": content, "sender": sender, "msg_id": msg_id}
        )
        return msg_id


@pytest.fixture
def fake_agentknock_factory(monkeypatch, run_op_module):
    """
    Replace `AgentKnock` inside run_op with `_FakeAgentKnock`. run_op builds
    two instances per `run_send` (one with FileLockLivenessProbe, one with
    AlwaysAliveProbe) so that each target can be routed via its appropriate
    client; `_FakeAgentKnock.instances` collects both in construction order.
    """
    _FakeAgentKnock.instances = []
    monkeypatch.setattr(run_op_module, "AgentKnock", _FakeAgentKnock)
    return _FakeAgentKnock


def test_send_to_claude_id_skips_wake_mailbox_only(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.1 (2) / (3): when `target_id` is in `CLAUDE_IDS`, run_send must
    - route the message via the AlwaysAliveProbe client (mailbox-only),
    - NOT attempt CONIN$ injection (adapter.wake NOT called),
    - record `woke=False, wake_detail="skipped_claude_mailbox_only"`.
    """
    payload = {
        "action": "send",
        "sender": "subleader-argon",
        "targets": [
            {
                "alias": "d4",
                "target_id": _SAMPLE_D4_UUID,  # d4 is a Claude session
                "role": "Commander",
                "wake": True,  # requested but must be ignored for Claude
                "content": "hello d4",
            }
        ],
    }
    result = run_op_module.run_send(payload)
    assert result["ok"] is True
    assert result["action"] == "send"
    row = result["results"][0]
    assert row["alias"] == "d4"
    assert row["target_id"] == _SAMPLE_D4_UUID
    assert isinstance(row["msg_id"], str) and len(row["msg_id"]) > 0, (
        "send must still enqueue a mailbox message even for Claude sessions."
    )
    assert row["woke"] is False, (
        "Claude ID path must NOT wake (CONIN$ is skipped); "
        f"got woke={row['woke']}, wake_detail={row.get('wake_detail')!r}"
    )
    assert row["wake_detail"] == "skipped_claude_mailbox_only", (
        f"Claude ID path must record wake_detail='skipped_claude_mailbox_only'; "
        f"got {row['wake_detail']!r}"
    )

    # And adapter.wake MUST NOT have been invoked for the Claude target.
    for inst in _FakeAgyAdapter.instances:
        assert _SAMPLE_D4_UUID not in inst.wake_calls, (
            "AgyDeliveryAdapter.wake was called for a Claude target; "
            "run_op must route Claude IDs through the mailbox-only path."
        )


def test_send_to_agy_target_invokes_wake_and_captures_last_wake_detail(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.1 (3): when `target_id` is NOT in CLAUDE_IDS, run_send must invoke
    `adapter.wake(target_id)` and record its outcome (adapter.last_wake_detail
    is exposed via the `wake_detail` field).
    """
    payload = {
        "action": "send",
        "sender": "subleader-argon",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": True,
                "content": "please verify",
            }
        ],
    }
    result = run_op_module.run_send(payload)
    row = result["results"][0]
    assert row["woke"] is True
    # wake_detail is the adapter's dict — reason surfaces the L0 outcome.
    wd = row["wake_detail"]
    if isinstance(wd, dict):
        assert wd.get("reason") == "ok"
    else:
        # Spec example shows it may be the dict directly or `last_wake_detail`
        # attribute; either shape is acceptable as long as reason is reachable.
        assert "ok" in str(wd).lower(), (
            f"wake_detail should surface the L0 reason; got {wd!r}"
        )
    assert len(_FakeAgyAdapter.instances) == 1
    inst = _FakeAgyAdapter.instances[0]
    assert inst.wake_calls == [_SAMPLE_AGY_UUID], (
        f"adapter.wake must be called exactly once with {_SAMPLE_AGY_UUID!r}; "
        f"got {inst.wake_calls!r}"
    )


def test_send_respects_wake_false_flag_even_for_agy_target(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.1 (2): per-target `wake: false` must suppress the CONIN$ nudge
    even for agy (non-Claude) targets. Mailbox-only delivery still happens.
    """
    payload = {
        "action": "send",
        "sender": "orchestrator",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": False,
                "content": "silent delivery",
            }
        ],
    }
    result = run_op_module.run_send(payload)
    row = result["results"][0]
    assert row["woke"] is False
    assert isinstance(row["msg_id"], str) and row["msg_id"]
    for inst in _FakeAgyAdapter.instances:
        assert inst.wake_calls == [], (
            "wake:false must suppress adapter.wake; "
            f"got wake_calls={inst.wake_calls!r}"
        )


# --- § S.4 ack action -----------------------------------------------------


def test_ack_removes_undelivered_marker_and_updates_read_json(
    run_op_module, fake_home
):
    """
    Spec §3.1 (3): ack must delete each `undelivered/<mid>` marker (when it
    exists) and write `read.json` so that each acked id maps to True.
    """
    self_id = _SAMPLE_6D_UUID
    msg_dir = fake_home / ".gemini" / "antigravity-cli" / "brain" / self_id / ".system_generated" / "messages"
    undelivered_dir = msg_dir / "undelivered"
    undelivered_dir.mkdir(parents=True, exist_ok=True)
    mid_1 = "e895fec6-882f-4a85-933d-6c30ee8cc8fb"
    mid_2 = str(_uuid.uuid4())
    (undelivered_dir / mid_1).write_text("", encoding="utf-8")
    (undelivered_dir / mid_2).write_text("", encoding="utf-8")

    payload = {"action": "ack", "self_id": self_id, "message_ids": [mid_1, mid_2]}
    result = run_op_module.run_ack(payload)
    assert result["ok"] is True
    assert result["action"] == "ack"
    assert set(result["acked"]) == {mid_1, mid_2}
    # Markers deleted.
    assert not (undelivered_dir / mid_1).exists()
    assert not (undelivered_dir / mid_2).exists()
    # read.json created and both IDs mapped to True.
    read_file = msg_dir / "read.json"
    assert read_file.is_file()
    data = _json.loads(read_file.read_text(encoding="utf-8"))
    assert data.get(mid_1) is True
    assert data.get(mid_2) is True


def test_ack_merges_into_existing_read_json_without_dropping_prior_entries(
    run_op_module, fake_home
):
    """
    Regression guard: ack must MERGE into an existing read.json, not replace
    it, so earlier acks are not lost on a subsequent ack call.
    """
    self_id = _SAMPLE_6D_UUID
    msg_dir = fake_home / ".gemini" / "antigravity-cli" / "brain" / self_id / ".system_generated" / "messages"
    undelivered_dir = msg_dir / "undelivered"
    undelivered_dir.mkdir(parents=True, exist_ok=True)

    prior_id = str(_uuid.uuid4())
    (msg_dir / "read.json").write_text(
        _json.dumps({prior_id: True}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    new_id = str(_uuid.uuid4())
    (undelivered_dir / new_id).write_text("", encoding="utf-8")

    result = run_op_module.run_ack(
        {"action": "ack", "self_id": self_id, "message_ids": [new_id]}
    )
    assert new_id in result["acked"]
    data = _json.loads((msg_dir / "read.json").read_text(encoding="utf-8"))
    assert data.get(prior_id) is True, (
        "ack must preserve prior read entries; existing read.json was clobbered."
    )
    assert data.get(new_id) is True


def test_ack_tolerates_missing_undelivered_marker(run_op_module, fake_home):
    """
    Spec §3.1 (3): markers are only removed `if marker.exists()`. ack must
    not raise when a marker was already consumed by a prior watcher pass.
    """
    self_id = _SAMPLE_6D_UUID
    msg_dir = fake_home / ".gemini" / "antigravity-cli" / "brain" / self_id / ".system_generated" / "messages"
    undelivered_dir = msg_dir / "undelivered"
    undelivered_dir.mkdir(parents=True, exist_ok=True)
    mid = str(_uuid.uuid4())  # marker intentionally NOT created
    result = run_op_module.run_ack({"action": "ack", "self_id": self_id, "message_ids": [mid]})
    assert result["ok"] is True
    assert mid in result["acked"]
    data = _json.loads((msg_dir / "read.json").read_text(encoding="utf-8"))
    assert data.get(mid) is True


# --- § S.5 Phase 1-C locale: defaults, payload passthrough, no forced "ja" ---


def test_locale_defaults_to_none_so_adapter_uses_en_default(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.3: when neither CLI args nor payload specify a locale, run_op
    MUST NOT force any value — it passes `None` so that AgyDeliveryAdapter's
    ctor-side resolution picks up `DEFAULT_LOCALE == "en"`. Specifically the
    adapter must NOT be ctor'd with wake_locale="ja" or output_locale="ja".
    """
    payload = {
        "action": "send",
        "sender": "orchestrator",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": True,
                "content": "english default",
            }
        ],
    }
    run_op_module.run_send(payload)
    assert len(_FakeAgyAdapter.instances) == 1
    inst = _FakeAgyAdapter.instances[0]
    assert inst.wake_locale is None, (
        "Phase 1-C invariant: run_op must not inject a default locale; "
        f"got wake_locale={inst.wake_locale!r}. Spec §3.3."
    )
    assert inst.output_locale is None, (
        "Phase 1-C invariant: run_op must not inject a default locale; "
        f"got output_locale={inst.output_locale!r}. Spec §3.3."
    )


def test_locale_from_payload_passed_through_when_specified(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.3: when `payload.wake_locale` / `payload.output_locale` are set,
    they must reach the adapter ctor verbatim.
    """
    payload = {
        "action": "send",
        "sender": "orchestrator",
        "wake_locale": "ja",
        "output_locale": "ja",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": True,
                "content": "日本語",
            }
        ],
    }
    run_op_module.run_send(payload)
    assert len(_FakeAgyAdapter.instances) == 1
    inst = _FakeAgyAdapter.instances[0]
    assert inst.wake_locale == "ja"
    assert inst.output_locale == "ja"


def test_cli_locale_arg_overrides_payload_locale(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.3: the `run_send` entrypoint takes `cli_wake_locale` /
    `cli_output_locale` keyword-only arguments; when supplied they override
    whatever is in the payload (CLI explicit wins over payload explicit).
    """
    payload = {
        "action": "send",
        "sender": "orchestrator",
        "wake_locale": "ja",
        "output_locale": "ja",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": True,
                "content": "ζ",
            }
        ],
    }
    sig = inspect.signature(run_op_module.run_send)
    assert "cli_wake_locale" in sig.parameters, (
        "run_send must accept keyword-only `cli_wake_locale` per spec §3.1 (3)."
    )
    assert "cli_output_locale" in sig.parameters, (
        "run_send must accept keyword-only `cli_output_locale` per spec §3.1 (3)."
    )

    run_op_module.run_send(payload, cli_wake_locale="fr", cli_output_locale="de")
    assert len(_FakeAgyAdapter.instances) == 1
    inst = _FakeAgyAdapter.instances[0]
    assert inst.wake_locale == "fr", (
        "CLI --wake-locale must win over payload.wake_locale; "
        f"got {inst.wake_locale!r}"
    )
    assert inst.output_locale == "de", (
        "CLI --output-locale must win over payload.output_locale; "
        f"got {inst.output_locale!r}"
    )


def test_locale_cli_only_passed_through(
    monkeypatch, run_op_module, fake_home, fake_adapter_factory, fake_agentknock_factory
):
    """
    Spec §3.3: CLI args alone (no payload field) → adapter gets the CLI value.
    This guards the common operator flow where `--wake-locale ja` is explicit
    but payload.json omits the key.
    """
    payload = {
        "action": "send",
        "sender": "orchestrator",
        "targets": [
            {
                "alias": "qa",
                "target_id": _SAMPLE_AGY_UUID,
                "role": "QA",
                "wake": True,
                "content": "en-config but ja-wake",
            }
        ],
    }
    run_op_module.run_send(payload, cli_wake_locale="ja", cli_output_locale=None)
    inst = _FakeAgyAdapter.instances[0]
    assert inst.wake_locale == "ja"
    # output_locale left None so adapter uses "en" default.
    assert inst.output_locale is None


# --- § S.6 main() CLI entrypoint smoke (d4 contract 2026-11-20) -------------
# Contract fixed by d4 for coder-a:
#   main(argv=None) -> int:
#     --payload (required) / --wake-locale / --output-locale (optional, None)
#     payload = json.load(--payload)
#     action: status -> run_status(payload)
#             send   -> run_send(payload, cli_wake_locale=..., cli_output_locale=...)
#             ack    -> run_ack(payload)
#             other  -> {"ok": False, "error": "<...>"}
#     print(json.dumps(result, ensure_ascii=False)) one line
#     return 0 if result["ok"] else 1
#     if __name__ == "__main__": raise SystemExit(main())


def test_main_status_prints_json_and_returns_zero(
    monkeypatch, capsys, run_op_module, fake_home, tmp_path
):
    """
    `main(["--payload", <status payload path>])` must:
      - load the payload JSON from the file,
      - dispatch to run_status,
      - emit exactly one line of JSON to stdout (parseable),
      - return 0 when result.ok is True.
    presence/find is mocked so status reports alive=True.
    """
    # A valid lock + mocked identity so run_status gives alive=True, ok=True.
    lock_path = fake_home / ".gemini" / "antigravity-cli" / "presence" / f"{_SAMPLE_AGY_UUID}.lock"
    lock_path.write_text("", encoding="utf-8")
    from agentknock.adapters import _win_console_ctypes as _wc_mod
    monkeypatch.setattr(
        _wc_mod,
        "find_process_by_lock_path",
        lambda *_a, **_k: (9908, (0x01DC0000 << 32) | 0xABCDEF01),
    )

    payload_path = tmp_path / "status_payload.json"
    payload_path.write_text(
        _json.dumps({
            "action": "status",
            "targets": [
                {"alias": "qa", "target_id": _SAMPLE_AGY_UUID, "role": "QA"},
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert hasattr(run_op_module, "main"), (
        "run_op.py must expose `main(argv=None) -> int` per d4 contract 2026-11-20."
    )
    rc = run_op_module.main(["--payload", str(payload_path)])
    assert rc == 0, f"status success must return 0; got {rc}"

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, (
        "main() must print exactly one JSON line to stdout; "
        f"got {len(lines)} non-empty lines: {lines!r}"
    )
    parsed = _json.loads(lines[0])
    assert parsed["ok"] is True
    assert parsed["action"] == "status"
    assert isinstance(parsed["results"], list) and len(parsed["results"]) == 1
    row = parsed["results"][0]
    assert row["alive"] is True
    assert row["reason"] == "ok"
    assert row["pid"] == 9908


def test_main_unknown_action_returns_one_with_ok_false(
    capsys, run_op_module, fake_home, tmp_path
):
    """
    Unknown `action` must yield `{"ok": False, "error": "..."}` on stdout and
    return exit code 1. Guards against silent pass-through of typos/malformed
    payloads.
    """
    payload_path = tmp_path / "bogus.json"
    payload_path.write_text(
        _json.dumps({"action": "bogus"}, ensure_ascii=False),
        encoding="utf-8",
    )
    rc = run_op_module.main(["--payload", str(payload_path)])
    assert rc == 1, f"unknown action must return 1; got {rc}"

    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = _json.loads(lines[0])
    assert parsed["ok"] is False
    assert "error" in parsed and isinstance(parsed["error"], str) and parsed["error"], (
        f"unknown action must include a human-readable `error` string; got {parsed!r}"
    )


def test_main_cli_locale_flags_reach_run_send_as_kwargs(
    monkeypatch, capsys, run_op_module, fake_home, tmp_path, fake_adapter_factory, fake_agentknock_factory
):
    """
    CLI flags `--wake-locale` / `--output-locale` must reach `run_send` as
    keyword-only `cli_wake_locale` / `cli_output_locale` so that CLI-explicit
    locale wins over payload-explicit. We intercept run_send to capture the
    exact kwargs main() hands it.
    """
    captured_kwargs: dict[str, _Any] = {}
    original_run_send = run_op_module.run_send

    def intercepting_run_send(payload, **kwargs):
        captured_kwargs.update(kwargs)
        # Delegate so stdout stays JSON-shaped and the test behaves like a
        # real send call (adapter is the _FakeAgyAdapter via fixture).
        return original_run_send(payload, **kwargs)

    monkeypatch.setattr(run_op_module, "run_send", intercepting_run_send)

    payload_path = tmp_path / "send.json"
    payload_path.write_text(
        _json.dumps({
            "action": "send",
            "sender": "orchestrator",
            # Payload locales intentionally present so we can assert CLI wins
            # at the run_send boundary (payload-vs-CLI precedence lives inside
            # run_send; here we only assert main hands CLI values through).
            "wake_locale": "ja",
            "output_locale": "ja",
            "targets": [
                {
                    "alias": "qa",
                    "target_id": _SAMPLE_AGY_UUID,
                    "role": "QA",
                    "wake": True,
                    "content": "cli-locale smoke",
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    rc = run_op_module.main(
        ["--payload", str(payload_path), "--wake-locale", "fr", "--output-locale", "de"]
    )
    assert rc == 0, f"successful send must return 0; got {rc}"

    assert captured_kwargs.get("cli_wake_locale") == "fr", (
        f"main() must forward --wake-locale as cli_wake_locale; "
        f"got {captured_kwargs!r}"
    )
    assert captured_kwargs.get("cli_output_locale") == "de", (
        f"main() must forward --output-locale as cli_output_locale; "
        f"got {captured_kwargs!r}"
    )

    # And the adapter ultimately gets the CLI-explicit values (end-to-end).
    inst = _FakeAgyAdapter.instances[-1]
    assert inst.wake_locale == "fr"
    assert inst.output_locale == "de"

    # Also sanity-check stdout is still a parseable one-line JSON.
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 1
    parsed = _json.loads(lines[0])
    assert parsed["ok"] is True
    assert parsed["action"] == "send"
