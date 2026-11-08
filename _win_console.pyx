# cython: language_level=3
# cython: boundscheck=False
# cython: wraparound=False
# distutils: libraries = kernel32 user32 rstrtmgr

"""
Antigravity 別窓エージェント バックグラウンド入力注入 Cython ネイティブ拡張 (_win_console.pyx)

特徴:
1. Windows SDK の `<windows.h>` および `<restartmanager.h>` を直接インクルードし、
   手動の16進数バイナリ定数や ctypes 構造体定義（パディング調整等）を完全に排除。
2. `find_pid_by_lock_path`: Restart Manager API を C ネイティブ (`nogil`) で呼び出し、
   presence/<会話ID>.lock を保持している agy.exe の PID を高速に逆引き。
3. `inject_console_input`: C ヒープ/スタック上の `INPUT_RECORD` 配列へ直接キーイベントを構築し、
   GIL を完全解放 (`with nogil:`) した状態で `AttachConsole` + `WriteConsoleInputW` + `Sleep` を実行。
   Python オブジェクト生成コストゼロ・ホットリロード監視スレッド (orochi) のブロッキングゼロを実現。
"""

from libc.stdlib cimport malloc, free
from libc.string cimport memset
from libc.stddef cimport wchar_t
from cpython.mem cimport PyMem_Free

cdef extern from "Python.h":
    wchar_t* PyUnicode_AsWideCharString(object unicode, Py_ssize_t *size)

cdef extern from *:
    """
    #define CONIN_DEVICE_NAME L"CONIN$"
    #define WCHAR_CR L'\\r'
    """
    cdef const wchar_t* CONIN_DEVICE_NAME
    cdef wchar_t WCHAR_CR


# ============================================================================
# 1. Windows SDK ヘッダの直接インクルード (<windows.h> / <restartmanager.h>)
# ============================================================================

cdef extern from "<windows.h>" nogil:
    ctypedef unsigned long DWORD
    ctypedef unsigned short WORD
    ctypedef unsigned int UINT
    ctypedef unsigned long ULONG
    ctypedef int BOOL
    ctypedef void* HANDLE
    ctypedef const wchar_t* LPCWSTR
    ctypedef wchar_t WCHAR
    ctypedef char CHAR

    # Windows 標準マクロ・定数 (ヘッダから直接解決されるためマジックナンバー不要)
    cdef DWORD ATTACH_PARENT_PROCESS
    cdef DWORD GENERIC_READ
    cdef DWORD GENERIC_WRITE
    cdef DWORD FILE_SHARE_READ
    cdef DWORD FILE_SHARE_WRITE
    cdef DWORD OPEN_EXISTING
    cdef HANDLE INVALID_HANDLE_VALUE

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


# ============================================================================
# 2. C ネイティブ (nogil) による Restart Manager PID 逆引き実装
# ============================================================================

cdef DWORD _find_pid_by_lock_path_nogil(LPCWSTR lock_path_w) noexcept nogil:
    """
    GIL を解放した状態で Restart Manager API を呼び出し、
    指定されたロックファイルを保持しているプロセスの PID を取得する。
    見つからない場合は 0 を返す。
    """
    cdef DWORD session = 0
    cdef WCHAR session_key[CCH_RM_SESSION_KEY + 1]
    memset(session_key, 0, sizeof(session_key))

    cdef DWORD res = RmStartSession(&session, 0, session_key)
    if res != 0:
        return 0

    cdef DWORD found_pid = 0
    cdef LPCWSTR resources[1]
    resources[0] = lock_path_w

    cdef UINT needed = 0
    cdef UINT count = 0
    cdef DWORD reason = 0
    cdef UINT i = 0
    cdef RM_PROCESS_INFO* process_infos = NULL

    try:
        if RmRegisterResources(session, 1, resources, 0, NULL, 0, NULL) != 0:
            return 0

        # 1回目: 必要な RM_PROCESS_INFO 配列要素数を取得
        RmGetList(session, &needed, &count, NULL, &reason)
        if needed == 0:
            return 0

        process_infos = <RM_PROCESS_INFO*>malloc(sizeof(RM_PROCESS_INFO) * needed)
        if process_infos == NULL:
            return 0

        count = needed
        res = RmGetList(session, &needed, &count, process_infos, &reason)
        if res == 0:
            for i in range(count):
                if process_infos[i].Process.dwProcessId > 0:
                    found_pid = process_infos[i].Process.dwProcessId
                    break
        return found_pid
    finally:
        if process_infos != NULL:
            free(process_infos)
        RmEndSession(session)


