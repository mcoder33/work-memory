import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_memory
from memory_hooks import MAINTENANCE, definitions, handle, merge_hooks
import install


class HookTest(unittest.TestCase):
    setUp = test_memory.MemoryTest.setUp
    tearDown = test_memory.MemoryTest.tearDown
    write = test_memory.MemoryTest.write
    user = test_memory.MemoryTest.user

    def event(self, name, **extra):
        return {"hook_event_name": name, "session_id": "session-0001",
                "cwd": "/projects/pilot", "turn_id": "turn-1", **extra}

    def test_start_catches_up_and_marks_history_untrusted(self):
        self.user("Нужно восстановление")
        out = handle(self.memory, self.event("SessionStart", source="compact"))
        self.assertEqual(len(self.memory.session_events("session-0001")), 1)
        text = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("недоверенные", text)
        self.assertIn("pilot", text)
        self.assertLess(len(text), 8000)

    def test_stop_captures_final_without_continuation(self):
        self.user("Сделай задачу")
        event = self.event("Stop", last_assistant_message="Результат выполнен")
        self.assertEqual(handle(self.memory, event), {})
        packet = self.memory.packet("session-0001")
        self.assertTrue(any("Результат выполнен" in x["text"] for x in packet["events"]))
        self.assertEqual(handle(self.memory, event), {})
        self.assertNotIn("decision", handle(self.memory, self.event("Stop", stop_hook_active=True)))
        result = packet["result_template"]
        result.update(goal="Задача", summary="Сделано", outcome="Готово", next_steps=[])
        self.memory.consolidate(result)
        self.assertEqual(handle(self.memory, self.event("Stop", stop_hook_active=True)), {})

    def test_maintenance_is_not_imported_as_user_authority(self):
        self.user(MAINTENANCE + " Сохрани память")
        out = handle(self.memory, self.event("UserPromptSubmit", prompt=MAINTENANCE + " Сохрани"))
        self.assertEqual(out, {})
        self.assertEqual(self.memory.session_events("session-0001"), [])

    def test_missing_session_and_missing_turn_fail_open(self):
        out = handle(self.memory, self.event("Stop", session_id="missing-0001"))
        self.assertIn("systemMessage", out)
        out = handle(self.memory, self.event("Stop", turn_id=None))
        self.assertNotIn("decision", out)

    def test_child_and_filtered_projects_do_not_inject(self):
        self.assertEqual(handle(self.memory, self.event("SessionStart", agent_id="child")), {})
        self.memory.config["include_projects"] = ["other"]
        self.assertEqual(handle(self.memory, self.event("UserPromptSubmit", prompt="Hello")), {})

    def test_config_merge_keeps_serena_and_is_idempotent(self):
        original = {"description": "keep", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "serena-hooks activate"}]}]}}
        additions = definitions("/path with space/python", "/runtime/memory_hooks.py", "/config.json")
        result = merge_hooks(original, additions)
        self.assertEqual(result["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        self.assertEqual(merge_hooks(result, additions), result)
        changed = merge_hooks(result, definitions("python", "/new/memory_hooks.py", "/config.json"))
        self.assertEqual(len(changed["hooks"]["SessionStart"]), 2)

    def test_cli_malformed_input_warns_without_blocking(self):
        config = self.root / "config.json"
        config.write_text(json.dumps(self.config))
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "memory_hooks.py"),
                               "--config", str(config)], input="{broken", text=True, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("systemMessage", json.loads(proc.stdout))

    def test_install_preserves_other_hooks_and_user_rules(self):
        home = self.root / "home"
        (home / ".config/work-memory").mkdir(parents=True)
        (home / ".config/work-memory/config.json").write_text(json.dumps(self.config))
        (home / ".codex").mkdir()
        hooks = home / ".codex/hooks.json"
        original = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "serena-hooks activate"}]}]}}
        hooks.write_text(json.dumps(original))
        agents = home / ".codex/AGENTS.md"
        agents.write_text("# Personal rules\nKeep this.\n")
        args = ["install.py", "--vault", str(self.vault), "--since", "2026-10-15", "--hooks", "--agent-instructions"]
        with patch("pathlib.Path.home", return_value=home), patch("sys.argv", args), patch("builtins.print"):
            install.install()
            first = hooks.read_text()
            agents.write_text(agents.read_text() + "\nCustom preservation rule.\n")
            install.install()
        self.assertEqual(hooks.read_text(), first)
        self.assertIn("Keep this.", agents.read_text())
        self.assertIn("Custom preservation rule.", agents.read_text())
        self.assertIn("[work-memory maintenance]", agents.read_text())
        self.assertNotIn("Перед содержательной задачей восстанови", agents.read_text())
        result = json.loads(first)
        self.assertEqual(result["hooks"]["SessionStart"][0], original["hooks"]["SessionStart"][0])
        self.assertIn("runtime", (home / ".local/bin/work-memory").read_text())

    def test_stop_receipt_survives_process_restart(self):
        self.user("Сохрани")
        event = self.event("Stop", last_assistant_message="Сохранено")
        self.assertEqual(handle(self.memory, event), {})
        self.memory.close()
        self.memory = test_memory.Memory(self.config)
        self.assertEqual(handle(self.memory, event), {})

    def test_prompt_context_redacts_note_secrets(self):
        self.user("Настройка")
        self.memory.sync()
        note = self.vault / "private-note.md"
        note.write_text("---\nproject: pilot\n---\nНастройка password=top-secret-value")
        out = handle(self.memory, self.event("UserPromptSubmit", prompt="Настройка"))
        self.assertNotIn("top-secret-value", json.dumps(out))

    def test_native_service_envelope_and_answer_are_audit_only_across_restart(self):
        self.user('<hook_prompt hook_run_id="stop:8:/tmp/hooks.json">[work-memory maintenance] Save</hook_prompt>')
        self.memory.sync()
        self.memory.close()
        self.memory = test_memory.Memory(self.config)
        self.user("# AGENTS.md instructions\nRuntime instructions")
        self.write("event_msg", {"type": "task_complete", "last_agent_message": "Память сохранена"})
        self.memory.sync()
        self.assertEqual(self.memory.session_events("session-0001"), [])
        self.assertFalse(self.memory.packet("session-0001")["pending"])
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM events WHERE role='system'").fetchone()[0], 2)
        self.user('Что означает "[work-memory maintenance]"?')
        self.write("event_msg", {"type": "task_complete", "last_agent_message": "Это служебный запрос"})
        self.memory.sync()
        self.assertEqual(len(self.memory.session_events("session-0001")), 2)

    def test_stop_write_failure_is_retried_without_receipt_claim(self):
        self.user("Сохрани результат")
        event = self.event("Stop", last_assistant_message="Результат")
        with patch.object(self.memory, "checkpoint", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                handle(self.memory, event)
        self.assertEqual(handle(self.memory, event), {})
        self.assertTrue(any("Результат" in e["text"] for e in self.memory.packet("session-0001")["events"]))

    def test_real_user_quote_of_hook_envelope_is_retained(self):
        self.user('Объясни: <hook_prompt hook_run_id="stop:8:x">[work-memory maintenance] Save</hook_prompt>')
        self.memory.sync()
        self.assertEqual(len(self.memory.session_events("session-0001")), 1)
