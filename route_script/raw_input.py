'通过 Windows Raw Input 读取真实相对鼠标移动。'
from __future__ import annotations

import ctypes
import threading
from ctypes import wintypes
from typing import Callable


RawMoveCallback = Callable[[int, int], None]
LogCallback = Callable[[str, BaseException], None]

WM_INPUT = 0x00FF
WM_QUIT = 0x0012
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIDEV_REMOVE = 0x00000001
RIDEV_INPUTSINK = 0x00000100
MOUSE_MOVE_ABSOLUTE = 0x0001
UINT_ERROR = 0xFFFFFFFF
HWND_MESSAGE = -3


class _RawInputDevice(ctypes.Structure):
    _fields_ = (
        ("usage_page", wintypes.USHORT),
        ("usage", wintypes.USHORT),
        ("flags", wintypes.DWORD),
        ("target", wintypes.HWND),
    )


class _RawInputHeader(ctypes.Structure):
    _fields_ = (
        ("type", wintypes.DWORD),
        ("size", wintypes.DWORD),
        ("device", wintypes.HANDLE),
        ("wparam", wintypes.WPARAM),
    )


class _RawMouseData(ctypes.Structure):
    _fields_ = (
        ("flags", wintypes.USHORT),
        ("reserved", wintypes.USHORT),
        ("button_flags", wintypes.USHORT),
        ("button_data", ctypes.c_short),
        ("raw_buttons", wintypes.ULONG),
        ("last_x", wintypes.LONG),
        ("last_y", wintypes.LONG),
        ("extra_information", wintypes.ULONG),
    )


