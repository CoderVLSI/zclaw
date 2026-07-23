#!/usr/bin/env python3
"""Local zclaw setup, status, and chat dashboard.

The server binds to loopback by default. Provisioning values are validated in
memory, encoded into an ESP-IDF NVS image, written to the device, and discarded.
Secrets are never returned by the API or written to the repository.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from web_relay import (
    SerialAgentBridge,
    is_json_content_type,
    is_post_origin_allowed,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
MAX_BODY_BYTES = 64 * 1024
MAX_SERIAL_MESSAGE = 4096
DEFAULT_MODELS = {
    "openai": "gpt-5.6-sol",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-3.6-flash",
    "openrouter": "openrouter/auto",
    "ollama": "qwen3:8b",
}
MODEL_CATALOG = {
    "openai": [
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.6",
        "gpt-5.5",
        "gpt-5.4",
        "gpt-5.4-mini",
    ],
    "anthropic": [
        "claude-fable-5",
        "claude-opus-4-8",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
    ],
    "gemini": [
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.1-pro-preview",
        "gemini-3.1-flash-lite",
        "gemini-3-flash-preview",
        "gemini-flash-latest",
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
    "openrouter": [
        "openrouter/auto",
        "~openai/gpt-latest",
        "openai/gpt-5.6-sol",
        "google/gemini-3.6-flash",
        "anthropic/claude-sonnet-5",
    ],
    "ollama": [
        "qwen3:8b",
        "qwen3:4b",
        "llama3.2:3b",
        "gemma3:4b",
    ],
}
TOKEN_RE = re.compile(r"^\d{6,20}:[A-Za-z0-9_-]{20,}$")
CHAT_ID_RE = re.compile(r"^-?\d{1,20}$")

if os.name == "nt":
    NO_WINDOW_FLAGS = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
else:
    NO_WINDOW_FLAGS = 0


@dataclass(frozen=True)
class ProvisionConfig:
    port: str
    wifi_ssid: str
    wifi_password: str
    backend: str
    model: str
    api_key: str
    api_url: str
    telegram_token: str
    telegram_chat_ids: tuple[str, ...]


@dataclass
class DashboardState:
    operation_lock: threading.Lock


def _string(payload: dict, key: str, *, required: bool = False) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value


def validate_provision_payload(payload: dict) -> ProvisionConfig:
    port = _string(payload, "port", required=True)
    wifi_ssid = _string(payload, "wifi_ssid", required=True)
    wifi_password = _string(payload, "wifi_password")
    backend = _string(payload, "backend", required=True).lower()
    model = _string(payload, "model", required=True)
    api_key = _string(payload, "api_key")
    api_url = _string(payload, "api_url")
    telegram_token = _string(payload, "telegram_token")
    raw_chat_ids = _string(payload, "telegram_chat_ids")

    if len(port) > 128:
        raise ValueError("Serial port is too long")
    if len(wifi_ssid.encode("utf-8")) > 32:
        raise ValueError("Wi-Fi SSID must be at most 32 UTF-8 bytes")
    if len(wifi_password.encode("utf-8")) > 64:
        raise ValueError("Wi-Fi password must be at most 64 UTF-8 bytes")
    if backend not in DEFAULT_MODELS:
        raise ValueError("Unsupported LLM provider")
    if len(model) > 128:
        raise ValueError("Model name must be at most 128 characters")
    if backend == "ollama":
        if not api_url:
            raise ValueError("Ollama URL is required")
        parsed_url = urlparse(api_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Ollama URL must be a complete http:// or https:// URL")
    elif not api_key:
        raise ValueError(f"{backend} API key is required")
    if len(api_key) > 511:
        raise ValueError("LLM API key must be at most 511 characters")
    if len(api_url) > 255:
        raise ValueError("LLM API URL must be at most 255 characters")

    if telegram_token and not TOKEN_RE.fullmatch(telegram_token):
        raise ValueError("Telegram token format is invalid")
    chat_ids = tuple(
        part.strip() for part in raw_chat_ids.split(",") if part.strip()
    )
    if len(chat_ids) > 4:
        raise ValueError("Telegram allows at most four authorized chat IDs")
    if any(not CHAT_ID_RE.fullmatch(chat_id) for chat_id in chat_ids):
        raise ValueError("Telegram chat IDs must be integers separated by commas")
    if bool(telegram_token) != bool(chat_ids):
        raise ValueError("Telegram token and at least one chat ID must be provided together")

    return ProvisionConfig(
        port=port,
        wifi_ssid=wifi_ssid,
        wifi_password=wifi_password,
        backend=backend,
        model=model,
        api_key=api_key,
        api_url=api_url,
        telegram_token=telegram_token,
        telegram_chat_ids=chat_ids,
    )


def list_serial_ports() -> list[dict[str, str]]:
    try:
        from serial.tools import list_ports  # type: ignore

        ports = [
            {
                "device": item.device,
                "description": item.description or "Serial device",
                "hwid": item.hwid or "",
            }
            for item in list_ports.comports()
        ]
        return sorted(ports, key=lambda item: item["device"])
    except (ImportError, OSError):
        return []


def _tool_paths() -> tuple[Path, Path]:
    pio_packages = Path.home() / ".platformio" / "packages"
    nvs_generator = (
        pio_packages
        / "framework-espidf"
        / "components"
        / "nvs_flash"
        / "nvs_partition_generator"
        / "nvs_partition_gen.py"
    )
    esptool_script = pio_packages / "tool-esptoolpy" / "esptool.py"
    if not nvs_generator.exists():
        raise RuntimeError(f"ESP-IDF NVS generator is missing: {nvs_generator}")
    if not esptool_script.exists():
        raise RuntimeError(f"esptool is missing: {esptool_script}")
    return nvs_generator, esptool_script


def _tool_environment() -> dict[str, str]:
    env = os.environ.copy()
    tool_root = Path.home() / ".platformio" / "packages" / "tool-esptoolpy"
    idf_sites = sorted(
        (Path.home() / ".platformio" / "penv").glob(".espidf-*/Lib/site-packages")
    )
    extra_paths = [
        str(path)
        for path in (
            *idf_sites,
            tool_root,
            tool_root / "_contrib",
        )
        if path.exists()
    ]
    existing = env.get("PYTHONPATH", "")
    if existing:
        extra_paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(extra_paths)
    return env


def _run_hidden(command: list[str], *, timeout: float) -> str:
    startup = None
    if os.name == "nt":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
    result = subprocess.run(
        command,
        cwd=PROJECT_DIR,
        env=_tool_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        creationflags=NO_WINDOW_FLAGS,
        startupinfo=startup,
        check=False,
    )
    output = result.stdout.strip()
    if result.returncode != 0:
        tail = "\n".join(output.splitlines()[-12:])
        raise RuntimeError(tail or f"Command failed with exit code {result.returncode}")
    return output


def _write_nvs_csv(path: Path, config: ProvisionConfig) -> None:
    rows = [
        ("key", "type", "encoding", "value"),
        ("zclaw", "namespace", "", ""),
        ("wifi_ssid", "data", "string", config.wifi_ssid),
        ("wifi_pass", "data", "string", config.wifi_password),
        ("llm_backend", "data", "string", config.backend),
        ("api_key", "data", "string", config.api_key),
        ("llm_model", "data", "string", config.model),
    ]
    if config.api_url:
        rows.append(("llm_api_url", "data", "string", config.api_url))
    if config.telegram_token:
        rows.extend(
            [
                ("tg_token", "data", "string", config.telegram_token),
                ("tg_chat_id", "data", "string", config.telegram_chat_ids[0]),
                (
                    "tg_chat_ids",
                    "data",
                    "string",
                    ",".join(config.telegram_chat_ids),
                ),
            ]
        )
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)


def provision_device(config: ProvisionConfig) -> dict:
    nvs_generator, esptool_script = _tool_paths()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="zclaw-dashboard-") as temp_dir:
        temporary = Path(temp_dir)
        csv_path = temporary / "nvs.csv"
        binary_path = temporary / "nvs.bin"
        _write_nvs_csv(csv_path, config)
        _run_hidden(
            [
                sys.executable,
                str(nvs_generator),
                "generate",
                str(csv_path),
                str(binary_path),
                "0x4000",
            ],
            timeout=45,
        )
        if not binary_path.exists() or binary_path.stat().st_size != 0x4000:
            raise RuntimeError("NVS generator did not produce a 16 KiB image")
        flash_output = _run_hidden(
            [
                sys.executable,
                str(esptool_script),
                "--chip",
                "esp32",
                "--port",
                config.port,
                "--baud",
                "460800",
                "--before",
                "default_reset",
                "--after",
                "hard_reset",
                "write_flash",
                "0x9000",
                str(binary_path),
            ],
            timeout=120,
        )

    verified = "Hash of data verified" in flash_output
    if not verified:
        raise RuntimeError("esptool completed without confirming the NVS write hash")
    return {
        "ok": True,
        "port": config.port,
        "backend": config.backend,
        "model": config.model,
        "telegram_configured": bool(config.telegram_token),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "message": "Credentials written and verified. The ESP32 is rebooting.",
    }


def ask_device(port: str, message: str) -> str:
    bridge = SerialAgentBridge(
        port=port,
        baudrate=115200,
        serial_timeout_s=0.1,
        response_timeout_s=75,
        idle_timeout_s=0.8,
        log_serial=False,
    )
    bridge.open()
    try:
        return bridge.ask(message)
    finally:
        bridge.close()


def make_handler(state: DashboardState):
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "zclaw-dashboard/1.0"

        def log_message(self, fmt: str, *args) -> None:  # pragma: no cover
            logging.info("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._send_html(DASHBOARD_HTML)
                return
            if parsed.path == "/api/status":
                ports = list_serial_ports()
                default_port = next(
                    (item["device"] for item in ports if item["device"].upper() == "COM6"),
                    ports[0]["device"] if ports else "",
                )
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "ports": ports,
                        "default_port": default_port,
                        "providers": DEFAULT_MODELS,
                        "model_catalog": MODEL_CATALOG,
                    },
                )
                return
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path not in {"/api/provision", "/api/device"}:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                return
            if not is_post_origin_allowed(
                self.headers.get("Origin"), self.headers.get("Host"), None
            ):
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Origin not allowed"})
                return
            if not is_json_content_type(self.headers.get("Content-Type")):
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "Content-Type must be application/json"},
                )
                return
            payload = self._read_json()
            if payload is None:
                return

            if not state.operation_lock.acquire(blocking=False):
                self._send_json(
                    HTTPStatus.CONFLICT,
                    {"error": "Another device operation is still running"},
                )
                return
            try:
                if parsed.path == "/api/provision":
                    try:
                        config = validate_provision_payload(payload)
                        result = provision_device(config)
                    except ValueError as exc:
                        self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                        return
                    except subprocess.TimeoutExpired:
                        self._send_json(
                            HTTPStatus.GATEWAY_TIMEOUT,
                            {"error": "Provisioning timed out while waiting for the ESP32"},
                        )
                        return
                    except RuntimeError as exc:
                        logging.warning("provisioning failed: %s", exc)
                        self._send_json(
                            HTTPStatus.BAD_GATEWAY,
                            {"error": f"Provisioning failed: {exc}"},
                        )
                        return
                    self._send_json(HTTPStatus.OK, result)
                    return

                try:
                    port = _string(payload, "port", required=True)
                    message = _string(payload, "message", required=True)
                    if len(message) > MAX_SERIAL_MESSAGE:
                        raise ValueError("Message is too long")
                    started = time.monotonic()
                    reply = ask_device(port, message)
                except ValueError as exc:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                    return
                except TimeoutError as exc:
                    self._send_json(HTTPStatus.GATEWAY_TIMEOUT, {"error": str(exc)})
                    return
                except RuntimeError as exc:
                    self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                    return
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "reply": reply,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                    },
                )
            finally:
                state.operation_lock.release()

        def _read_json(self) -> dict | None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_BYTES:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid body size"})
                return None
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON body"})
                return None
            if not isinstance(payload, dict):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "JSON body must be an object"})
                return None
            return payload

        def _send_json(self, status: HTTPStatus, payload: dict) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status.value)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            try:
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                logging.info("client disconnected before the response completed")

        def _send_html(self, html: str) -> None:
            encoded = html.encode("utf-8")
            self.send_response(HTTPStatus.OK.value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'",
            )
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return DashboardHandler


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>zclaw Control Deck</title>
  <style>
    :root {
      color-scheme: dark;
      --ink: #e8f0f5;
      --muted: #8ca0ac;
      --line: #243641;
      --panel: #101a20;
      --panel-hi: #14232b;
      --bg: #091014;
      --green: #61e7aa;
      --cyan: #64d7ed;
      --amber: #ffc66d;
      --red: #ff7b7b;
      --radius: 18px;
      font-family: Inter, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(900px 500px at 8% -12%, rgba(100,215,237,.12), transparent 60%),
        radial-gradient(750px 500px at 100% 0, rgba(97,231,170,.08), transparent 55%),
        var(--bg);
    }
    button, input, select, textarea { font: inherit; }
    button { cursor: pointer; }
    .app { max-width: 1180px; margin: 0 auto; padding: 24px 18px 48px; }
    header {
      display: flex; align-items: center; justify-content: space-between;
      gap: 18px; margin-bottom: 20px;
    }
    .brand { display: flex; align-items: center; gap: 14px; }
    .mark {
      width: 48px; height: 48px; display: grid; place-items: center;
      border: 1px solid rgba(97,231,170,.55); border-radius: 14px;
      color: var(--green); font: 800 18px ui-monospace, monospace;
      background: linear-gradient(145deg, rgba(97,231,170,.15), rgba(100,215,237,.06));
      box-shadow: 0 0 32px rgba(97,231,170,.1);
    }
    h1 { margin: 0; font-size: clamp(1.25rem, 3vw, 1.8rem); letter-spacing: -.02em; }
    .subtitle { color: var(--muted); margin-top: 4px; font-size: .9rem; }
    .badge {
      display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px;
      border: 1px solid var(--line); border-radius: 999px; color: var(--muted);
      background: rgba(16,26,32,.8); font-size: .82rem;
    }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--amber); }
    .badge.ok .dot { background: var(--green); box-shadow: 0 0 12px rgba(97,231,170,.65); }
    .badge.bad .dot { background: var(--red); }
    .tabs { display: flex; gap: 8px; margin-bottom: 16px; }
    .tab {
      border: 1px solid var(--line); border-radius: 12px; padding: 9px 14px;
      color: var(--muted); background: rgba(16,26,32,.74);
    }
    .tab.active { color: var(--ink); border-color: rgba(100,215,237,.55); background: var(--panel-hi); }
    .view { display: none; }
    .view.active { display: block; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.45fr) minmax(290px, .75fr); gap: 16px; }
    .stack { display: grid; gap: 16px; }
    .card {
      border: 1px solid var(--line); border-radius: var(--radius); padding: 18px;
      background: linear-gradient(180deg, rgba(20,35,43,.92), rgba(16,26,32,.96));
      box-shadow: 0 18px 60px rgba(0,0,0,.22);
    }
    .card h2 { font-size: 1rem; margin: 0 0 4px; }
    .card .desc { color: var(--muted); font-size: .84rem; margin: 0 0 16px; line-height: 1.5; }
    .fields { display: grid; grid-template-columns: repeat(2, minmax(0,1fr)); gap: 13px; }
    label { display: grid; gap: 7px; color: #b9c8d0; font-size: .78rem; letter-spacing: .02em; }
    label.full { grid-column: 1 / -1; }
    input, select, textarea {
      width: 100%; min-height: 42px; color: var(--ink); background: #0b1419;
      border: 1px solid #2a3e49; border-radius: 11px; padding: 10px 12px; outline: none;
    }
    textarea { min-height: 118px; resize: vertical; }
    input:focus, select:focus, textarea:focus { border-color: var(--cyan); box-shadow: 0 0 0 3px rgba(100,215,237,.1); }
    .secret { position: relative; }
    .secret input { padding-right: 64px; }
    .reveal {
      position: absolute; right: 6px; top: 6px; min-height: 30px; padding: 4px 8px;
      border: 0; border-radius: 8px; color: var(--cyan); background: transparent; font-size: .75rem;
    }
    .actions { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 16px; }
    .primary, .secondary, .quick {
      border: 1px solid transparent; border-radius: 11px; min-height: 42px; padding: 9px 14px;
      font-weight: 700;
    }
    .primary { color: #062016; background: var(--green); }
    .primary:hover { filter: brightness(1.06); }
    .secondary, .quick { color: var(--ink); background: #17262e; border-color: #2b414c; }
    button:disabled { cursor: wait; opacity: .55; }
    .hint { color: var(--muted); font-size: .75rem; line-height: 1.45; }
    .hint a { color: var(--cyan); }
    .steps { display: grid; gap: 13px; }
    .step { display: grid; grid-template-columns: 26px 1fr; gap: 10px; align-items: start; }
    .step-num {
      width: 26px; height: 26px; display: grid; place-items: center; border-radius: 8px;
      color: var(--cyan); background: rgba(100,215,237,.1); border: 1px solid rgba(100,215,237,.25);
      font: 700 .75rem ui-monospace, monospace;
    }
    .step strong { display: block; font-size: .85rem; margin-bottom: 3px; }
    .step span { color: var(--muted); font-size: .78rem; line-height: 1.45; }
    .log {
      min-height: 146px; max-height: 310px; overflow: auto; white-space: pre-wrap;
      border: 1px solid #20333e; border-radius: 12px; padding: 12px; color: #bed0d8;
      background: #071015; font: .78rem/1.55 ui-monospace, "Cascadia Mono", monospace;
    }
    .log .ok { color: var(--green); }
    .quick-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
    .quick { min-height: 34px; padding: 6px 10px; font-size: .76rem; }
    .chat-grid { display: grid; grid-template-columns: 1fr 320px; gap: 16px; }
    .send-row { display: grid; grid-template-columns: 1fr auto; gap: 10px; margin-top: 10px; }
    .send-row textarea { min-height: 86px; }
    .console { min-height: 380px; }
    .warning { color: var(--amber); }
    .hidden { display: none !important; }
    @media (max-width: 820px) {
      .grid, .chat-grid { grid-template-columns: 1fr; }
      header { align-items: flex-start; }
    }
    @media (max-width: 580px) {
      .app { padding: 16px 10px 32px; }
      .fields { grid-template-columns: 1fr; }
      label.full { grid-column: auto; }
      header { display: grid; }
      .primary, .secondary { width: 100%; }
      .send-row { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="app">
    <header>
      <div class="brand">
        <div class="mark">ZC</div>
        <div><h1>zclaw Control Deck</h1><div class="subtitle">Private USB setup, device status, and agent console</div></div>
      </div>
      <div id="connectionBadge" class="badge"><span class="dot"></span><span id="connectionText">Checking USB</span></div>
    </header>

    <nav class="tabs" aria-label="Dashboard sections">
      <button class="tab active" data-view="setup">Setup</button>
      <button class="tab" data-view="console">Console</button>
    </nav>

    <section id="setupView" class="view active">
      <div class="grid">
        <form id="provisionForm" class="stack" autocomplete="off">
          <section class="card">
            <h2>1 · Device and Wi‑Fi</h2>
            <p class="desc">The dashboard writes credentials directly to the ESP32 over USB. Nothing is uploaded.</p>
            <div class="fields">
              <label>USB serial port<select id="port" name="port" required></select></label>
              <label>Wi‑Fi network<input name="wifi_ssid" maxlength="32" required placeholder="Home Wi‑Fi"></label>
              <label class="full">Wi‑Fi password
                <span class="secret"><input name="wifi_password" type="password" maxlength="64" placeholder="Network password"><button class="reveal" type="button">Show</button></span>
              </label>
            </div>
          </section>

          <section class="card">
            <h2>2 · Language model</h2>
            <p class="desc">Use OpenAI, Anthropic, Google Gemini, OpenRouter, or a local Ollama server reachable from the ESP32 network.</p>
            <div class="fields">
              <label>Provider<select id="backend" name="backend">
                <option value="openai">OpenAI</option>
                <option value="anthropic">Anthropic</option>
                <option value="gemini">Google Gemini</option>
                <option value="openrouter">OpenRouter</option>
                <option value="ollama">Ollama (local)</option>
              </select></label>
              <label>Model<input id="model" name="model" value="gpt-5.6-sol" list="model-options" required><datalist id="model-options"></datalist></label>
              <label id="apiKeyLabel" class="full">API key
                <span class="secret"><input name="api_key" type="password" placeholder="Entered locally; never saved by dashboard"><button class="reveal" type="button">Show</button></span>
              </label>
              <label id="apiUrlLabel" class="full hidden">Ollama URL<input name="api_url" placeholder="http://192.168.1.50:11434"></label>
            </div>
            <p class="hint">Current chat and agent model suggestions verified through 23 July 2026. You can still type a custom model ID.</p>
          </section>

          <section class="card">
            <h2>3 · Telegram</h2>
            <p class="desc">Optional. Messages are accepted only from the chat IDs you authorize.</p>
            <div class="fields">
              <label class="full">Bot token
                <span class="secret"><input name="telegram_token" type="password" placeholder="123456789:AA..."><button class="reveal" type="button">Show</button></span>
              </label>
              <label class="full">Authorized chat IDs<input name="telegram_chat_ids" placeholder="123456789 or comma-separated IDs"></label>
            </div>
            <p class="hint">Create a bot with <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>. Find your chat ID with <a href="https://t.me/userinfobot" target="_blank" rel="noopener">@userinfobot</a>.</p>
            <div class="actions">
              <button id="provisionButton" class="primary" type="submit">Provision ESP32</button>
              <button id="refreshButton" class="secondary" type="button">Refresh USB ports</button>
              <span class="hint">The board reboots automatically after a verified write.</span>
            </div>
          </section>
        </form>

        <aside class="stack">
          <section class="card">
            <h2>Setup path</h2><p class="desc">One pass connects every layer.</p>
            <div class="steps">
              <div class="step"><span class="step-num">01</span><div><strong>USB provisioning</strong><span>Credentials are packed into the ESP32 NVS partition.</span></div></div>
              <div class="step"><span class="step-num">02</span><div><strong>Wi‑Fi and provider</strong><span>zclaw connects and initializes the selected model backend.</span></div></div>
              <div class="step"><span class="step-num">03</span><div><strong>Telegram polling</strong><span>The bot accepts only the configured chat allowlist.</span></div></div>
              <div class="step"><span class="step-num">04</span><div><strong>Test in Console</strong><span>Run /settings, then send a normal message through the ESP32 agent.</span></div></div>
            </div>
            <p class="hint warning">Values persist in ESP32 NVS. This development flow does not enable flash encryption, so protect physical access to the board.</p>
          </section>
          <section class="card">
            <h2>Activity</h2><p class="desc">Secrets are deliberately excluded from this log.</p>
            <div id="setupLog" class="log" role="status">Ready. Connect the ESP32 and refresh USB ports.</div>
          </section>
        </aside>
      </div>
    </section>

    <section id="consoleView" class="view">
      <div class="chat-grid">
        <section class="card">
          <h2>ESP32 console</h2><p class="desc">Normal text goes to the LLM agent. Slash commands inspect the device. Exclamation commands use the local ESP shell.</p>
          <div class="quick-row">
            <button class="quick" data-command="/settings">Settings</button>
            <button class="quick" data-command="/wifi status">Wi‑Fi</button>
            <button class="quick" data-command="/diag">Diagnostics</button>
            <button class="quick" data-command="!df">Filesystem</button>
          </div>
          <div id="consoleLog" class="log console">No commands sent yet.</div>
          <div class="send-row">
            <textarea id="message" maxlength="4096" placeholder="Ask zclaw something, or type /settings"></textarea>
            <button id="sendButton" class="primary" type="button">Send</button>
          </div>
        </section>
        <aside class="card">
          <h2>Connection checklist</h2>
          <div class="steps">
            <div class="step"><span class="step-num">✓</span><div><strong>Firmware</strong><span>zclaw v2.13.0 is flashed.</span></div></div>
            <div class="step"><span class="step-num">1</span><div><strong>Provision</strong><span>Complete the Setup tab once.</span></div></div>
            <div class="step"><span class="step-num">2</span><div><strong>Wait for Wi‑Fi</strong><span>Give the ESP32 about 10–30 seconds after reboot.</span></div></div>
            <div class="step"><span class="step-num">3</span><div><strong>Test</strong><span>Use /settings and then send “hello”.</span></div></div>
          </div>
          <p class="hint warning">Keep other serial monitors closed while this dashboard is using the board.</p>
        </aside>
      </div>
    </section>
  </div>

  <script>
    const defaults = {openai:"gpt-5.6-sol", anthropic:"claude-sonnet-5", gemini:"gemini-3.6-flash", openrouter:"openrouter/auto", ollama:"qwen3:8b"};
    const modelCatalog = {
      openai:["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-5.6","gpt-5.5","gpt-5.4","gpt-5.4-mini"],
      anthropic:["claude-fable-5","claude-opus-4-8","claude-sonnet-5","claude-haiku-4-5","claude-sonnet-4-6"],
      gemini:["gemini-3.6-flash","gemini-3.5-flash","gemini-3.5-flash-lite","gemini-3.1-pro-preview","gemini-3.1-flash-lite","gemini-3-flash-preview","gemini-flash-latest","gemini-2.5-pro","gemini-2.5-flash","gemini-2.5-flash-lite"],
      openrouter:["openrouter/auto","~openai/gpt-latest","openai/gpt-5.6-sol","google/gemini-3.6-flash","anthropic/claude-sonnet-5"],
      ollama:["qwen3:8b","qwen3:4b","llama3.2:3b","gemma3:4b"]
    };
    const $ = (selector) => document.querySelector(selector);
    const form = $("#provisionForm");
    const port = $("#port");
    const backend = $("#backend");
    const model = $("#model");
    const modelOptions = $("#model-options");
    const setupLog = $("#setupLog");
    const consoleLog = $("#consoleLog");
    const badge = $("#connectionBadge");
    const connectionText = $("#connectionText");

    function logSetup(text, ok = null) {
      setupLog.textContent = text;
      setupLog.style.color = ok === true ? "var(--green)" : ok === false ? "var(--red)" : "";
    }
    function setConnection(ok, text) {
      badge.classList.toggle("ok", ok);
      badge.classList.toggle("bad", !ok);
      connectionText.textContent = text;
    }
    function refreshModelOptions() {
      modelOptions.replaceChildren();
      for (const id of modelCatalog[backend.value] || []) {
        const option = document.createElement("option");
        option.value = id;
        modelOptions.appendChild(option);
      }
    }
    async function refreshPorts() {
      setConnection(false, "Checking USB");
      try {
        const res = await fetch("/api/status", {cache:"no-store"});
        const data = await res.json();
        Object.assign(defaults, data.providers || {});
        Object.assign(modelCatalog, data.model_catalog || {});
        refreshModelOptions();
        port.replaceChildren();
        for (const item of data.ports) {
          const option = document.createElement("option");
          option.value = item.device;
          option.textContent = `${item.device} · ${item.description}`;
          if (item.device === data.default_port) option.selected = true;
          port.appendChild(option);
        }
        if (!data.ports.length) {
          const option = document.createElement("option");
          option.value = "COM6"; option.textContent = "COM6 · expected ESP32 port";
          port.appendChild(option);
          setConnection(false, "ESP32 not detected");
          logSetup("No USB serial device detected. Reconnect the board, then refresh.", false);
        } else {
          setConnection(true, `${data.default_port || data.ports[0].device} detected`);
          logSetup("USB device detected. Enter Wi‑Fi and service credentials.");
        }
      } catch (error) {
        setConnection(false, "Dashboard error");
        logSetup(`Could not query the local dashboard: ${error.message}`, false);
      }
    }
    backend.addEventListener("change", () => {
      model.value = defaults[backend.value];
      refreshModelOptions();
      const ollama = backend.value === "ollama";
      $("#apiKeyLabel").classList.toggle("hidden", ollama);
      $("#apiUrlLabel").classList.toggle("hidden", !ollama);
      if (ollama) form.elements.api_key.value = "";
      else form.elements.api_url.value = "";
    });
    refreshModelOptions();
    document.querySelectorAll(".reveal").forEach((button) => {
      button.addEventListener("click", () => {
        const input = button.parentElement.querySelector("input");
        input.type = input.type === "password" ? "text" : "password";
        button.textContent = input.type === "password" ? "Show" : "Hide";
      });
    });
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => {
        document.querySelectorAll(".tab").forEach((node) => node.classList.toggle("active", node === tab));
        $("#setupView").classList.toggle("active", tab.dataset.view === "setup");
        $("#consoleView").classList.toggle("active", tab.dataset.view === "console");
      });
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("#provisionButton");
      const payload = Object.fromEntries(new FormData(form).entries());
      button.disabled = true;
      logSetup("Preparing local USB credential write… Keep the board connected.");
      try {
        const res = await fetch("/api/provision", {
          method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        logSetup(`✓ ${data.message}\nProvider: ${data.backend} · Model: ${data.model}\nTelegram: ${data.telegram_configured ? "configured" : "not configured"}\nCompleted in ${(data.elapsed_ms/1000).toFixed(1)} s.`, true);
        ["wifi_password", "api_key", "telegram_token"].forEach((name) => {
          form.elements[name].value = "";
        });
        setTimeout(() => document.querySelector('[data-view="console"]').click(), 4500);
      } catch (error) {
        logSetup(`Provisioning failed:\n${error.message}`, false);
      } finally { button.disabled = false; }
    });
    async function sendDevice(message) {
      const selectedPort = port.value || "COM6";
      const button = $("#sendButton");
      button.disabled = true;
      consoleLog.textContent = `> ${message}\n\nWaiting for ${selectedPort}…`;
      try {
        const res = await fetch("/api/device", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body:JSON.stringify({port:selectedPort, message})
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
        consoleLog.textContent = `> ${message}\n\n${data.reply}\n\n[${data.elapsed_ms} ms]`;
      } catch (error) {
        consoleLog.textContent = `> ${message}\n\nError: ${error.message}`;
      } finally { button.disabled = false; }
    }
    $("#sendButton").addEventListener("click", () => {
      const message = $("#message").value.trim();
      if (message) { $("#message").value = ""; sendDevice(message); }
    });
    $("#message").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault(); $("#sendButton").click();
      }
    });
    document.querySelectorAll("[data-command]").forEach((button) => {
      button.addEventListener("click", () => sendDevice(button.dataset.command));
    });
    $("#refreshButton").addEventListener("click", refreshPorts);
    refreshPorts();
  </script>
</body>
</html>
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("For safety, the setup dashboard only binds to loopback.")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    state = DashboardState(operation_lock=threading.Lock())
    server = ThreadingHTTPServer((args.host, args.port), make_handler(state))
    logging.info("zclaw dashboard: http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
