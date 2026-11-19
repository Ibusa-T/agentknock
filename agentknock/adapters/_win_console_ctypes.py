# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later
"""
agentknock.adapters._win_console_ctypes — Pure-Python ctypes Win32 L0 Backend.

Provides the exact same L0 API and Kernel HANDLE-pinned TOCTOU security guarantees
as `_win_console.pyx`:
- `find_process_by_lock_path(lock_path, allowed_images=None, *, same_session_only=True) -> Optional[tuple[int, int]]`
- `find_pid_by_lock_path(lock_path, allowed_images=None) -> Optional[int]`
- `inject_console_input_verified(target_pid, expected_start_time, ...) -> tuple[bool, str, int]`
- `inject_console_input(target_pid, text=..., ..., *, expected_start_time=None, allowed_images=None) -> bool`

Security invariants:
1. Never returns a fake `True` stub. On non-Windows or any verification failure,
   always fails closed (`None` or `False`).
2. Pins target process via `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)`
   before checking liveness (`WaitForSingleObject == WAIT_TIMEOUT`), creation time
   (`GetProcessTimes == expected_start_time`), and image basename (`QueryFullProcessImageNameW`),
   holding the kernel HANDLE open across `FreeConsole` -> `AttachConsole` -> `CONIN$` write ->
   parent console restore, and closing it only in the outermost `finally`.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import sys
import time
from typing import Final, Optional, Sequence

_BACKEND: Final[str] = "ctypes"
DEFAULT_ALLOWED_IMAGES: Final[tuple[str, ...]] = ("agy.exe",)

# Win32 Constants
PROCESS_QUERY_LIMITED_INFORMATION: Final[int] = 0x1000
SYNCHRONIZE: Final[int] = 0x00100000
WAIT_OBJECT_0: Final[int] = 0x00000000
WAIT_TIMEOUT: Final[int] = 0x00000102
ERROR_MORE_DATA: Final[int] = 234
MAX_RM_PROCS: Final[int] = 256
MAX_RM_RETRIES: Final[int] = 3

ATTACH_PARENT_PROCESS: Final[int] = 0xFFFFFFFF
GENERIC_READ: Final[int] = 0x80000000
GENERIC_WRITE: Final[int] = 0x40000000
FILE_SHARE_READ: Final[int] = 0x00000001
FILE_SHARE_WRITE: Final[int] = 0x00000002
OPEN_EXISTING: Final[int] = 3
INVALID_HANDLE_VALUE: Final[int] = ctypes.c_void_p(-1).value or -1

KEY_EVENT: Final[int] = 0x0001
VK_RETURN: Final[int] = 0x0D
MAPVK_VK_TO_VSC: Final[int] = 0

TYPING_CURSOR_SAMPLE_SEC: Final[float] = 0.15
TYPING_RETRY_DELAY_SEC: Final[float] = 0.4
TYPING_MAX_RETRIES: Final[int] = 2
MAX_PEEK_INPUT_EVENTS: Final[int] = 64
MAX_CONSOLE_READ_COLS: Final[int] = 4096

CCH_RM_SESSION_KEY: Final[int] = 32
CCH_RM_MAX_APP_NAME: Final[int] = 255
CCH_RM_MAX_SVC_NAME: Final[int] = 63


class FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwProcessId", wintypes.DWORD),
        ("ProcessStartTime", FILETIME),
    ]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wintypes.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


class _CHAR_UNION(ctypes.Union):
    _fields_ = [
        ("UnicodeChar", wintypes.WCHAR),
        ("AsciiChar", ctypes.c_char),
    ]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _CHAR_UNION),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EVENT_UNION(ctypes.Union):
    _fields_ = [
        ("KeyEvent", KEY_EVENT_RECORD),
    ]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", wintypes.WORD),
        ("Event", _EVENT_UNION),
    ]


class COORD(ctypes.Structure):
    _fields_ = [
        ("X", wintypes.SHORT),
        ("Y", wintypes.SHORT),
    ]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [
        ("Left", wintypes.SHORT),
        ("Top", wintypes.SHORT),
        ("Right", wintypes.SHORT),
        ("Bottom", wintypes.SHORT),
    ]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", SMALL_RECT),
        ("dwMaximumWindowSize", COORD),
    ]


def _filetime_to_u64(ft: FILETIME) -> int:
    return ((int(ft.dwHighDateTime) & 0xFFFFFFFF) << 32) | (
        int(ft.dwLowDateTime) & 0xFFFFFFFF
    )


def _normalize_allowed_images(
    allowed_images: Optional[Sequence[str]],
) -> Optional[set[str]]:
    if allowed_images is None:
        return None
    return {os.path.basename(str(img)).strip().lower() for img in allowed_images if str(img).strip()}


if sys.platform == "win32":
    _kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    _user32 = ctypes.WinDLL("user32.dll", use_last_error=True)
    _rstrtmgr = ctypes.WinDLL("rstrtmgr.dll", use_last_error=True)

    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE

    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD

    _kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
        ctypes.POINTER(FILETIME),
    ]
    _kernel32.GetProcessTimes.restype = wintypes.BOOL

    _kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    _kernel32.GetCurrentProcessId.argtypes = []
    _kernel32.GetCurrentProcessId.restype = wintypes.DWORD

    _kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

    _kernel32.FreeConsole.argtypes = []
    _kernel32.FreeConsole.restype = wintypes.BOOL

    _kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    _kernel32.AttachConsole.restype = wintypes.BOOL

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE

    _kernel32.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.WriteConsoleInputW.restype = wintypes.BOOL

    _kernel32.GetNumberOfConsoleInputEvents.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.GetNumberOfConsoleInputEvents.restype = wintypes.BOOL

    _kernel32.PeekConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.PeekConsoleInputW.restype = wintypes.BOOL

    _kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
    ]
    _kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL

    _kernel32.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        COORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL

    _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    _user32.MapVirtualKeyW.restype = wintypes.UINT

    _rstrtmgr.RmStartSession.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    _rstrtmgr.RmStartSession.restype = wintypes.DWORD

    _rstrtmgr.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT,
        wintypes.LPVOID,
        wintypes.UINT,
        wintypes.LPVOID,
    ]
    _rstrtmgr.RmRegisterResources.restype = wintypes.DWORD

    _rstrtmgr.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
         ctypes.POINTER(RM_PROCESS_INFO),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _rstrtmgr.RmGetList.restype = wintypes.DWORD

    _rstrtmgr.RmEndSession.argtypes = [wintypes.DWORD]
    _rstrtmgr.RmEndSession.restype = wintypes.DWORD
else:
    _kernel32 = None
    _user32 = None
    _rstrtmgr = None


def _is_valid_handle(h: Optional[int]) -> bool:
    if h is None or h == 0:
        return False
    if h == INVALID_HANDLE_VALUE or h == -1:
        return False
    return True


def _query_image_basename_lower(h_proc: int) -> tuple[Optional[str], int]:
    """Query process executable basename (lowercase) from an open process HANDLE."""
    buf_len = wintypes.DWORD(1024)
    buf = ctypes.create_unicode_buffer(buf_len.value)
    if not _kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(buf_len)):
        return None, ctypes.get_last_error()
    full_path = buf.value
    if not full_path:
        return None, 0
    return os.path.basename(full_path).lower(), 0


def _get_current_session_id() -> Optional[int]:
    if _kernel32 is None:
        return None
    sess_id = wintypes.DWORD(0)
    cur_pid = _kernel32.GetCurrentProcessId()
    if not _kernel32.ProcessIdToSessionId(cur_pid, ctypes.byref(sess_id)):
        return None
    return int(sess_id.value)


def find_process_by_lock_path(
    lock_path: str,
    allowed_images: Optional[Sequence[str]] = None,
    *,
    same_session_only: bool = True,
) -> Optional[tuple[int, int]]:
    """
    Find the single verified alive process holding `lock_path` and return
    `(pid, start_time_u64)`. Returns `None` if not found, ambiguous (>=2 PIDs),
    or verification fails (fail-closed).
    """
    if sys.platform != "win32" or _rstrtmgr is None or _kernel32 is None:
        return None
    if not lock_path or not isinstance(lock_path, str):
        return None

    allowed_set = _normalize_allowed_images(allowed_images)
    if allowed_set is not None and len(allowed_set) == 0:
        return None

    self_session: Optional[int] = None
    if same_session_only:
        self_session = _get_current_session_id()
        if self_session is None:
            return None

    session_handle = wintypes.DWORD(0)
    session_key = (wintypes.WCHAR * (CCH_RM_SESSION_KEY + 1))()
    res = _rstrtmgr.RmStartSession(ctypes.byref(session_handle), 0, session_key)
    if res != 0:
        return None

    try:
        files = (wintypes.LPCWSTR * 1)(str(lock_path))
        res = _rstrtmgr.RmRegisterResources(
            session_handle, 1, files, 0, None, 0, None
        )
        if res != 0:
            return None

        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reboot_reasons = wintypes.DWORD(0)

        res = _rstrtmgr.RmGetList(
            session_handle,
            ctypes.byref(needed),
            ctypes.byref(count),
            None,
            ctypes.byref(reboot_reasons),
        )
        if needed.value == 0:
            return None
        if res not in (0, ERROR_MORE_DATA):
            return None

        proc_info_array = None
        for _ in range(MAX_RM_RETRIES):
            alloc_count = int(needed.value)
            if alloc_count <= 0 or alloc_count > MAX_RM_PROCS:
                return None
            proc_info_array = (RM_PROCESS_INFO * alloc_count)()
            count.value = alloc_count
            res = _rstrtmgr.RmGetList(
                session_handle,
                ctypes.byref(needed),
                ctypes.byref(count),
                proc_info_array,
                ctypes.byref(reboot_reasons),
            )
            if res == 0:
                break
            if res != ERROR_MORE_DATA:
                return None
        else:
            return None

        if proc_info_array is None or count.value == 0:
            return None

        verified_candidates: dict[int, int] = {}
        for i in range(int(count.value)):
            entry = proc_info_array[i]
            cand_pid = int(entry.Process.dwProcessId)
            cand_ft = _filetime_to_u64(entry.Process.ProcessStartTime)
            if cand_pid <= 0 or cand_ft <= 0:
                continue

            if same_session_only and self_session is not None:
                if int(entry.TSSessionId) != self_session:
                    continue

            h_proc = _kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                False,
                cand_pid,
            )
            if not _is_valid_handle(h_proc):
                continue

            try:
                if _kernel32.WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                    continue

                c_ft = FILETIME()
                e_ft = FILETIME()
                k_ft = FILETIME()
                u_ft = FILETIME()
                if not _kernel32.GetProcessTimes(
                    h_proc,
                    ctypes.byref(c_ft),
                    ctypes.byref(e_ft),
                    ctypes.byref(k_ft),
                    ctypes.byref(u_ft),
                ):
                    continue

                actual_ft = _filetime_to_u64(c_ft)
                if actual_ft <= 0 or actual_ft != cand_ft:
                    continue

                if allowed_set is not None:
                    exe_base, _ = _query_image_basename_lower(h_proc)
                    if not exe_base or exe_base not in allowed_set:
                        continue

                verified_candidates[cand_pid] = actual_ft
            finally:
                _kernel32.CloseHandle(h_proc)

        if len(verified_candidates) != 1:
            return None

        only_pid, only_ft = next(iter(verified_candidates.items()))
        return (only_pid, only_ft)
    except Exception:
        return None
    finally:
        _rstrtmgr.RmEndSession(session_handle)


def find_pid_by_lock_path(
    lock_path: str,
    allowed_images: Optional[Sequence[str]] = None,
) -> Optional[int]:
    """Compatibility wrapper returning only `pid` from `find_process_by_lock_path`."""
    identity = find_process_by_lock_path(lock_path, allowed_images=allowed_images)
    if identity is None:
        return None
    return identity[0]


def _make_key_record(
    ch: str,
    vk_code: int,
    scan_code: int,
    key_down: bool,
) -> INPUT_RECORD:
    rec = INPUT_RECORD()
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = 1 if key_down else 0
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk_code
    rec.Event.KeyEvent.wVirtualScanCode = scan_code
    rec.Event.KeyEvent.uChar.UnicodeChar = ch
    rec.Event.KeyEvent.dwControlKeyState = 0
    return rec


def _has_visible_text(line_prefix: str) -> bool:
    """
    Condition B: Skip leading prompt run (`>` at col 0 plus contiguous 0x20 spaces),
    then return True if any remaining cell satisfies `ord(ch) > 0x20 and ord(ch) != 0x7F`.
    """
    if not line_prefix:
        return False
    start = 0
    n = len(line_prefix)
    if line_prefix[0] == ">":
        start = 1
        while start < n and line_prefix[start] == " ":
            start += 1
    for ch in line_prefix[start:]:
        code = ord(ch)
        if code > 0x20 and code != 0x7F:
            return True
    return False


def _has_pending_console_key_input(h_conin: int) -> tuple[Optional[bool], int]:
    """
    Check 1: Detect queued KEY_EVENT (bKeyDown=True) on CONIN$ via PeekConsoleInputW.
    Returns `(is_typing_or_None_on_error, win32_error)`.
    """
    get_num = getattr(_kernel32, "GetNumberOfConsoleInputEvents", None)
    peek_fn = getattr(_kernel32, "PeekConsoleInputW", None)
    if not callable(get_num) or not callable(peek_fn):
        return (False, 0)

    num_events = wintypes.DWORD(0)
    if not get_num(h_conin, ctypes.byref(num_events)):
        return (None, ctypes.get_last_error())

    count = max(1, min(int(num_events.value), MAX_PEEK_INPUT_EVENTS))
    buf = (INPUT_RECORD * count)()
    read_count = wintypes.DWORD(0)
    if not peek_fn(h_conin, buf, count, ctypes.byref(read_count)):
        return (None, ctypes.get_last_error())

    actual = min(int(read_count.value), count)
    for i in range(actual):
        rec = buf[i]
        if rec.EventType == KEY_EVENT and bool(rec.Event.KeyEvent.bKeyDown):
            return (True, 0)
    return (False, 0)


def _has_active_cursor_or_input_line(h_conout: int) -> tuple[Optional[bool], int]:
    """
    Check 2 (2-B standalone): Sample console buffer (150ms interval) and return True if
    cursor X > 0 and cols 0..X-1 contain any visible char (> 0x20 and != 0x7F, including wide/non-ASCII).
    Protects paused/thinking users even when cursor is stationary.
    Returns `(is_typing_or_None_on_error, win32_error)`.
    """
    get_csbi = getattr(_kernel32, "GetConsoleScreenBufferInfo", None)
    read_chars = getattr(_kernel32, "ReadConsoleOutputCharacterW", None)
    if not callable(get_csbi) or not callable(read_chars):
        return (False, 0)
    if not _is_valid_handle(h_conout):
        return (None, 0)

    csbi_1 = CONSOLE_SCREEN_BUFFER_INFO()
    if not get_csbi(h_conout, ctypes.byref(csbi_1)):
        return (None, ctypes.get_last_error())

    time.sleep(TYPING_CURSOR_SAMPLE_SEC)

    csbi_2 = CONSOLE_SCREEN_BUFFER_INFO()
    if not get_csbi(h_conout, ctypes.byref(csbi_2)):
        return (None, ctypes.get_last_error())

    cur_x = int(csbi_2.dwCursorPosition.X)
    cur_y = int(csbi_2.dwCursorPosition.Y)
    if cur_x <= 0 or cur_y < 0:
        return (False, 0)

    read_len = min(cur_x, MAX_CONSOLE_READ_COLS)
    char_buf = ctypes.create_unicode_buffer(read_len + 1)
    chars_read = wintypes.DWORD(0)
    coord = COORD(0, cur_y)
    if not read_chars(h_conout, char_buf, read_len, coord, ctypes.byref(chars_read)):
        return (None, ctypes.get_last_error())

    actual_len = min(int(chars_read.value), read_len)
    if actual_len <= 0:
        return (False, 0)

    return (_has_visible_text(char_buf[:actual_len]), 0)


def _is_user_typing_with_retries(h_proc: int, h_conin: int) -> tuple[str, int]:
    """
    Run User-Typing Collision Guard with bounded retries (max 2 retries at 0.4s).
    Returns `(status_code, win32_error)` where status_code is one of:
    - `"ok"`: clear to inject
    - `"deferred_user_typing"`: still typing after retries
    - `"process_already_exited"`: target exited during retry wait
    - `"user_typing_check_failed"`: Win32 console inspection API failed
    """
    can_check_events = callable(
        getattr(_kernel32, "GetNumberOfConsoleInputEvents", None)
    ) and callable(getattr(_kernel32, "PeekConsoleInputW", None))
    can_check_screen = callable(
        getattr(_kernel32, "GetConsoleScreenBufferInfo", None)
    ) and callable(getattr(_kernel32, "ReadConsoleOutputCharacterW", None))
    if not can_check_events and not can_check_screen:
        return ("ok", 0)

    h_conout = None
    try:
        for attempt in range(TYPING_MAX_RETRIES + 1):
            if attempt > 0 and _kernel32.WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                return ("process_already_exited", 0)

            typing_now = False
            if can_check_events:
                ev_typing, ev_err = _has_pending_console_key_input(h_conin)
                if ev_typing is None:
                    return ("user_typing_check_failed", ev_err)
                typing_now = ev_typing

            if can_check_screen:
                if h_conout is None:
                    h_conout = _kernel32.CreateFileW(
                        "CONOUT$",
                        GENERIC_READ,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        None,
                        OPEN_EXISTING,
                        0,
                        None,
                    )
                    if not _is_valid_handle(h_conout):
                        return ("user_typing_check_failed", ctypes.get_last_error())
                scr_typing, scr_err = _has_active_cursor_or_input_line(h_conout)
                if scr_typing is None:
                    return ("user_typing_check_failed", scr_err)
                typing_now = typing_now or scr_typing

            if not typing_now:
                return ("ok", 0)

            if attempt < TYPING_MAX_RETRIES:
                time.sleep(TYPING_RETRY_DELAY_SEC)

        return ("deferred_user_typing", 0)
    finally:
        if _is_valid_handle(h_conout):
            _kernel32.CloseHandle(h_conout)


def inject_console_input_verified(
    target_pid: int,
    expected_start_time: int,
    text: str = "進めて",
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
    *,
    allowed_images: Optional[Sequence[str]] = None,
    respect_user_typing: bool = True,
) -> tuple[bool, str, int]:
    """
    Kernel HANDLE-pinned & ProcessStartTime-verified CONIN$ injection.
    Returns `(ok, reason_code, win32_last_error)`.
    """
    if (
        not isinstance(target_pid, int)
        or isinstance(target_pid, bool)
        or target_pid <= 0
        or not isinstance(expected_start_time, int)
        or isinstance(expected_start_time, bool)
        or expected_start_time <= 0
    ):
        return (False, "invalid_pid_or_start_time", 0)

    if sys.platform != "win32" or _kernel32 is None or _user32 is None:
        return (False, "win_console_unavailable", 0)

    allowed_set = _normalize_allowed_images(allowed_images)
    if allowed_set is not None and len(allowed_set) == 0:
        return (False, "image_name_mismatch", 0)

    # [Step 1] Pin target PID in Windows kernel by holding a process HANDLE
    h_proc = _kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
        False,
        target_pid,
    )
    if not _is_valid_handle(h_proc):
        return (False, "open_process_failed", ctypes.get_last_error())

    try:
        # [Step 2] Non-blocking liveness check
        if _kernel32.WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
            return (False, "process_already_exited", 0)

        # [Step 3] Exact creation time check against expected_start_time (64-bit FILETIME)
        c_ft = FILETIME()
        e_ft = FILETIME()
        k_ft = FILETIME()
        u_ft = FILETIME()
        if not _kernel32.GetProcessTimes(
            h_proc,
            ctypes.byref(c_ft),
            ctypes.byref(e_ft),
            ctypes.byref(k_ft),
            ctypes.byref(u_ft),
        ):
            return (False, "get_process_times_failed", ctypes.get_last_error())

        actual_start_time = _filetime_to_u64(c_ft)
        if actual_start_time <= 0 or actual_start_time != expected_start_time:
            return (False, "pid_reused_start_time_mismatch", 0)

        # [Step 4] Executable image basename verification (when allowed_images is specified)
        if allowed_set is not None:
            exe_base, err = _query_image_basename_lower(h_proc)
            if exe_base is None:
                return (False, "image_query_failed", err)
            if exe_base not in allowed_set:
                return (False, "image_name_mismatch", 0)

        # [Step 5] AttachConsole + WriteConsoleInputW while STILL holding h_proc
        _kernel32.FreeConsole()
        if not _kernel32.AttachConsole(target_pid):
            err = ctypes.get_last_error()
            _kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
            return (False, "attach_console_failed", err)

        h_conin = None
        try:
            h_conin = _kernel32.CreateFileW(
                "CONIN$",
                GENERIC_READ | GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )
            if not _is_valid_handle(h_conin):
                return (False, "conin_open_failed", ctypes.get_last_error())

            if _kernel32.WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                return (False, "process_already_exited", 0)

            if respect_user_typing:
                guard_status, guard_err = _is_user_typing_with_retries(h_proc, h_conin)
                if guard_status != "ok":
                    return (False, guard_status, guard_err)

            if text:
                utf16_units = text.encode("utf-16-le")
                unit_count = len(utf16_units) // 2
                if unit_count > 0:
                    total_events = unit_count * 2
                    records = (INPUT_RECORD * total_events)()
                    for idx in range(unit_count):
                        code_unit = int.from_bytes(
                            utf16_units[idx * 2 : idx * 2 + 2], "little"
                        )
                        ch = chr(code_unit)
                        records[idx * 2] = _make_key_record(ch, 0, 0, True)
                        records[idx * 2 + 1] = _make_key_record(ch, 0, 0, False)

                    written = wintypes.DWORD(0)
                    if not _kernel32.WriteConsoleInputW(
                        h_conin,
                        records,
                        total_events,
                        ctypes.byref(written),
                    ):
                        return (
                            False,
                            "write_console_input_failed",
                            ctypes.get_last_error(),
                        )

            if send_enter:
                if submit_delay_sec > 0:
                    time.sleep(submit_delay_sec)

                scan_return = int(
                    _user32.MapVirtualKeyW(VK_RETURN, MAPVK_VK_TO_VSC)
                ) & 0xFFFF
                enter_records = (INPUT_RECORD * 2)(
                    _make_key_record("\r", VK_RETURN, scan_return, True),
                    _make_key_record("\r", VK_RETURN, scan_return, False),
                )
                written = wintypes.DWORD(0)
                if not _kernel32.WriteConsoleInputW(
                    h_conin,
                    enter_records,
                    2,
                    ctypes.byref(written),
                ):
                    return (
                        False,
                        "write_console_input_failed",
                        ctypes.get_last_error(),
                    )

            return (True, "ok", 0)
        finally:
            if _is_valid_handle(h_conin):
                _kernel32.CloseHandle(h_conin)
            _kernel32.FreeConsole()
            _kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
    finally:
        # [Step 6] Close pinned process handle ONLY after console detach/restore
        _kernel32.CloseHandle(h_proc)


def inject_console_input(
    target_pid: int,
    text: str = "進めて",
    send_enter: bool = True,
    submit_delay_sec: float = 0.08,
    *,
    expected_start_time: Optional[int] = None,
    allowed_images: Optional[Sequence[str]] = None,
    respect_user_typing: bool = True,
) -> bool:
    """
    Boolean-returning entrypoint.
    Fail-closed default: returns `False` immediately if `expected_start_time` is
    omitted (`None`) or `<= 0`.
    """
    if (
        expected_start_time is None
        or not isinstance(expected_start_time, int)
        or isinstance(expected_start_time, bool)
        or expected_start_time <= 0
    ):
        return False

    ok, _, _ = inject_console_input_verified(
        target_pid,
        expected_start_time,
        text,
        send_enter,
        submit_delay_sec,
        allowed_images=allowed_images,
        respect_user_typing=respect_user_typing,
    )
    return ok
