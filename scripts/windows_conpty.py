"""Minimal headless Windows ConPTY process runner.

This follows Microsoft's documented CreatePseudoConsole flow.  Attaching the
build to a pseudoconsole prevents Windows Terminal from being selected as a
visible default console for native compiler descendants.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import subprocess
import threading
from typing import BinaryIO, Callable, Sequence


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [
        ("StartupInfo", STARTUPINFOW),
        ("lpAttributeList", ctypes.c_void_p),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
INFINITE = 0xFFFFFFFF


def _configure_kernel32() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.CreatePseudoConsole.argtypes = [
        COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    kernel32.CreatePseudoConsole.restype = ctypes.c_long
    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ClosePseudoConsole.argtypes = [ctypes.c_void_p]
    kernel32.ClosePseudoConsole.restype = None
    return kernel32


def _raise_last_error(operation: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation} failed: {ctypes.FormatError(error)}")


def run_conpty(
    command: Sequence[str],
    cwd: os.PathLike[str] | str,
    output: BinaryIO,
    on_started: Callable[[int], None] | None = None,
) -> int:
    """Run *command* inside a windowless pseudoconsole and return its exit code."""
    if os.name != "nt":
        raise RuntimeError("ConPTY is only available on Windows")

    import msvcrt

    kernel32 = _configure_kernel32()
    input_read = wintypes.HANDLE()
    input_write = wintypes.HANDLE()
    output_read = wintypes.HANDLE()
    output_write = wintypes.HANDLE()
    pseudo_console = ctypes.c_void_p()
    attribute_buffer: ctypes.Array[ctypes.c_char] | None = None
    process_info = PROCESS_INFORMATION()
    reader_thread: threading.Thread | None = None
    output_fd: int | None = None

    def close_handle(handle: wintypes.HANDLE | int | None) -> None:
        value = handle.value if hasattr(handle, "value") else handle
        if value:
            kernel32.CloseHandle(wintypes.HANDLE(value))
            if hasattr(handle, "value"):
                handle.value = None

    try:
        if not kernel32.CreatePipe(
            ctypes.byref(input_read), ctypes.byref(input_write), None, 0
        ):
            _raise_last_error("CreatePipe(input)")
        if not kernel32.CreatePipe(
            ctypes.byref(output_read), ctypes.byref(output_write), None, 0
        ):
            _raise_last_error("CreatePipe(output)")

        result = kernel32.CreatePseudoConsole(
            COORD(160, 50), input_read, output_write, 0, ctypes.byref(pseudo_console)
        )
        if result != 0:
            raise OSError(result, f"CreatePseudoConsole failed with HRESULT 0x{result & 0xFFFFFFFF:08x}")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(startup)
        attribute_size = ctypes.c_size_t()
        kernel32.InitializeProcThreadAttributeList(
            None, 1, 0, ctypes.byref(attribute_size)
        )
        attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
        startup.lpAttributeList = ctypes.cast(attribute_buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            startup.lpAttributeList, 1, 0, ctypes.byref(attribute_size)
        ):
            _raise_last_error("InitializeProcThreadAttributeList")
        if not kernel32.UpdateProcThreadAttribute(
            startup.lpAttributeList,
            0,
            PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
            pseudo_console,
            ctypes.sizeof(pseudo_console),
            None,
            None,
        ):
            _raise_last_error("UpdateProcThreadAttribute")

        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        if not kernel32.CreateProcessW(
            None,
            command_line,
            None,
            None,
            False,
            EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT,
            None,
            str(cwd),
            ctypes.byref(startup.StartupInfo),
            ctypes.byref(process_info),
        ):
            _raise_last_error("CreateProcessW")

        close_handle(process_info.hThread)
        process_info.hThread = None
        close_handle(input_read)
        close_handle(output_write)

        output_fd = msvcrt.open_osfhandle(output_read.value, os.O_RDONLY)
        output_read.value = None

        def drain_output() -> None:
            assert output_fd is not None
            try:
                with os.fdopen(output_fd, "rb", buffering=0) as stream:
                    while True:
                        data = stream.read(65536)
                        if not data:
                            break
                        output.write(data)
                        output.flush()
            except OSError as error:
                output.write(f"\n[ConPTY output closed: {error}]\n".encode())
                output.flush()

        reader_thread = threading.Thread(target=drain_output, name="conpty-log", daemon=True)
        reader_thread.start()
        if on_started:
            on_started(int(process_info.dwProcessId))

        kernel32.WaitForSingleObject(process_info.hProcess, INFINITE)
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process_info.hProcess, ctypes.byref(exit_code)):
            _raise_last_error("GetExitCodeProcess")

        kernel32.ClosePseudoConsole(pseudo_console)
        pseudo_console.value = None
        reader_thread.join(timeout=15)
        return int(exit_code.value)
    finally:
        close_handle(process_info.hThread)
        process_info.hThread = None
        close_handle(process_info.hProcess)
        process_info.hProcess = None
        if pseudo_console.value:
            kernel32.ClosePseudoConsole(pseudo_console)
        if attribute_buffer is not None:
            startup_list = locals().get("startup")
            if startup_list and startup_list.lpAttributeList:
                kernel32.DeleteProcThreadAttributeList(startup_list.lpAttributeList)
        close_handle(input_read)
        close_handle(input_write)
        close_handle(output_read)
        close_handle(output_write)
