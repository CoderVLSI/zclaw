#!/usr/bin/env python3
"""Run the local PlatformIO build without creating Windows console windows.

The launcher pins this workspace's Python environment instead of using
whichever ``pio.exe`` happens to be first on PATH. Build output and status are
written under .local-build/.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from windows_conpty import run_conpty


PROJECT_DIR = Path(__file__).resolve().parents[1]
WORKSPACE_DIR = PROJECT_DIR.parent
DEFAULT_BUILD_PROJECT_DIR = Path.home() / "zclaw-shell-build"
STATE_DIR = PROJECT_DIR / ".local-build"
STATE_FILE = STATE_DIR / "status.json"
LOG_FILE = STATE_DIR / "build.log"
RUNTIME_PYTHON = WORKSPACE_DIR / ".venv_runtime" / "Scripts" / "python.exe"
RUNTIME_PYTHONW = WORKSPACE_DIR / ".venv_runtime" / "Scripts" / "pythonw.exe"
RUNTIME_SITE_PACKAGES = WORKSPACE_DIR / ".venv_runtime" / "Lib" / "site-packages"
RUNTIME_CONFIG = WORKSPACE_DIR / ".venv_runtime" / "pyvenv.cfg"
DEFAULT_ENVIRONMENT = "esp32-wroom-zclaw-shell"

if os.name == "nt":
    NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
else:
    NO_WINDOW_FLAGS = 0


def _discover_base_python() -> Path:
    try:
        for line in RUNTIME_CONFIG.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip().lower() == "executable":
                return Path(value.strip())
    except OSError:
        pass
    return RUNTIME_PYTHON


BASE_PYTHON = _discover_base_python()


def _ensure_idf_console_pythons(idf_sites: list[Path]) -> list[Path]:
    """Replace broken venv redirectors with the matching console interpreter.

    A venv created from KiCad's Python can incorrectly redirect ``python.exe``
    to ``pythonw.exe``. Native ESP-IDF tools launched below that GUI process
    then ask Windows Terminal for a new console. Keeping the original launcher
    as a backup and installing the real console executable prevents that.
    """
    base_dlls = sorted(BASE_PYTHON.parent.glob("python3*.dll"))
    if not base_dlls:
        raise RuntimeError(f"Python runtime DLL is missing beside {BASE_PYTHON}")
    repaired: list[Path] = []
    for site_packages in idf_sites:
        venv_dir = site_packages.parents[1]
        scripts_dir = venv_dir / "Scripts"
        target = scripts_dir / "python.exe"
        backup = scripts_dir / "python-redirector.exe"
        if not target.exists():
            raise RuntimeError(f"ESP-IDF Python is missing: {target}")
        if target.stat().st_size != BASE_PYTHON.stat().st_size:
            if not backup.exists():
                shutil.copy2(target, backup)
            shutil.copy2(BASE_PYTHON, target)
        for runtime_dll in base_dlls:
            shutil.copy2(runtime_dll, scripts_dir / runtime_dll.name)
        repaired.append(target)
    return repaired


def _ensure_python_path_bridge() -> Path:
    """Let KiCad's Python honor a build-only package path environment variable.

    KiCad's sitecustomize intentionally rebuilds sys.path and therefore removes
    the standard PYTHONPATH entries.  A conditional .pth hook in KiCad's user
    site keeps normal KiCad sessions unchanged while allowing this launcher to
    expose PlatformIO and ESP-IDF packages to console-mode Python children.
    """
    result = subprocess.run(
        [str(BASE_PYTHON), "-c", "import site; print(site.USER_SITE)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=NO_WINDOW_FLAGS,
        startupinfo=_startup_info(),
        check=False,
    )
    user_site = Path(result.stdout.strip())
    if result.returncode != 0 or not user_site.is_absolute():
        raise RuntimeError(
            "Unable to locate the base Python user site: " + result.stdout.strip()
        )
    user_site.mkdir(parents=True, exist_ok=True)
    bridge = user_site / "zclaw_platformio.pth"
    contents = (
        "import os,sys; sys.path[0:0] = filter(None, "
        "os.environ.get('ZCLAW_BUILD_PYTHONPATH', '').split(os.pathsep))\n"
    )
    if not bridge.exists() or bridge.read_text(encoding="utf-8") != contents:
        bridge.write_text(contents, encoding="utf-8")
    return bridge


def _write_headless_project_config(build_project_dir: Path) -> Path:
    """Create an ignored PlatformIO config that bypasses venv redirector EXEs."""
    source = build_project_dir / "platformio.ini"
    destination = build_project_dir / ".pio" / "headless-platformio.ini"
    destination.parent.mkdir(parents=True, exist_ok=True)
    python_override = f'-DPYTHON="{BASE_PYTHON.as_posix()}"'
    lines: list[str] = []
    replaced = False
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("board_build.cmake_extra_args"):
            key, separator, value = line.partition("=")
            if separator:
                line = f"{key}= {value.strip()} {python_override}"
                replaced = True
        lines.append(line)
    if not replaced:
        raise RuntimeError("platformio.ini is missing board_build.cmake_extra_args")
    destination.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return destination


def _write_state(*, reset: bool = False, **values: object) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    if STATE_FILE.exists() and not reset:
        try:
            current = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            current = {}
    current.update(values)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def _read_state() -> dict[str, object]:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"status": "not-started"}


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    ctypes.windll.kernel32.CloseHandle(process)
    return True


def _startup_info() -> subprocess.STARTUPINFO | None:
    if os.name != "nt":
        return None
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return startup


def _git_head(project_dir: Path) -> str:
    result = subprocess.run(
        ["git.exe", "-C", str(project_dir), "rev-parse", "HEAD"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=NO_WINDOW_FLAGS,
        startupinfo=_startup_info(),
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _worker(environment: str, build_project_dir: Path) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["VIRTUAL_ENV"] = str(WORKSPACE_DIR / ".venv_runtime")
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    idf_sites = sorted(
        (Path.home() / ".platformio" / "penv").glob(".espidf-*/Lib/site-packages")
    )
    repaired_idf_pythons = _ensure_idf_console_pythons(idf_sites)
    os.environ["PATH"] = os.pathsep.join(
        [str(BASE_PYTHON.parent), os.environ.get("PATH", "")]
    )
    python_paths = [str(RUNTIME_SITE_PACKAGES), *(str(path) for path in idf_sites)]
    if existing_pythonpath:
        python_paths.append(existing_pythonpath)
    os.environ["PYTHONPATH"] = os.pathsep.join(python_paths)
    os.environ["ZCLAW_BUILD_PYTHONPATH"] = os.pathsep.join(python_paths)
    python_path_bridge = _ensure_python_path_bridge()
    project_config = _write_headless_project_config(build_project_dir)
    command = [
        str(BASE_PYTHON),
        "-m",
        "platformio",
        "run",
        "--project-dir",
        str(build_project_dir),
        "--project-conf",
        str(project_config),
        "--environment",
        environment,
        "--jobs",
        "2",
    ]
    _write_state(
        reset=True,
        status="starting",
        worker_pid=os.getpid(),
        environment=environment,
        build_project_dir=str(build_project_dir),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        command=command,
        log=str(LOG_FILE),
        python_path_bridge=str(python_path_bridge),
        repaired_idf_pythons=[str(path) for path in repaired_idf_pythons],
    )
    try:
        with LOG_FILE.open("wb") as log:
            log.write(b"Quiet local build: Windows ConPTY headless session.\n")
            log.write(("Command: " + subprocess.list2cmdline(command) + "\n\n").encode())
            log.flush()
            exit_code = run_conpty(
                command,
                build_project_dir,
                log,
                on_started=lambda pid: _write_state(status="running", child_pid=pid),
            )
    except BaseException as error:
        with LOG_FILE.open("ab") as log:
            log.write(f"\nLauncher failure: {type(error).__name__}: {error}\n".encode())
        _write_state(
            status="launcher-failed",
            exit_code=None,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            error=f"{type(error).__name__}: {error}",
        )
        return 1

    _write_state(
        status="succeeded" if exit_code == 0 else "failed",
        exit_code=exit_code,
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    return exit_code


def _start(environment: str, build_project_dir: Path) -> int:
    if not RUNTIME_PYTHON.exists() or not RUNTIME_PYTHONW.exists() or not BASE_PYTHON.exists():
        print(f"Workspace runtime is missing: {RUNTIME_PYTHON.parent}", file=sys.stderr)
        return 2
    if " " in str(build_project_dir):
        print(f"Build path must not contain spaces: {build_project_dir}", file=sys.stderr)
        return 2
    if not (build_project_dir / "platformio.ini").exists():
        print(f"Build project is missing platformio.ini: {build_project_dir}", file=sys.stderr)
        return 2
    source_head = _git_head(PROJECT_DIR)
    build_head = _git_head(build_project_dir)
    if source_head and build_head and source_head != build_head:
        print(
            "Short-path build clone is out of date: "
            f"source={source_head[:12]} build={build_head[:12]}",
            file=sys.stderr,
        )
        return 2

    previous = _read_state()
    previous_pid = int(previous.get("worker_pid") or 0)
    if previous.get("status") in {"starting", "running"} and _process_exists(previous_pid):
        print(f"A quiet build is already running (PID {previous_pid}).")
        return 3

    _write_state(
        reset=True,
        status="launching",
        environment=environment,
        build_project_dir=str(build_project_dir),
    )

    command = [
        str(RUNTIME_PYTHONW),
        str(Path(__file__).resolve()),
        "--worker",
        "--environment",
        environment,
        "--project-dir",
        str(build_project_dir),
    ]
    process = subprocess.Popen(
        command,
        cwd=PROJECT_DIR,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW_FLAGS,
        startupinfo=_startup_info(),
        close_fds=True,
    )
    _write_state(
        status="starting",
        worker_pid=process.pid,
        environment=environment,
        build_project_dir=str(build_project_dir),
    )
    print(f"Quiet build started as PID {process.pid}. Log: {LOG_FILE}")
    return 0


def _stop() -> int:
    state = _read_state()
    pid = int(state.get("worker_pid") or 0)
    if not _process_exists(pid):
        _write_state(status="stopped", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        print("No quiet build is running.")
        return 0
    result = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=NO_WINDOW_FLAGS,
        startupinfo=_startup_info(),
        check=False,
    )
    _write_state(status="stopped", finished_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    print(result.stdout.strip() or "Quiet build stopped.")
    return 0 if result.returncode == 0 else result.returncode


def _status() -> int:
    state = _read_state()
    pid = int(state.get("worker_pid") or 0)
    if state.get("status") in {"starting", "running"} and not _process_exists(pid):
        state["status"] = "stale"
    print(json.dumps(state, indent=2))
    if LOG_FILE.exists():
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        if lines:
            print("\nLast build log lines:")
            print("\n".join(lines[-20:]))
    return 0


def _self_test() -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    test_log = STATE_DIR / "self-test.log"
    nested = (
        "import subprocess,sys,time; "
        "r=subprocess.run([sys.executable,'-c','print(\"nested child ok\")'],"
        "capture_output=True,text=True,check=True); print(r.stdout,end=''); time.sleep(3)"
    )
    existing_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = str(RUNTIME_SITE_PACKAGES) + (
        os.pathsep + existing_pythonpath if existing_pythonpath else ""
    )
    with test_log.open("wb") as log:
        exit_code = run_conpty([str(BASE_PYTHON), "-c", nested], PROJECT_DIR, log)
    output = test_log.read_text(encoding="utf-8", errors="replace").strip()
    print(f"self-test exit={exit_code}; output={output!r}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--start", action="store_true", help="start a hidden build")
    action.add_argument("--status", action="store_true", help="show build state and log tail")
    action.add_argument("--stop", action="store_true", help="stop the hidden build process tree")
    action.add_argument("--self-test", action="store_true", help="verify hidden nested child execution")
    action.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--environment", default=DEFAULT_ENVIRONMENT)
    parser.add_argument(
        "--project-dir",
        type=Path,
        default=DEFAULT_BUILD_PROJECT_DIR,
        help="no-space project clone used for PlatformIO compilation",
    )
    args = parser.parse_args()
    if args.worker:
        return _worker(args.environment, args.project_dir.resolve())
    if args.start:
        return _start(args.environment, args.project_dir.resolve())
    if args.status:
        return _status()
    if args.stop:
        return _stop()
    return _self_test()


if __name__ == "__main__":
    raise SystemExit(main())
