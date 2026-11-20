import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from foreman.transcripts import (
    build_run_evidence,
    condense_claude_jsonl,
    condense_codex_jsonl,
    condense_grok_jsonl,
    condense_transcript,
    locate_session_transcript,
)


class TranscriptsTest(unittest.TestCase):
    def test_condense_all_three_dialects_and_redact_secrets_and_pii(self):
        claude_jsonl = "\n".join([
            json.dumps({
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking config for owner@example.invalid token=ghp_0123456789abcdefghij0123456789"},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "src/app.py"}},
                        {"type": "tool_result", "is_error": True, "content": "permission denied (555) 234-5678"},
                    ],
                },
            })
        ])
        out_claude = condense_claude_jsonl(claude_jsonl)
        self.assertIn("[tool:Read] src/app.py", out_claude)
        self.assertIn("[tool_error]", out_claude)
        self.assertNotIn("owner@example.invalid", out_claude)
        self.assertNotIn("ghp_0123456789abcdefghij0123456789", out_claude)
        self.assertNotIn("(555) 234-5678", out_claude)

        codex_jsonl = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "pytest", "exit_code": 0}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Patched xai-0123456789abcdefghij safely."}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}),
        ])
        out_codex = condense_codex_jsonl(codex_jsonl)
        self.assertIn("[cmd exit=0] pytest", out_codex)
        self.assertNotIn("xai-0123456789abcdefghij", out_codex)
        self.assertIn("[usage]", out_codex)

        grok_jsonl = "\n".join([
            json.dumps({
                "role": "assistant",
                "content": "Calling search for user@example.invalid",
                "tool_calls": [{"function": {"name": "grep", "arguments": "pattern"}}],
            })
        ])
        out_grok = condense_grok_jsonl(grok_jsonl)
        self.assertIn("[tool:grep] pattern", out_grok)
        self.assertNotIn("user@example.invalid", out_grok)

    def test_real_codex_rollout_and_system_reminder_filtering(self):
        real_codex_rollout = "\n".join([
            json.dumps({"type": "session_meta", "payload": {"id": "cx-1"}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>noise</environment_context>"}]}}),
            json.dumps({"type": "response_item", "payload": {"type": "function_call", "name": "exec", "arguments": "pytest tests"}}),
            json.dumps({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Implemented fix for secret=xai-0123456789abcdefghij"}]}}),
            json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100, "output_tokens": 25}}}}),
        ])
        condensed_cx = condense_codex_jsonl(real_codex_rollout)
        self.assertIn("[tool:exec] pytest tests", condensed_cx)
        self.assertIn("[assistant] Implemented fix", condensed_cx)
        self.assertIn("[usage]", condensed_cx)
        self.assertNotIn("<environment_context>", condensed_cx)
        self.assertNotIn("xai-0123456789abcdefghij", condensed_cx)

        claude_with_reminder = "\n".join([
            json.dumps({"type": "system", "message": {"role": "system", "content": "hidden"}}),
            json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "<system-reminder>huge prompt</system-reminder>"}]}}),
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Checked src/a.py"}]}}),
        ])
        condensed_cl = condense_claude_jsonl(claude_with_reminder)
        self.assertEqual(condensed_cl, "[assistant] Checked src/a.py")

    def test_locate_transcript_blocks_traversal_and_build_evidence(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            claude_dir = root / "claude"
            codex_dir = root / "codex"
            grok_dir = root / "grok"
            claude_dir.mkdir()
            codex_dir.mkdir()
            (grok_dir / "grok-sess-7").mkdir(parents=True)

            session_file = codex_dir / "rollout-codex-99.jsonl"
            session_file.write_text(
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Built feature"}}) + "\n",
                encoding="utf-8",
            )
            grok_file = grok_dir / "grok-sess-7" / "chat_history.jsonl"
            grok_file.write_text(json.dumps({"role": "assistant", "content": "Reviewed cleanly"}) + "\n", encoding="utf-8")

            roots = {"claude": claude_dir, "codex": codex_dir, "grok": grok_dir}
            self.assertIsNone(locate_session_transcript("../etc/passwd", search_roots=roots))
            self.assertEqual(locate_session_transcript("codex-99", search_roots=roots), session_file)
            self.assertEqual(locate_session_transcript("grok-sess-7", search_roots=roots), grok_file)
            self.assertIn("Reviewed cleanly", condense_transcript(grok_file))

            evidence = build_run_evidence(
                {
                    "run_id": "mac:Example/Engine#1:t1",
                    "repo": "Example/Engine",
                    "issue": 1,
                    "host": "mac-studio",
                    "stop": "verified",
                    "verify_rounds": 0,
                    "review_rounds": 0,
                    "total_tokens": 500,
                    "cost_usd": 0.12,
                    "duration_ms": 1000,
                    "models_used": {"implement": "gpt-6.2-sol-high"},
                    "state": {"request_title": "Add test for owner@example.invalid", "sessions": {"implement": "codex-99"}},
                },
                stage_calls=[{"seq": 0, "node": "implement", "model": "gpt-6.2-sol-high", "status": "ran", "total_tokens": 500, "duration_ms": 1000, "session_id": "codex-99"}],
                search_roots=roots,
                diff_text="+ added line\n",
            )
            self.assertIn("Transcript (codex-99):", evidence)
            self.assertIn("Built feature", evidence)
            self.assertIn("Diff excerpt:", evidence)
            self.assertNotIn("owner@example.invalid", evidence)


if __name__ == "__main__":
    unittest.main()
