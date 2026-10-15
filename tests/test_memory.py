import json
from pathlib import Path
import tempfile
import unittest

from work_memory import Memory, digest, redact


class MemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.source = self.root / "sessions"
        self.source.mkdir()
        self.log = self.source / "rollout-2026-10-15-example.jsonl"
        self.config = {"vault": str(self.vault), "state": str(self.root / "state"),
                       "sources": [str(self.source)], "since": "2026-10-15",
                       "projects": {"/projects/pilot": "pilot"}}
        self.memory = Memory(self.config)
        self.write("session_meta", {"id": "session-0001", "cwd": "/projects/pilot", "source": "vscode", "timestamp": "2026-10-15T00:00:00Z"})

    def tearDown(self):
        self.memory.db.close()
        self.tmp.cleanup()

    def write(self, kind, payload, timestamp="2026-10-15T01:00:00Z", newline=True):
        with self.log.open("a") as out:
            out.write(json.dumps({"type": kind, "payload": payload, "timestamp": timestamp}, ensure_ascii=False) + ("\n" if newline else ""))

    def user(self, text):
        self.write("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})

    def final(self, text):
        self.write("event_msg", {"type": "task_complete", "last_agent_message": text})

    def result(self, knowledge=None):
        self.memory.sync()
        packet = self.memory.packet()
        result = packet["result_template"]
        result.update(goal="Восстановить доставку", summary="Проверили маршрут", outcome="Причина ещё проверяется", next_steps=["Прочитать свежие события"], knowledge=knowledge or [])
        return result

    def knowledge(self, status="confirmed", role="user", key="route-decision"):
        event = self.memory.db.execute("SELECT * FROM events WHERE role=? LIMIT 1", (role,)).fetchone()
        return {"key": key, "kind": "decision", "title": "Доставка", "body": "Выбрали маршрут", "status": status,
                "project": "pilot", "evidence": [{"event_id": event["id"], "quote": event["text"]}], "supersedes": [], "base_revision": None}

    def test_incremental_ingest_and_idempotent_notes(self):
        self.user("Найди ошибку доставки")
        self.final("Пока проверяем")
        self.assertEqual(self.memory.sync()["added_events"], 2)
        snapshot = {p: p.read_bytes() for p in self.vault.rglob("*.md")}
        self.assertEqual(self.memory.sync()["added_events"], 0)
        self.assertEqual(snapshot, {p: p.read_bytes() for p in self.vault.rglob("*.md")})
        self.user("Продолжи поиск")
        self.assertEqual(self.memory.sync()["added_events"], 1)

    def test_partial_line_is_retried(self):
        self.user("Начало")
        self.write("event_msg", {"type": "task_complete", "last_agent_message": "Конец"}, newline=False)
        self.assertEqual(self.memory.sync()["added_events"], 1)
        with self.log.open("a") as out:
            out.write("\n")
        self.assertEqual(self.memory.sync()["added_events"], 1)

    def test_injected_rules_reasoning_and_tool_dumps_not_imported(self):
        self.user("# AGENTS.md instructions\nSECRET")
        self.write("response_item", {"type": "reasoning", "summary": "PRIVATE"})
        self.write("response_item", {"type": "custom_tool_call_output", "output": "DATABASE_DUMP"})
        self.user("Содержательная задача")
        self.memory.sync()
        rows = self.memory.session_events("session-0001")
        self.assertEqual([r["text"] for r in rows], ["Содержательная задача"])

    def test_child_session_excluded(self):
        self.log.write_text("")
        self.write("session_meta", {"id": "child-00001", "cwd": "/projects/pilot", "source": {"subagent": {}}, "timestamp": "2026-10-15T00:00:00Z"})
        self.user("Не самостоятельная сессия")
        self.assertEqual(self.memory.sync()["sessions"], 0)

    def test_nested_metadata_does_not_change_identity(self):
        self.user("Первый запрос")
        self.write("session_meta", {"id": "old-id-00001", "cwd": "/old", "timestamp": "2026-10-14T00:00:00Z"})
        self.user("Новый запрос")
        self.memory.sync()
        self.assertEqual(len(self.memory.session_events("session-0001")), 2)

    def test_credentials_redacted_before_persistence(self):
        self.user("password=ultra-secret Bearer abcdefghijkl postgres://user:password@host/db")
        self.memory.sync()
        text = self.memory.session_events("session-0001")[0]["text"]
        self.assertNotIn("ultra-secret", text)
        self.assertNotIn("abcdefghijkl", text)
        self.assertNotIn("user:password", text)

    def test_manual_edits_outside_block_survive(self):
        self.user("Начало")
        self.memory.sync()
        path = self.vault / self.memory.session_path("session-0001")
        path.write_text(path.read_text() + "\nМоя заметка\n")
        self.user("Продолжение")
        self.memory.sync()
        self.assertIn("Моя заметка", path.read_text())
        self.assertIn("Продолжение", path.read_text())

    def test_manual_edit_inside_block_becomes_conflict(self):
        self.user("Начало")
        self.memory.sync()
        path = self.vault / self.memory.session_path("session-0001")
        path.write_text(path.read_text().replace("Начало", "Моя правка"))
        before = path.read_text()
        self.user("Продолжение")
        state = self.memory.sync()
        self.assertEqual(path.read_text(), before)
        self.assertTrue(any(i["key"].startswith("conflict:") for i in state["issues"]))

    def test_packet_and_grounded_consolidation(self):
        self.user("Выбираем маршрут А")
        result = self.result()
        result["knowledge"] = [self.knowledge()]
        self.memory.consolidate(result)
        self.assertFalse(self.memory.packet()["pending"])
        self.assertTrue((self.vault / "Память/Знания/route-decision.md").exists())
        self.assertTrue(self.memory.find("Доставка", "pilot"))

    def test_unfounded_confirmation_rejected_atomically(self):
        self.final("Это точно принято")
        result = self.result()
        result["knowledge"] = [self.knowledge(role="assistant")]
        with self.assertRaisesRegex(ValueError, "пользователя"):
            self.memory.consolidate(result)
        self.assertEqual(self.memory.status()["knowledge"], 0)
        self.assertTrue(self.memory.packet()["pending"])

    def test_false_quote_and_stale_revision_rejected(self):
        self.user("Выбираем маршрут А")
        result = self.result()
        knowledge = self.knowledge()
        knowledge["evidence"][0]["quote"] = "Пользователь этого не говорил"
        result["knowledge"] = [knowledge]
        with self.assertRaisesRegex(ValueError, "Цитата"):
            self.memory.consolidate(result)
        result["knowledge"] = []
        receipt = self.memory.consolidate(result)
        self.assertEqual(self.memory.consolidate(result), receipt)
        result["summary"] = "Другой результат из устаревшего снимка"
        with self.assertRaisesRegex(ValueError, "изменён"):
            self.memory.consolidate(result)

    def test_new_events_after_summary_remain_pending(self):
        self.user("Начинаем")
        result = self.result()
        self.user("Условия изменились")
        self.memory.sync()
        self.memory.consolidate(result)
        packet = self.memory.packet()
        self.assertEqual(len(packet["events"]), 1)
        self.assertEqual(packet["events"][0]["text"], "Условия изменились")

    def test_superseded_metadata_is_updated(self):
        self.user("Выбрали А")
        result = self.result()
        result["knowledge"] = [self.knowledge()]
        self.memory.consolidate(result)
        self.user("Теперь выбираем Б")
        result = self.result()
        item = self.knowledge(key="new-route-decision")
        item["supersedes"] = ["route-decision"]
        result["knowledge"] = [item]
        self.memory.consolidate(result)
        doc = self.memory.db.execute("SELECT status FROM documents WHERE path='Память/Знания/route-decision.md'").fetchone()
        self.assertEqual(doc[0], "superseded")

    def test_reindex_reads_human_correction(self):
        path = self.vault / "Исправление.md"
        path.write_text("---\nproject: pilot\n---\nМаршрутизация исправлена человеком")
        self.memory.sync()
        self.assertTrue(self.memory.find("Маршрутизация", "pilot"))
        path.unlink()
        self.memory.sync()
        self.assertFalse(self.memory.find("Маршрутизация", "pilot"))

    def test_project_scope_and_path_traversal(self):
        self.user("Выбираем А")
        result = self.result()
        item = self.knowledge()
        item["key"] = "../../outside"
        result["knowledge"] = [item]
        with self.assertRaises(ValueError):
            self.memory.consolidate(result)
        item["key"] = "valid-key"
        item["project"] = "other"
        with self.assertRaisesRegex(ValueError, "другому проекту"):
            self.memory.consolidate(result)

    def test_signals_require_independent_sessions(self):
        prompt = "Всегда сохраняй основания решения отдельной заметкой"
        self.user(prompt)
        self.user(prompt)
        self.memory.sync()
        self.assertFalse(self.memory.signals())
        self.log = self.source / "rollout-2026-10-15-second.jsonl"
        self.write("session_meta", {"id": "session-0002", "cwd": "/projects/pilot", "source": "vscode", "timestamp": "2026-10-15T00:00:00Z"})
        self.user(prompt)
        self.memory.sync()
        signals = self.memory.signals()
        self.assertTrue(signals)
        self.assertEqual(signals[0]["status"], "proposed")
        self.assertEqual(len(signals[0]["sessions"]), 2)

    def test_knowledge_versions_are_retained(self):
        self.user("Выбираем А")
        result = self.result()
        result["knowledge"] = [self.knowledge()]
        self.memory.consolidate(result)
        original = self.memory.db.execute("SELECT * FROM knowledge").fetchone()
        self.user("Уточнение условий")
        result = self.result()
        item = self.knowledge()
        item["base_revision"] = original["revision"]
        item["body"] = "Уточнённое решение"
        result["knowledge"] = [item]
        self.memory.consolidate(result)
        history = self.memory.db.execute("SELECT * FROM knowledge_history").fetchone()
        self.assertEqual(history["payload"], original["payload"])
        self.assertEqual(len(self.memory.show(item["key"])["history"]), 1)

    def test_transport_wrappers_are_not_work_patterns(self):
        prompt = '<send_user_message_question_reply>questionItemId request user input async</send_user_message_question_reply>'
        self.user(prompt)
        self.log = self.source / "rollout-2026-10-15-other.jsonl"
        self.write("session_meta", {"id": "session-0002", "cwd": "/projects/pilot", "source": "vscode", "timestamp": "2026-10-15T00:00:00Z"})
        self.user(prompt)
        self.memory.sync()
        self.assertFalse(self.memory.signals())

    def test_checkpoint_is_grounded_agent_report_and_idempotent(self):
        self.user("Реализуй")
        self.memory.sync()
        artifact = self.root / "implementation.txt"
        artifact.write_text("result")
        self.memory.checkpoint("session-0001", "Результат сохранён", [artifact])
        self.memory.checkpoint("session-0001", "Результат сохранён", [artifact])
        events = self.memory.session_events("session-0001")
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["role"], "assistant")
        self.assertIn(digest("result"), events[-1]["text"])
        self.assertTrue(Path(events[-1]["source"]).exists())


if __name__ == "__main__":
    unittest.main()
