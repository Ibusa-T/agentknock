# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# distutils: libraries = kernel32 user32 rstrtmgr
# Copyright (c) 2026 Ibusa-T
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Antigravity 別窓エージェント バックグラウンド入力注入 Cython ネイティブ拡張 (_win_console.pyx)

特徴 (v0.3.1 TOCTOU Hardened):
1. Windows SDK の `<windows.h>` および `<restartmanager.h>` を直接インクルード。
2. `find_process_by_lock_path`: Restart Manager API (`RmGetList`) + `ERROR_MORE_DATA` 有界リトライ +
   `OpenProcess` / `WaitForSingleObject` / `GetProcessTimes` / `QueryFullProcessImageNameW` /
   `ProcessIdToSessionId` により、単一の適格プロセス `(pid, start_time_u64)` を厳密特定。
3. `inject_console_input_verified`: `OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE)`
   で対象プロセス HANDLE を最外層 `finally` までピン留めした状態で生存・開始時刻・実行イメージ名を照合し、
   GIL 完全解放 (`with nogil:`) で `AttachConsole` + `WriteConsoleInputW` を実行。
"""

import os
from libc.stdlib cimport malloc, free
from libc.string cimport memset
from libc.stddef cimport wchar_t
from libc.stdint cimport uint64_t
from cpython.mem cimport PyMem_Free

_BACKEND = "cython"
DEFAULT_ALLOWED_IMAGES = ("agy.exe",)

cdef extern from "Python.h":
    wchar_t* PyUnicode_AsWideCharString(object unicode, Py_ssize_t *size)
    object PyUnicode_FromWideChar(const wchar_t *w, Py_ssize_t size)

cdef extern from *:
    """
    #define CONIN_DEVICE_NAME L"CONIN$"
    #define CONOUT_DEVICE_NAME L"CONOUT$"
    #define WCHAR_CR L'\\r'
    """
    cdef const wchar_t* CONIN_DEVICE_NAME
    cdef const wchar_t* CONOUT_DEVICE_NAME
    cdef wchar_t WCHAR_CR


# ============================================================================
# 1. Windows SDK ヘッダの直接インクルード (<windows.h> / <restartmanager.h>)
# ============================================================================

cdef extern from "<windows.h>" nogil:
    ctypedef unsigned long DWORD
    ctypedef unsigned short WORD
    ctypedef short SHORT
    ctypedef unsigned int UINT
    ctypedef unsigned long ULONG
    ctypedef int BOOL
    ctypedef void* HANDLE
    ctypedef const wchar_t* LPCWSTR
    ctypedef wchar_t* LPWSTR
    ctypedef wchar_t WCHAR
    ctypedef char CHAR

    cdef DWORD ATTACH_PARENT_PROCESS
    cdef DWORD GENERIC_READ
    cdef DWORD GENERIC_WRITE
    cdef DWORD FILE_SHARE_READ
    cdef DWORD FILE_SHARE_WRITE
    cdef DWORD OPEN_EXISTING
    cdef HANDLE INVALID_HANDLE_VALUE

    cdef DWORD PROCESS_QUERY_LIMITED_INFORMATION
    cdef DWORD SYNCHRONIZE
    cdef DWORD WAIT_OBJECT_0
    cdef DWORD WAIT_TIMEOUT
    cdef DWORD ERROR_MORE_DATA

    cdef WORD KEY_EVENT
    cdef WORD VK_RETURN
    cdef UINT MAPVK_VK_TO_VSC

    ctypedef struct FILETIME:
        DWORD dwLowDateTime
        DWORD dwHighDateTime

    ctypedef union _CHAR_UNION:
        WCHAR UnicodeChar
        CHAR AsciiChar

    ctypedef struct KEY_EVENT_RECORD:
        BOOL bKeyDown
        WORD wRepeatCount
        WORD wVirtualKeyCode
        WORD wVirtualScanCode
        _CHAR_UNION uChar
        DWORD dwControlKeyState

    ctypedef union _EVENT_UNION:
        KEY_EVENT_RECORD KeyEvent

    ctypedef struct INPUT_RECORD:
        WORD EventType
        _EVENT_UNION Event

    ctypedef struct COORD:
        SHORT X
        SHORT Y

    ctypedef struct SMALL_RECT:
        SHORT Left
        SHORT Top
        SHORT Right
        SHORT Bottom

    ctypedef struct CONSOLE_SCREEN_BUFFER_INFO:
        COORD dwSize
        COORD dwCursorPosition
        WORD wAttributes
        SMALL_RECT srWindow
        COORD dwMaximumWindowSize

    HANDLE OpenProcess(DWORD dwDesiredAccess, BOOL bInheritHandle, DWORD dwProcessId)
    DWORD WaitForSingleObject(HANDLE hHandle, DWORD dwMilliseconds)
    BOOL GetProcessTimes(
        HANDLE hProcess,
        FILETIME* lpCreationTime,
        FILETIME* lpExitTime,
        FILETIME* lpKernelTime,
        FILETIME* lpUserTime,
    )
    BOOL QueryFullProcessImageNameW(
        HANDLE hProcess,
        DWORD dwFlags,
        LPWSTR lpExeName,
        DWORD* lpdwSize,
    )
    DWORD GetLastError()
    DWORD GetCurrentProcessId()
    BOOL ProcessIdToSessionId(DWORD dwProcessId, DWORD* pSessionId)

    BOOL FreeConsole()
    BOOL AttachConsole(DWORD dwProcessId)
    HANDLE CreateFileW(
        LPCWSTR lpFileName,
        DWORD dwDesiredAccess,
        DWORD dwShareMode,
        void* lpSecurityAttributes,
        DWORD dwCreationDisposition,
        DWORD dwFlagsAndAttributes,
        HANDLE hTemplateFile,
    )
    BOOL WriteConsoleInputW(
        HANDLE hConsoleInput,
        const INPUT_RECORD* lpBuffer,
        DWORD nLength,
        DWORD* lpNumberOfEventsWritten,
    )
    BOOL GetNumberOfConsoleInputEvents(
        HANDLE hConsoleInput,
        DWORD* lpcNumberOfEvents,
    )
    BOOL PeekConsoleInputW(
        HANDLE hConsoleInput,
        INPUT_RECORD* lpBuffer,
        DWORD nLength,
        DWORD* lpNumberOfEventsRead,
    )
    BOOL GetConsoleScreenBufferInfo(
        HANDLE hConsoleOutput,
        CONSOLE_SCREEN_BUFFER_INFO* lpConsoleScreenBufferInfo,
    )
    BOOL ReadConsoleOutputCharacterW(
        HANDLE hConsoleOutput,
        LPWSTR lpCharacter,
        DWORD nLength,
        COORD dwReadCoord,
        DWORD* lpNumberOfCharsRead,
    )
    BOOL CloseHandle(HANDLE hObject)
    void Sleep(DWORD dwMilliseconds)
    UINT MapVirtualKeyW(UINT uCode, UINT uMapType)


cdef extern from "<restartmanager.h>" nogil:
    enum:
        CCH_RM_SESSION_KEY
        CCH_RM_MAX_APP_NAME
        CCH_RM_MAX_SVC_NAME

    ctypedef struct RM_UNIQUE_PROCESS:
        DWORD dwProcessId
        FILETIME ProcessStartTime

    ctypedef struct RM_PROCESS_INFO:
        RM_UNIQUE_PROCESS Process
        WCHAR strAppName[CCH_RM_MAX_APP_NAME + 1]
        WCHAR strServiceShortName[CCH_RM_MAX_SVC_NAME + 1]
        int ApplicationType
        ULONG AppStatus
        DWORD TSSessionId
        BOOL bRestartable

    DWORD RmStartSession(
        DWORD* pSessionHandle,
        DWORD dwSessionFlags,
        WCHAR strSessionKey[],
    )
    DWORD RmRegisterResources(
        DWORD dwSessionHandle,
        UINT nFiles,
        LPCWSTR rgsFileNames[],
        UINT nApplications,
        RM_UNIQUE_PROCESS rgApplications[],
        UINT nServices,
        LPCWSTR rgsServiceNames[],
    )
    DWORD RmGetList(
        DWORD dwSessionHandle,
        UINT* pnProcInfoNeeded,
        UINT* pnProcInfo,
        RM_PROCESS_INFO rgAffectedApps[],
        DWORD* lpdwRebootReasons,
    )
    DWORD RmEndSession(DWORD dwSessionHandle)


cdef inline uint64_t _ft_to_u64(FILETIME ft) noexcept nogil:
    return ((<uint64_t>ft.dwHighDateTime) << 32) | (<uint64_t>ft.dwLowDateTime)


def _normalize_allowed_images(object allowed_images):
    if allowed_images is None:
        return None
    return {os.path.basename(str(img)).strip().lower() for img in allowed_images if str(img).strip()}


cdef inline bint _match_allowed_w_nogil(
    const wchar_t* eb,
    Py_ssize_t eb_len,
    const wchar_t* allowed,
    Py_ssize_t allowed_len,
) noexcept nogil:
    """nogil C comparison: exact match OR `<allowed>.<1+ ASCII digits>.old`."""
    cdef Py_ssize_t i = 0
    cdef Py_ssize_t middle_start = 0
    cdef Py_ssize_t middle_end = 0
    cdef wchar_t c = 0
    if eb == NULL or allowed == NULL or eb_len <= 0 or allowed_len <= 0:
        return 0
    if eb_len == allowed_len:
        for i in range(eb_len):
            if eb[i] != allowed[i]:
                return 0
        return 1
    if eb_len < allowed_len + 6:
        return 0
    for i in range(allowed_len):
        if eb[i] != allowed[i]:
            return 0
    if eb[allowed_len] != <wchar_t>46:  # '.'
        return 0
    if (
        eb[eb_len - 4] != <wchar_t>46
        or eb[eb_len - 3] != <wchar_t>111
        or eb[eb_len - 2] != <wchar_t>108
        or eb[eb_len - 1] != <wchar_t>100
    ):
        return 0
    middle_start = allowed_len + 1
    middle_end = eb_len - 4
    if middle_end <= middle_start:
        return 0
    for i in range(middle_start, middle_end):
        c = eb[i]
        if c < <wchar_t>48 or c > <wchar_t>57:
            return 0
    return 1


def _image_matches_allowed(object exe_base, object allowed_set):
    """Return True if `exe_base` matches `allowed_set` or `<allowed>.<digits>.old`."""
    if not exe_base or not allowed_set:
        return False
    cdef str eb = str(exe_base).lower()
    cdef Py_ssize_t eb_len = 0
    cdef wchar_t* eb_w = PyUnicode_AsWideCharString(eb, &eb_len)
    if eb_w == NULL:
        raise MemoryError("Failed to allocate wide char string")
    cdef Py_ssize_t allowed_len = 0
    cdef wchar_t* allowed_w = NULL
    cdef bint matched = 0
    try:
        for allowed in allowed_set:
            if not isinstance(allowed, str) or not allowed:
                continue
            allowed_w = PyUnicode_AsWideCharString(<str>allowed, &allowed_len)
            if allowed_w == NULL:
                raise MemoryError("Failed to allocate wide char string")
            with nogil:
                matched = _match_allowed_w_nogil(eb_w, eb_len, allowed_w, allowed_len)
            PyMem_Free(allowed_w)
            allowed_w = NULL
            if matched:
                return True
        return False
    finally:
        if allowed_w != NULL:
            PyMem_Free(allowed_w)
        PyMem_Free(eb_w)


def _query_image_basename_py(unsigned long pid):
    """Helper using OpenProcess + QueryFullProcessImageNameW for Python-side allowlist check."""
    cdef HANDLE h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, <DWORD>pid)
    if h == NULL or h == INVALID_HANDLE_VALUE:
        return None
    cdef WCHAR buf[1024]
    cdef DWORD buf_size = 1024
    try:
        if not QueryFullProcessImageNameW(h, 0, buf, &buf_size):
            return None
        path_str = PyUnicode_FromWideChar(buf, <Py_ssize_t>buf_size)
        return os.path.basename(path_str).lower()
    finally:
        CloseHandle(h)


# ============================================================================
# 2. Restart Manager + HANDLE 検証によるプロセス逆引き実装
# ============================================================================

cpdef object find_process_by_lock_path(
    str lock_path,
    object allowed_images = None,
    bint same_session_only = True,
):
    """
    lock_path を保持する単一の検証済み生存プロセス `(pid, start_time_u64)` を返す。
    該当なし・複数候補衝突・検証失敗時は None を返す (fail-closed)。
    """
    if not lock_path:
        return None

    allowed_set = _normalize_allowed_images(allowed_images)
    if allowed_set is not None and len(allowed_set) == 0:
        return None

    cdef Py_ssize_t size = 0
    cdef wchar_t* lock_path_w = PyUnicode_AsWideCharString(lock_path, &size)
    if lock_path_w == NULL:
        raise MemoryError("Failed to allocate wide char string for lock_path")

    cdef DWORD session = 0
    cdef WCHAR session_key[CCH_RM_SESSION_KEY + 1]
    memset(session_key, 0, sizeof(session_key))

    cdef LPCWSTR resources[1]
    resources[0] = lock_path_w

    cdef UINT needed = 0
    cdef UINT count = 0
    cdef DWORD reason = 0
    cdef DWORD res = 0
    cdef int retry = 0
    cdef UINT i = 0
    cdef RM_PROCESS_INFO* process_infos = NULL

    cdef DWORD self_session = 0
    cdef BOOL has_self_session = ProcessIdToSessionId(GetCurrentProcessId(), &self_session)
    if same_session_only and not has_self_session:
        PyMem_Free(lock_path_w)
        return None

    cdef DWORD cand_pid = 0
    cdef uint64_t cand_ft = 0
    cdef uint64_t actual_ft = 0
    cdef HANDLE h_proc = NULL
    cdef FILETIME c_ft, e_ft, k_ft, u_ft
    cdef WCHAR exe_buf[1024]
    cdef DWORD exe_len = 1024

    verified = {}

    res = RmStartSession(&session, 0, session_key)
    if res != 0:
        PyMem_Free(lock_path_w)
        return None

    try:
        if RmRegisterResources(session, 1, resources, 0, NULL, 0, NULL) != 0:
            return None

        res = RmGetList(session, &needed, &count, NULL, &reason)
        if needed == 0 or (res != 0 and res != ERROR_MORE_DATA):
            return None

        for retry in range(3):
            if needed == 0 or needed > 256:
                return None
            if process_infos != NULL:
                free(process_infos)
                process_infos = NULL
            process_infos = <RM_PROCESS_INFO*>malloc(sizeof(RM_PROCESS_INFO) * needed)
            if process_infos == NULL:
                return None
            count = needed
            res = RmGetList(session, &needed, &count, process_infos, &reason)
            if res == 0:
                break
            if res != ERROR_MORE_DATA:
                return None
        else:
            return None

        for i in range(count):
            cand_pid = process_infos[i].Process.dwProcessId
            cand_ft = _ft_to_u64(process_infos[i].Process.ProcessStartTime)
            if cand_pid == 0 or cand_ft == 0:
                continue
            if same_session_only and process_infos[i].TSSessionId != self_session:
                continue

            h_proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, cand_pid)
            if h_proc == NULL or h_proc == INVALID_HANDLE_VALUE:
                continue
            try:
                if WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                    continue
                if not GetProcessTimes(h_proc, &c_ft, &e_ft, &k_ft, &u_ft):
                    continue
                actual_ft = _ft_to_u64(c_ft)
                if actual_ft == 0 or actual_ft != cand_ft:
                    continue

                if allowed_set is not None:
                    exe_len = 1024
                    if not QueryFullProcessImageNameW(h_proc, 0, exe_buf, &exe_len):
                        continue
                    exe_path = PyUnicode_FromWideChar(exe_buf, <Py_ssize_t>exe_len)
                    if not _image_matches_allowed(os.path.basename(exe_path), allowed_set):
                        continue

                verified[<int>cand_pid] = <object>actual_ft
            finally:
                CloseHandle(h_proc)

        if len(verified) != 1:
            return None
        return next(iter(verified.items()))
    finally:
        if process_infos != NULL:
            free(process_infos)
        RmEndSession(session)
        PyMem_Free(lock_path_w)


cpdef object find_pid_by_lock_path(str lock_path, object allowed_images = None):
    """互換ラッパー: find_process_by_lock_path から PID (int) のみを返す。"""
    identity = find_process_by_lock_path(lock_path, allowed_images=allowed_images)
    if identity is None:
        return None
    return identity[0]


# ============================================================================
# 3. C ネイティブ (nogil) + HANDLE ピン留めによる CONIN$ 入力注入実装
# ============================================================================

cdef inline void _init_key_record(
    INPUT_RECORD* rec,
    WCHAR ch,
    WORD vk_code,
    WORD scan_code,
    BOOL key_down,
) noexcept nogil:
    memset(rec, 0, sizeof(INPUT_RECORD))
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = key_down
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk_code
    rec.Event.KeyEvent.wVirtualScanCode = scan_code
    rec.Event.KeyEvent.uChar.UnicodeChar = ch
    rec.Event.KeyEvent.dwControlKeyState = 0


cdef inline bint _has_visible_w(const WCHAR* buf, DWORD length) noexcept nogil:
    cdef DWORD i = 0
    cdef unsigned int u = 0
    if buf == NULL or length == 0:
        return 0
    if <unsigned int>buf[0] == 0x3E:
        i = 1
        while i < length and <unsigned int>buf[i] == 0x20:
            i += 1
    while i < length:
        u = <unsigned int>buf[i]
        if u > 0x20 and u != 0x7F:
            return 1
        i += 1
    return 0


cdef int _has_pending_console_key_input_c(HANDLE h_conin, DWORD* out_err) noexcept nogil:
    """Returns 1 if pending key-down found, 0 if none, -1 on Win32 error."""
    cdef DWORD num_events = 0
    cdef DWORD count = 0
    cdef DWORD read_count = 0
    cdef DWORD actual = 0
    cdef DWORD i = 0
    cdef INPUT_RECORD peek_buf[64]

    if not GetNumberOfConsoleInputEvents(h_conin, &num_events):
        out_err[0] = GetLastError()
        return -1
    if num_events == 0:
        count = 1
    elif num_events <= 64:
        count = num_events
    else:
        count = 64
    if not PeekConsoleInputW(h_conin, peek_buf, count, &read_count):
        out_err[0] = GetLastError()
        return -1
    actual = read_count if read_count <= count else count
    for i in range(actual):
        if peek_buf[i].EventType == KEY_EVENT and peek_buf[i].Event.KeyEvent.bKeyDown:
            return 1
    return 0


cdef int _has_active_cursor_or_input_line_c(HANDLE h_conout, DWORD* out_err) noexcept nogil:
    """
    Check 2 (2-B standalone): Sample console buffer (150ms) and return 1 if
    X > 0 and cols 0..X-1 contain any visible char (> 0x20 and != 0x7F, including wide/non-ASCII).
    Returns 1 if active typing detected, 0 if idle, -1 on Win32 error.
    """
    cdef CONSOLE_SCREEN_BUFFER_INFO csbi_1
    cdef CONSOLE_SCREEN_BUFFER_INFO csbi_2
    cdef DWORD read_len = 0
    cdef DWORD chars_read = 0
    cdef DWORD actual_len = 0
    cdef COORD read_coord
    cdef WCHAR* char_buf = NULL
    cdef bint active = 0

    if h_conout == NULL or h_conout == INVALID_HANDLE_VALUE:
        out_err[0] = 0
        return -1
    memset(&csbi_1, 0, sizeof(CONSOLE_SCREEN_BUFFER_INFO))
    if not GetConsoleScreenBufferInfo(h_conout, &csbi_1):
        out_err[0] = GetLastError()
        return -1

    Sleep(150)

    memset(&csbi_2, 0, sizeof(CONSOLE_SCREEN_BUFFER_INFO))
    if not GetConsoleScreenBufferInfo(h_conout, &csbi_2):
        out_err[0] = GetLastError()
        return -1

    if csbi_2.dwCursorPosition.X <= 0 or csbi_2.dwCursorPosition.Y < 0:
        return 0

    read_len = <DWORD>csbi_2.dwCursorPosition.X
    if read_len > 4096:
        read_len = 4096

    char_buf = <WCHAR*>malloc(sizeof(WCHAR) * (read_len + 1))
    if char_buf == NULL:
        out_err[0] = 0
        return -1
    read_coord.X = 0
    read_coord.Y = csbi_2.dwCursorPosition.Y
    if not ReadConsoleOutputCharacterW(h_conout, char_buf, read_len, read_coord, &chars_read):
        out_err[0] = GetLastError()
        free(char_buf)
        return -1
    actual_len = chars_read if chars_read <= read_len else read_len
    if actual_len > 0:
        active = _has_visible_w(char_buf, actual_len)
    free(char_buf)
    return 1 if active else 0


cdef int _check_user_typing_with_retries_c(
    HANDLE h_proc,
    HANDLE h_conin,
    DWORD* out_err,
) noexcept nogil:
    """
    Returns nogil_status:
    - 0: clear to inject
    - 3: process_already_exited
    - 5: deferred_user_typing
    - 6: user_typing_check_failed (with out_err populated)
    """
    cdef int attempt = 0
    cdef int ev_res = 0
    cdef int scr_res = 0
    cdef bint typing_now = 0
    cdef HANDLE h_conout = INVALID_HANDLE_VALUE
    cdef bint conout_tried = 0

    out_err[0] = 0
    for attempt in range(3):
        if attempt > 0:
            if WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                if h_conout != INVALID_HANDLE_VALUE and h_conout != NULL:
                    CloseHandle(h_conout)
                return 3

        typing_now = 0
        ev_res = _has_pending_console_key_input_c(h_conin, out_err)
        if ev_res < 0:
            if h_conout != INVALID_HANDLE_VALUE and h_conout != NULL:
                CloseHandle(h_conout)
            return 6
        if ev_res > 0:
            typing_now = 1
        if not conout_tried:
            conout_tried = 1
            h_conout = CreateFileW(
                CONOUT_DEVICE_NAME,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                NULL,
                OPEN_EXISTING,
                0,
                NULL,
            )
            if h_conout == INVALID_HANDLE_VALUE or h_conout == NULL:
                out_err[0] = GetLastError()
                return 6
        scr_res = _has_active_cursor_or_input_line_c(h_conout, out_err)
        if scr_res < 0:
            CloseHandle(h_conout)
            return 6
        if scr_res > 0:
            typing_now = 1

        if not typing_now:
            if h_conout != INVALID_HANDLE_VALUE and h_conout != NULL:
                CloseHandle(h_conout)
            return 0

        if attempt < 2:
            Sleep(400)

    if h_conout != INVALID_HANDLE_VALUE and h_conout != NULL:
        CloseHandle(h_conout)
    return 5


cpdef tuple inject_console_input_verified(
    object target_pid,
    object expected_start_time,
    str text = "進めて",
    bint send_enter = True,
    double submit_delay_sec = 0.08,
    object allowed_images = None,
    bint respect_user_typing = True,
):
    """
    HANDLE ピン留め＋ProcessStartTime 厳密照合付き CONIN$ 注入。
    戻り値: (ok: bool, reason_code: str, win32_last_error: int)
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

    allowed_set = _normalize_allowed_images(allowed_images)
    if allowed_set is not None and len(allowed_set) == 0:
        return (False, "image_name_mismatch", 0)

    cdef DWORD pid_dw = <DWORD>target_pid
    cdef uint64_t expected_ft = <uint64_t>expected_start_time
    cdef DWORD delay_ms = <DWORD>(submit_delay_sec * 1000.0) if submit_delay_sec > 0 else 0
    cdef Py_ssize_t text_len = 0
    cdef wchar_t* text_w = NULL

    if text:
        text_w = PyUnicode_AsWideCharString(text, &text_len)
        if text_w == NULL:
            raise MemoryError("Failed to allocate wide char string for text")

    cdef HANDLE h_proc = NULL
    cdef HANDLE h_conin = INVALID_HANDLE_VALUE
    cdef FILETIME c_ft, e_ft, k_ft, u_ft
    cdef uint64_t actual_ft = 0
    cdef WCHAR exe_buf[1024]
    cdef DWORD exe_len = 1024
    cdef INPUT_RECORD* records = NULL
    cdef INPUT_RECORD enter_records[2]
    cdef DWORD total_events = 0
    cdef DWORD written = 0
    cdef DWORD err_code = 0
    cdef WORD scan_return = 0
    cdef Py_ssize_t i = 0
    cdef int nogil_status = 0

    try:
        # [Step 1] Pin target PID in Windows kernel by holding h_proc
        h_proc = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, 0, pid_dw)
        if h_proc == NULL or h_proc == INVALID_HANDLE_VALUE:
            return (False, "open_process_failed", <int>GetLastError())

        try:
            # [Step 2] Non-blocking liveness check
            if WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                return (False, "process_already_exited", 0)

            # [Step 3] Exact creation time check
            if not GetProcessTimes(h_proc, &c_ft, &e_ft, &k_ft, &u_ft):
                return (False, "get_process_times_failed", <int>GetLastError())
            actual_ft = _ft_to_u64(c_ft)
            if actual_ft == 0 or actual_ft != expected_ft:
                return (False, "pid_reused_start_time_mismatch", 0)

            # [Step 4] Image basename check (if allowed_images is specified)
            if allowed_set is not None:
                exe_len = 1024
                if not QueryFullProcessImageNameW(h_proc, 0, exe_buf, &exe_len):
                    return (False, "image_query_failed", <int>GetLastError())
                exe_path = PyUnicode_FromWideChar(exe_buf, <Py_ssize_t>exe_len)
                if not _image_matches_allowed(os.path.basename(exe_path), allowed_set):
                    return (False, "image_name_mismatch", 0)

            # [Step 5] Console attach + CONIN$ write under nogil while holding h_proc
            with nogil:
                FreeConsole()
                if not AttachConsole(pid_dw):
                    err_code = GetLastError()
                    AttachConsole(ATTACH_PARENT_PROCESS)
                    nogil_status = 1  # attach_console_failed
                else:
                    h_conin = CreateFileW(
                        CONIN_DEVICE_NAME,
                        GENERIC_READ | GENERIC_WRITE,
                        FILE_SHARE_READ | FILE_SHARE_WRITE,
                        NULL,
                        OPEN_EXISTING,
                        0,
                        NULL,
                    )
                    if h_conin == INVALID_HANDLE_VALUE or h_conin == NULL:
                        err_code = GetLastError()
                        nogil_status = 2  # conin_open_failed
                    elif WaitForSingleObject(h_proc, 0) != WAIT_TIMEOUT:
                        nogil_status = 3  # process_already_exited
                    else:
                        if respect_user_typing:
                            nogil_status = _check_user_typing_with_retries_c(h_proc, h_conin, &err_code)

                        if nogil_status == 0 and text_w != NULL and text_len > 0:
                            total_events = <DWORD>(text_len * 2)
                            records = <INPUT_RECORD*>malloc(sizeof(INPUT_RECORD) * total_events)
                            if records == NULL:
                                err_code = 0
                                nogil_status = 4  # write_console_input_failed
                            else:
                                for i in range(text_len):
                                    _init_key_record(&records[i * 2], text_w[i], 0, 0, 1)
                                    _init_key_record(&records[i * 2 + 1], text_w[i], 0, 0, 0)

                                if not WriteConsoleInputW(h_conin, records, total_events, &written):
                                    err_code = GetLastError()
                                    nogil_status = 4  # write_console_input_failed

                        if nogil_status == 0 and send_enter:
                            if delay_ms > 0:
                                Sleep(delay_ms)

                            scan_return = <WORD>MapVirtualKeyW(VK_RETURN, MAPVK_VK_TO_VSC)
                            _init_key_record(&enter_records[0], WCHAR_CR, VK_RETURN, scan_return, 1)
                            _init_key_record(&enter_records[1], WCHAR_CR, VK_RETURN, scan_return, 0)

                            if not WriteConsoleInputW(h_conin, enter_records, 2, &written):
                                err_code = GetLastError()
                                nogil_status = 4  # write_console_input_failed

                    if records != NULL:
                        free(records)
                        records = NULL
                    if h_conin != INVALID_HANDLE_VALUE and h_conin != NULL:
                        CloseHandle(h_conin)
                        h_conin = INVALID_HANDLE_VALUE
                    FreeConsole()
                    AttachConsole(ATTACH_PARENT_PROCESS)

            if nogil_status == 1:
                return (False, "attach_console_failed", <int>err_code)
            elif nogil_status == 2:
                return (False, "conin_open_failed", <int>err_code)
            elif nogil_status == 3:
                return (False, "process_already_exited", 0)
            elif nogil_status == 4:
                return (False, "write_console_input_failed", <int>err_code)
            elif nogil_status == 5:
                return (False, "deferred_user_typing", 0)
            elif nogil_status == 6:
                return (False, "user_typing_check_failed", <int>err_code)
            return (True, "ok", 0)
        finally:
            CloseHandle(h_proc)
    finally:
        if text_w != NULL:
            PyMem_Free(text_w)


cpdef bint inject_console_input(
    object target_pid,
    str text = "進めて",
    bint send_enter = True,
    double submit_delay_sec = 0.08,
    object expected_start_time = None,
    object allowed_images = None,
    bint respect_user_typing = True,
):
    """
    bool 返却の互換入口。
    Fail-Closed 既定: expected_start_time が None または <= 0 の場合は即 False を返す。
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
    return bool(ok)
