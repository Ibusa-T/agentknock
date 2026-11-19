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