_LRESULT = ctypes.c_ssize_t
_WNDPROC = ctypes.WINFUNCTYPE(
    _LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class _WindowClass(ctypes.Structure):
    _fields_ = (
        ("style", wintypes.UINT),
        ("window_proc", _WNDPROC),
        ("class_extra", ctypes.c_int),
        ("window_extra", ctypes.c_int),
        ("instance", wintypes.HINSTANCE),
        ("icon", wintypes.HICON),
        ("cursor", wintypes.HCURSOR),
        ("background", wintypes.HBRUSH),
        ("menu_name", wintypes.LPCWSTR),
        ("class_name", wintypes.LPCWSTR),
    )


def decode_raw_mouse(data: bytes) -> tuple[int, int] | None:
    '从 Raw Input 数据块提取相对移动，绝对坐标包返回空。'
    header_size = ctypes.sizeof(_RawInputHeader)
    mouse_size = ctypes.sizeof(_RawMouseData)
    if len(data) < header_size + mouse_size:
        return None
    header = _RawInputHeader.from_buffer_copy(data[:header_size])
    if int(header.type) != RIM_TYPEMOUSE:
        return None
    mouse = _RawMouseData.from_buffer_copy(
        data[header_size:header_size + mouse_size])
    if int(mouse.flags) & MOUSE_MOVE_ABSOLUTE:
        return None
    dx, dy = int(mouse.last_x), int(mouse.last_y)
    return (dx, dy) if dx or dy else None


class RawMouseMonitor:
    '在独立消息线程中接收相对鼠标数据。'

    INITIALIZE_TIMEOUT = 2.0
    STOP_TIMEOUT = 2.0
    MAX_PACKET_SIZE = 4096

    def __init__(
            self, callback: RawMoveCallback,
            *, error_log: LogCallback | None = None) -> None:
        self._callback = callback
        self._error_log = error_log or (lambda _message, _exc: None)
        self._lock = threading.RLock()
        self._initialized = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._initialization_error: BaseException | None = None
        self._window = 0
        self._class_name = ""
        self._window_proc: _WNDPROC | None = None

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive() and self._window)

    def start(self) -> None:
        '启动监听；初始化失败时抛错，由调用方选择回退。'
        with self._lock:
            if self._thread is not None:
                if self._thread.is_alive() and self._window:
                    return
                raise RuntimeError("Raw Input 监听器状态无效")
            self._initialized.clear()
            self._initialization_error = None
            self._thread = threading.Thread(
                target=self._run,
                name="HOKWorld Raw Input",
                daemon=True,
            )
            thread = self._thread
        thread.start()
        if not self._initialized.wait(self.INITIALIZE_TIMEOUT):
            self.stop()
            raise TimeoutError("Raw Input 初始化超时")
        error = self._initialization_error
        if error is not None:
            self.stop()
            raise RuntimeError("Raw Input 初始化失败") from error

    def stop(self) -> None:
        '请求消息线程退出并等待资源释放。'
        with self._lock:
            thread = self._thread
            thread_id = self._thread_id
        if thread is None:
            return
        if thread.ident == threading.get_ident():
            ctypes.windll.user32.PostQuitMessage(0)
            return
        if thread_id:
            _user32().PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        thread.join(timeout=self.STOP_TIMEOUT)
        if thread.is_alive():
            self._error_log(
                "Raw Input 消息线程未在限定时间内退出",
                TimeoutError("Raw Input 停止超时"),
            )

    def _run(self) -> None:
        user32 = _user32()
        kernel32 = _kernel32()
        instance = kernel32.GetModuleHandleW(None)
        class_name = f"HOKWorldRawMouse_{id(self):x}"
        registered_class = False
        registered_device = False
        window = 0

        @_WNDPROC
        def window_proc(hwnd, message, wparam, lparam):
            if message == WM_INPUT:
                try:
                    self._process_packet(lparam)
                except Exception as exc:
                    self._error_log("读取 Raw Input 鼠标数据失败", exc)
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)

        try:
            with self._lock:
                self._thread_id = int(kernel32.GetCurrentThreadId())
                self._class_name = class_name
                self._window_proc = window_proc
            window_class = _WindowClass(
                0, window_proc, 0, 0, instance,
                None, None, None, None, class_name,
            )
            if not user32.RegisterClassW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered_class = True
            window = user32.CreateWindowExW(
                0, class_name, class_name, 0,
                0, 0, 0, 0, ctypes.c_void_p(HWND_MESSAGE),
                None, instance, None,
            )
            if not window:
                raise ctypes.WinError(ctypes.get_last_error())
            device = _RawInputDevice(
                0x01, 0x02, RIDEV_INPUTSINK, wintypes.HWND(window))
            if not user32.RegisterRawInputDevices(
                    ctypes.byref(device), 1, ctypes.sizeof(_RawInputDevice)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered_device = True
            with self._lock:
                self._window = int(window)
            self._initialized.set()

            message = wintypes.MSG()
            while True:
                result = int(user32.GetMessageW(
                    ctypes.byref(message), None, 0, 0))
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
        except BaseException as exc:
            if not self._initialized.is_set():
                self._initialization_error = exc
                self._initialized.set()
            else:
                self._error_log("Raw Input 消息线程异常", exc)
        finally:
            if registered_device:
                device = _RawInputDevice(0x01, 0x02, RIDEV_REMOVE, None)
                if not user32.RegisterRawInputDevices(
                        ctypes.byref(device), 1,
                        ctypes.sizeof(_RawInputDevice)):
                    self._error_log(
                        "注销 Raw Input 鼠标设备失败",
                        ctypes.WinError(ctypes.get_last_error()),
                    )
            if window:
                user32.DestroyWindow(window)
            if registered_class:
                user32.UnregisterClassW(class_name, instance)
            with self._lock:
                self._window = 0
                self._thread_id = 0
                self._thread = None
                self._window_proc = None
            self._initialized.set()

    def _process_packet(self, raw_handle: int) -> None:
        user32 = _user32()
        size = wintypes.UINT(0)
        header_size = ctypes.sizeof(_RawInputHeader)
        result = int(user32.GetRawInputData(
            ctypes.c_void_p(raw_handle), RID_INPUT, None,
            ctypes.byref(size), header_size,
        ))
        if result == UINT_ERROR:
            raise ctypes.WinError(ctypes.get_last_error())
        if size.value == 0 or size.value > self.MAX_PACKET_SIZE:
            return
        buffer = ctypes.create_string_buffer(size.value)
        read_size = wintypes.UINT(size.value)
        result = int(user32.GetRawInputData(
            ctypes.c_void_p(raw_handle), RID_INPUT, buffer,
            ctypes.byref(read_size), header_size,
        ))
        if result == UINT_ERROR:
            raise ctypes.WinError(ctypes.get_last_error())
        movement = decode_raw_mouse(buffer.raw[:read_size.value])
        if movement is not None:
            self._callback(*movement)


_USER32 = None
_KERNEL32 = None


def _user32():
    global _USER32
    if _USER32 is None:
        library = ctypes.WinDLL("user32", use_last_error=True)
        library.RegisterClassW.argtypes = (ctypes.POINTER(_WindowClass),)
        library.RegisterClassW.restype = wintypes.ATOM
        library.CreateWindowExW.argtypes = (
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, wintypes.HMENU,
            wintypes.HINSTANCE, wintypes.LPVOID,
        )
        library.CreateWindowExW.restype = wintypes.HWND
        library.DefWindowProcW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        library.DefWindowProcW.restype = _LRESULT
        library.RegisterRawInputDevices.argtypes = (
            ctypes.POINTER(_RawInputDevice), wintypes.UINT, wintypes.UINT)
        library.RegisterRawInputDevices.restype = wintypes.BOOL
        library.GetRawInputData.argtypes = (
            ctypes.c_void_p, wintypes.UINT, wintypes.LPVOID,
            ctypes.POINTER(wintypes.UINT), wintypes.UINT)
        library.GetRawInputData.restype = wintypes.UINT
        library.GetMessageW.argtypes = (
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT)
        library.GetMessageW.restype = wintypes.BOOL
        library.TranslateMessage.argtypes = (ctypes.POINTER(wintypes.MSG),)
        library.TranslateMessage.restype = wintypes.BOOL
        library.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
        library.DispatchMessageW.restype = _LRESULT
        library.PostThreadMessageW.argtypes = (
            wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        library.PostThreadMessageW.restype = wintypes.BOOL
        library.DestroyWindow.argtypes = (wintypes.HWND,)
        library.DestroyWindow.restype = wintypes.BOOL
        library.UnregisterClassW.argtypes = (
            wintypes.LPCWSTR, wintypes.HINSTANCE)
        library.UnregisterClassW.restype = wintypes.BOOL
        _USER32 = library
    return _USER32


def _kernel32():
    global _KERNEL32
    if _KERNEL32 is None:
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        library.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
        library.GetModuleHandleW.restype = wintypes.HMODULE
        library.GetCurrentThreadId.argtypes = ()
        library.GetCurrentThreadId.restype = wintypes.DWORD
        _KERNEL32 = library
    return _KERNEL32
