#!/usr/bin/env python3
"""Host tests for the local zclaw control dashboard."""

from __future__ import annotations

import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock


PROJECT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_DIR / "scripts"))

import zclaw_dashboard as dashboard  # noqa: E402


def valid_payload() -> dict[str, str]:
    return {
        "port": "COM6",
        "wifi_ssid": "Test Network",
        "wifi_password": "not-a-real-password",
        "backend": "openai",
        "model": "gpt-5.6-sol",
        "api_key": "sk-test-not-real",
        "api_url": "",
        "telegram_token": "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
        "telegram_chat_ids": "123456789,-100987654321",
    }


class DashboardValidationTests(unittest.TestCase):
    def test_valid_payload_normalizes_chat_ids(self) -> None:
        config = dashboard.validate_provision_payload(valid_payload())
        self.assertEqual(config.backend, "openai")
        self.assertEqual(
            config.telegram_chat_ids, ("123456789", "-100987654321")
        )

    def test_provider_key_is_required(self) -> None:
        payload = valid_payload()
        payload["api_key"] = ""
        with self.assertRaisesRegex(ValueError, "API key is required"):
            dashboard.validate_provision_payload(payload)

    def test_ollama_requires_complete_url_not_key(self) -> None:
        payload = valid_payload()
        payload.update(
            backend="ollama",
            model="qwen3:8b",
            api_key="",
            api_url="http://192.168.1.50:11434",
        )
        config = dashboard.validate_provision_payload(payload)
        self.assertEqual(config.api_url, "http://192.168.1.50:11434")

    def test_gemini_is_supported_with_api_key(self) -> None:
        payload = valid_payload()
        payload.update(
            backend="gemini",
            model="gemini-3.6-flash",
            api_key="google-test-key",
        )
        config = dashboard.validate_provision_payload(payload)
        self.assertEqual(config.backend, "gemini")
        self.assertEqual(config.model, "gemini-3.6-flash")
        self.assertIn("gemini-3.6-flash", dashboard.MODEL_CATALOG["gemini"])

    def test_telegram_token_and_ids_are_paired(self) -> None:
        payload = valid_payload()
        payload["telegram_chat_ids"] = ""
        with self.assertRaisesRegex(ValueError, "provided together"):
            dashboard.validate_provision_payload(payload)

    def test_invalid_chat_id_is_rejected(self) -> None:
        payload = valid_payload()
        payload["telegram_chat_ids"] = "not-a-number"
        with self.assertRaisesRegex(ValueError, "must be integers"):
            dashboard.validate_provision_payload(payload)

    def test_nvs_csv_contains_expected_namespace_and_keys(self) -> None:
        config = dashboard.validate_provision_payload(valid_payload())
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "nvs.csv"
            dashboard._write_nvs_csv(csv_path, config)
            contents = csv_path.read_text(encoding="utf-8")
        self.assertIn("zclaw,namespace", contents)
        self.assertIn("wifi_ssid,data,string,Test Network", contents)
        self.assertIn("llm_backend,data,string,openai", contents)
        self.assertIn("tg_chat_ids,data,string", contents)

    @mock.patch.object(dashboard, "_tool_paths")
    @mock.patch.object(dashboard, "_run_hidden")
    def test_provision_result_never_returns_secrets(
        self, run_hidden: mock.Mock, tool_paths: mock.Mock
    ) -> None:
        tool_paths.return_value = (Path("nvs_partition_gen.py"), Path("esptool.py"))

        def fake_run(command: list[str], *, timeout: float) -> str:
            del timeout
            if "generate" in command:
                Path(command[-2]).write_bytes(b"\0" * 0x4000)
                return "Created NVS binary"
            return "Hash of data verified."

        run_hidden.side_effect = fake_run
        payload = valid_payload()
        config = dashboard.validate_provision_payload(payload)
        result = dashboard.provision_device(config)
        encoded = repr(result)
        self.assertTrue(result["ok"])
        self.assertNotIn(payload["api_key"], encoded)
        self.assertNotIn(payload["telegram_token"], encoded)
        self.assertNotIn(payload["wifi_password"], encoded)


if __name__ == "__main__":
    unittest.main()