cpdef object find_pid_by_lock_path(str lock_path):
    """
    Python からロックファイルパスを受け取り、保持しているプロセスの PID (int) を返す。
    該当プロセスがない場合は None を返す。
    """
    cdef Py_ssize_t size = 0
    cdef wchar_t* lock_path_w = PyUnicode_AsWideCharString(lock_path, &size)
    if lock_path_w == NULL:
        raise MemoryError("Failed to allocate wide char string for lock_path")

    cdef DWORD pid = 0
    try:
        with nogil:
            pid = _find_pid_by_lock_path_nogil(lock_path_w)
    finally:
        PyMem_Free(lock_path_w)

    if pid > 0:
        return <int>pid
    return None


# ============================================================================
# 3. C ネイティブ (nogil) によるバックグラウンド CONIN$ 入力注入実装
# ============================================================================

cdef inline void _init_key_record(
    INPUT_RECORD* rec,
    WCHAR ch,
    WORD vk_code,
    WORD scan_code,
    BOOL key_down,
) noexcept nogil:
    """C 構造体ポインタへ直接 KEY_EVENT_RECORD を書き込むインライン関数"""
    memset(rec, 0, sizeof(INPUT_RECORD))
    rec.EventType = KEY_EVENT
    rec.Event.KeyEvent.bKeyDown = key_down
    rec.Event.KeyEvent.wRepeatCount = 1
    rec.Event.KeyEvent.wVirtualKeyCode = vk_code
    rec.Event.KeyEvent.wVirtualScanCode = scan_code
    rec.Event.KeyEvent.uChar.UnicodeChar = ch
    rec.Event.KeyEvent.dwControlKeyState = 0


cdef BOOL _inject_console_input_nogil(
    DWORD target_pid,
    const wchar_t* text_w,
    Py_ssize_t text_len,
    BOOL send_enter,
    DWORD delay_ms,
) noexcept nogil:
    """
    GIL 完全解放下で対象 PID のコンソールへ裏でアタッチし、
    文字列イベント配列と Enter キーイベントを CONIN$ へ直接書き込む。
    """
    FreeConsole()
    if not AttachConsole(target_pid):
        AttachConsole(ATTACH_PARENT_PROCESS)
        return 0

    cdef HANDLE h_conin = INVALID_HANDLE_VALUE
    cdef INPUT_RECORD* records = NULL
    cdef DWORD total_events = 0
    cdef DWORD written = 0
    cdef Py_ssize_t i = 0
    cdef INPUT_RECORD enter_records[2]
    cdef WORD scan_return = 0

    try:
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
            return 0

        # 1. テキスト文字列のキー押下・離上イベントを C ヒープ配列へ一括構築
        if text_w != NULL and text_len > 0:
            total_events = <DWORD>(text_len * 2)
            records = <INPUT_RECORD*>malloc(sizeof(INPUT_RECORD) * total_events)
            if records == NULL:
                return 0

            for i in range(text_len):
                _init_key_record(&records[i * 2], text_w[i], 0, 0, 1)
                _init_key_record(&records[i * 2 + 1], text_w[i], 0, 0, 0)

            if not WriteConsoleInputW(h_conin, records, total_events, &written):
                return 0

        # 2. TUI フレームワークの入力取り込みウェイト (nogil のため Python スレッドを一切止めない)
        if send_enter:
            if delay_ms > 0:
                Sleep(delay_ms)

            scan_return = <WORD>MapVirtualKeyW(VK_RETURN, MAPVK_VK_TO_VSC)
            _init_key_record(&enter_records[0], WCHAR_CR, VK_RETURN, scan_return, 1)
            _init_key_record(&enter_records[1], WCHAR_CR, VK_RETURN, scan_return, 0)

            if not WriteConsoleInputW(h_conin, enter_records, 2, &written):
                return 0

        return 1
    finally:
        if records != NULL:
            free(records)
        if h_conin != INVALID_HANDLE_VALUE and h_conin != NULL:
            CloseHandle(h_conin)
        FreeConsole()
        AttachConsole(ATTACH_PARENT_PROCESS)


cpdef bint inject_console_input(
    unsigned long target_pid,
    str text = "進めて",
    bint send_enter = True,
    double submit_delay_sec = 0.08,
):
    """
    指定した PID (agy.exe) のコンソール入力バッファ (CONIN$) に
    バックグラウンドで文字列と Enter キーを直接書き込む（フォーカス移動なし・GIL解放）。
    """
    cdef Py_ssize_t text_len = 0
    cdef wchar_t* text_w = NULL
    cdef DWORD delay_ms = <DWORD>(submit_delay_sec * 1000.0) if submit_delay_sec > 0 else 0
    cdef BOOL ok = 0

    if text:
        text_w = PyUnicode_AsWideCharString(text, &text_len)
        if text_w == NULL:
            raise MemoryError("Failed to allocate wide char string for text")

    try:
        with nogil:
            ok = _inject_console_input_nogil(
                <DWORD>target_pid,
                text_w,
                text_len,
                1 if send_enter else 0,
                delay_ms,
            )
    finally:
        if text_w != NULL:
            PyMem_Free(text_w)

    return bool(ok)
