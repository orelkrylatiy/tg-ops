import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from admin_agent import (
    _manual_folder_peer_ids,
    explicit_send_intent,
    explicit_start_intent,
    explicit_stop_intent,
    parse_plan,
)
from admin_runtime import ProcessManager, _profiles, build_command


class AgentIntentTests(unittest.TestCase):
    def test_send_requires_positive_explicit_language(self):
        self.assertTrue(explicit_send_intent("Напиши @frontend_hr"))
        self.assertTrue(explicit_send_intent("Отправь ему ответ"))
        self.assertFalse(explicit_send_intent("Подготовь ответ, но не отправляй"))
        self.assertFalse(explicit_send_intent("Не пиши ему"))
        self.assertFalse(explicit_send_intent("Не отправь ему"))

    def test_start_and_stop_respect_negation(self):
        self.assertTrue(explicit_start_intent("Запусти автоответчик"))
        self.assertFalse(explicit_start_intent("Не запускай автоответчик"))
        self.assertTrue(explicit_stop_intent("Останови процесс"))
        self.assertFalse(explicit_stop_intent("Не останавливай процесс"))

    def test_parse_plan_accepts_json_and_fenced_json(self):
        expected = {
            "tool": "search_contacts",
            "args": {"query": "frontend"},
            "reply": "",
        }
        self.assertEqual(
            parse_plan('{"tool":"search_contacts","args":{"query":"frontend"}}'),
            expected,
        )
        fenced = (
            chr(96) * 3
            + 'json\n{"tool":"search_contacts","args":{"query":"frontend"}}\n'
            + chr(96) * 3
        )
        self.assertEqual(parse_plan(fenced), expected)

    def test_people_and_custom_folders_are_manual_but_status_folders_are_not(self):
        filters = [
            ("Люди", object(), {10}),
            ("HR", object(), {11}),
            ("Менторы ML", object(), {12}),
            ("Холодки", object(), {13}),
            ("Диалог", object(), {14}),
        ]
        self.assertEqual(
            _manual_folder_peer_ids(filters, {"Холодки", "Диалог", "Тёплые", "Отказы"}),
            {10, 11, 12},
        )

    def test_non_json_response_is_plain_reply(self):
        self.assertEqual(
            parse_plan("В базе пока нет подходящих контактов."),
            {"tool": None, "reply": "В базе пока нет подходящих контактов."},
        )


class ProcessAllowlistTests(unittest.TestCase):
    def test_known_task_builds_fixed_argv(self):
        profile = _profiles()[0]
        command = build_command("lead_scan", {"profile": profile, "limit": 80})
        self.assertEqual(command[1:4], ["leads.py", "scan", "--profile"])
        self.assertEqual(command[-2:], ["--limit", "80"])

    def test_unknown_task_is_rejected(self):
        with self.assertRaises(ValueError):
            build_command("run_shell", {"command": "rm -rf /"})

    def test_limits_are_validated(self):
        profile = _profiles()[0]
        with self.assertRaises(ValueError):
            build_command("lead_scan", {"profile": profile, "limit": 9000})

    def test_stopping_managed_cron_clears_only_its_own_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / ".cron.lock"
            lock_path.write_text("12345", encoding="utf-8")
            with patch("admin_runtime.CRON_LOCK", lock_path):
                ProcessManager._clear_cron_lock_for_pid(99999)
                self.assertTrue(lock_path.exists())
                ProcessManager._clear_cron_lock_for_pid(12345)
                self.assertFalse(lock_path.exists())

    def test_catalog_marks_sending_operations(self):
        tasks = {item["id"]: item for item in ProcessManager().task_catalog()}
        self.assertTrue(tasks["cron_run"]["sends"])
        self.assertTrue(tasks["responder"]["sends"])
        self.assertFalse(tasks["lead_scan"]["sends"])


if __name__ == "__main__":
    unittest.main()
