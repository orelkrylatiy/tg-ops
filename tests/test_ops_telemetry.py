import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import admin
import ops_telemetry


class TelemetryTests(unittest.TestCase):
    def test_record_event_omits_message_content(self):
        with tempfile.TemporaryDirectory() as directory:
            original = ops_telemetry.EVENTS_PATH
            ops_telemetry.EVENTS_PATH = Path(directory) / "events.jsonl"
            try:
                self.assertTrue(
                    ops_telemetry.record_event(
                        "message_sent",
                        category="manual",
                        chat_id=123,
                        text="private message",
                        caption="private caption",
                    )
                )
                events = ops_telemetry.read_events()
            finally:
                ops_telemetry.EVENTS_PATH = original

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "message_sent")
        self.assertNotIn("text", events[0])
        self.assertNotIn("caption", events[0])

    def test_session_id_uses_configured_registry(self):
        with tempfile.TemporaryDirectory() as directory:
            original_here = ops_telemetry.HERE
            ops_telemetry.HERE = Path(directory)
            (Path(directory) / "sessions.json").write_text(
                '{"sessions":[{"id":"primary","session":"userbot"}]}',
                encoding="utf-8",
            )
            try:
                with patch.dict(os.environ, {"TG_OPS_SESSION": "userbot"}, clear=True):
                    self.assertEqual(ops_telemetry.current_session_id(), "primary")
            finally:
                ops_telemetry.HERE = original_here


    def test_summary_deduplicates_duplicate_account_sessions(self):
        sessions = [
            {
                "id": "primary",
                "user_id": 42,
                "status": "online",
                "total_chats": 20,
                "active_chats_30d": 8,
                "unread_chats": 3,
            },
            {
                "id": "backup-session",
                "user_id": 42,
                "status": "online",
                "total_chats": 20,
                "active_chats_30d": 8,
                "unread_chats": 3,
            },
            {
                "id": "secondary",
                "user_id": 99,
                "status": "online",
                "total_chats": 10,
                "active_chats_30d": 4,
                "unread_chats": 1,
            },
        ]
        with patch("admin.read_events", return_value=[]), patch(
            "admin._pipeline_snapshot", return_value={}
        ):
            summary = admin.build_summary("all", "all", {"sessions": sessions})

        self.assertEqual(summary["live"]["configured_sessions"], 3)
        self.assertEqual(summary["live"]["active_chats_30d"], 12)
        self.assertEqual(summary["live"]["total_chats"], 30)
        self.assertEqual(summary["live"]["unread_chats"], 4)

    def test_resume_classifier(self):
        self.assertTrue(ops_telemetry.is_resume_file("resume/resume-react.pdf"))
        self.assertTrue(ops_telemetry.is_resume_file("CV-Maxim.pdf"))
        self.assertFalse(ops_telemetry.is_resume_file("vacancy-brief.pdf"))


if __name__ == "__main__":
    unittest.main()
