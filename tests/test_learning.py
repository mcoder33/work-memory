import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import test_memory
from work_memory import Memory
from memory_hooks import handle


class LearningTest(unittest.TestCase):
    setUp = test_memory.MemoryTest.setUp
    tearDown = test_memory.MemoryTest.tearDown
    write = test_memory.MemoryTest.write
    user = test_memory.MemoryTest.user
    final = test_memory.MemoryTest.final
    result = test_memory.MemoryTest.result

    def ref(self, text="Observed result"):
        self.final(text)
        self.memory.sync()
        row = self.memory.session_events("session-0001")[-1]
        return {"event_id": row["id"], "quote": text}

    def item(self, kind="topic", key="delivery-topic"):
        x = {"key": key, "kind": kind, "project": "pilot", "title": "Delivery diagnosis",
             "body": "Check offsets and downstream result", "evidence": [self.ref()],
             "links": [], "base_revision": None}
        if kind == "procedure":
            x.update(trigger="When diagnosing missing delivery", steps=["Check offset movement", "Verify downstream"],
                     pitfalls=["A running process is not proof of delivery"], checks=["Observe a fresh delivered record"])
        return x

    def apply(self, items):
        r = self.result()
        r["learning"] = items
        return self.memory.consolidate(r)

    def add_session(self, sid, message="Baseline 20s, candidate 10s, both successful", fork=None, project="pilot"):
        path = self.source / (sid + ".jsonl")
        meta = {"id": sid, "cwd": "/projects/" + project, "timestamp": "2026-10-16T00:00:00Z"}
        if fork:
            meta["forked_from_id"] = fork
        lines = [{"type": "session_meta", "payload": meta},
                 {"type": "event_msg", "timestamp": "2026-10-16T00:01:00Z", "payload": {"type": "task_complete", "last_agent_message": message}}]
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        self.memory.sync()
        event = self.memory.session_events(sid)[-1]
        return {"event_id": event["id"], "quote": message}

    def trial(self, key, sid, ident=None, **changes):
        row = self.memory.learning.show(key)
        ref = self.add_session(sid)
        raw = {"id": ident or sid, "kind": "trial", "key": key, "revision": row["revision"],
               "session_id": sid, "note": "Independent comparison", "holdout": True, "evidence": [ref],
               "baseline": {"success": True, "errors": 0, "seconds": 20},
               "candidate": {"success": True, "errors": 0, "seconds": 10}}
        raw.update(changes)
        return raw

    def test_topic_creation_links_context_and_revision_conflict(self):
        a = self.item(); b = self.item(key="delivery-neighbor"); b["links"] = [a["key"]]
        self.apply([a, b])
        result = self.memory.context("delivery", "pilot")
        self.assertTrue(result["learning"])
        self.assertTrue((self.vault / "Память/Темы/delivery-topic.md").exists())
        self.assertIn("delivery-topic", (self.vault / "Память/Темы/delivery-neighbor.md").read_text())
        self.user("A later task")
        with self.assertRaisesRegex(ValueError, "Версия"):
            self.apply([a])

    def test_invalid_learning_rolls_back_summary_and_knowledge(self):
        x = self.item(); x["links"] = ["unknown-topic"]
        r = self.result(); r["learning"] = [x]
        with self.assertRaises(ValueError):
            self.memory.consolidate(r)
        self.assertIsNone(self.memory.db.execute("SELECT summary FROM sessions WHERE id='session-0001'").fetchone()[0])
        self.assertEqual(self.memory.db.execute("SELECT COUNT(*) FROM learning_items").fetchone()[0], 0)

    def test_source_snapshot_redacted_immutable_and_scoped(self):
        a = self.memory.learning.source("pilot", "Docs", "https://example.org", "password=secret-value Pagination uses cursors.")
        b = self.memory.learning.source("pilot", "Docs", "https://example.org", "password=secret-value Pagination uses cursors.")
        self.assertEqual(a["source_id"], b["source_id"])
        row = self.memory.db.execute("SELECT content FROM learning_sources").fetchone()[0]
        self.assertNotIn("secret-value", row)
        x = self.item(); x["evidence"] = [{"source_id": a["source_id"], "quote": "Pagination uses cursors."}]
        self.apply([x])
        self.assertIn("Источники", (self.vault / "Память/Темы/delivery-topic.md").read_text())
        with self.assertRaisesRegex(ValueError, "другого проекта"):
            self.memory.learning.evidence(x["evidence"], "other")

    def test_wrapped_hooks_filtered_and_legacy_role_repaired(self):
        text = '<hook_prompt hook_run_id="stop:8">[work-memory maintenance] Save memory</hook_prompt>'
        self.user(text)
        self.memory.sync()
        self.assertEqual(self.memory.session_events("session-0001"), [])
        with self.memory.write():
            self.memory.add_event("session-0001", "user", text, "2026-10-16", "old", 1)
        event = self.memory.db.execute("SELECT id FROM events").fetchone()[0]
        self.memory.close(); self.memory = Memory(self.config)
        self.assertEqual(self.memory.db.execute("SELECT role FROM events WHERE id=?", (event,)).fetchone()[0], "system")
        self.assertFalse(self.memory.packet("session-0001")["pending"])
        with self.assertRaises(ValueError):
            self.memory.learning.evidence([{"event_id": event, "quote": text}], "pilot")
        self.assertEqual(handle(self.memory, {"hook_event_name": "UserPromptSubmit", "session_id": "session-0001",
                                             "cwd": "/projects/pilot", "prompt": text}), {})

    def test_procedure_requires_two_independent_holdouts(self):
        self.apply([self.item("procedure", "offset-check")])
        first = self.trial("offset-check", "session-trial-001")
        self.memory.learning.observe(first)
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["status"], "candidate")
        self.memory.learning.observe(first)
        again = {**first, "id": "same-session-again"}
        self.memory.learning.observe(again)
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["holdout_wins"], 1)
        self.memory.learning.observe(self.trial("offset-check", "session-trial-002"))
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["status"], "validated")

    def test_training_session_and_forks_cannot_be_holdout(self):
        self.apply([self.item("procedure", "offset-check")])
        ref = self.memory.learning.show("offset-check")["evidence"][0]
        raw = self.trial("offset-check", "session-trial-001", session_id="session-0001", evidence=[ref])
        with self.assertRaisesRegex(ValueError, "Holdout"):
            self.memory.learning.observe(raw)
        ref = self.add_session("session-fork-0001", fork="session-0001")
        raw.update(session_id="session-fork-0001", evidence=[ref])
        with self.assertRaisesRegex(ValueError, "независимая"):
            self.memory.learning.observe(raw)

    def test_failure_overrules_wins_and_unknown_use_is_not_success(self):
        self.apply([self.item("procedure", "offset-check")])
        for sid in ("session-trial-001", "session-trial-002"):
            self.memory.learning.observe(self.trial("offset-check", sid))
        raw = self.trial("offset-check", "session-trial-003")
        raw.update(kind="use", outcome="unknown")
        self.memory.learning.observe(raw)
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["uses"], 1)
        raw.update(id="failed-use-001", outcome="failure")
        self.memory.learning.observe(raw)
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["status"], "needs-review")
        self.assertTrue(self.memory.learning.curate("pilot")["findings"])

    def test_bad_metrics_and_mutating_observation_rejected(self):
        self.apply([self.item("procedure", "offset-check")])
        raw = self.trial("offset-check", "session-trial-001")
        bad = copy.deepcopy(raw); bad["candidate"]["seconds"] = float("nan")
        with self.assertRaises(ValueError):
            self.memory.learning.observe(bad)
        self.memory.learning.observe(raw)
        raw["note"] = "Changed later"
        with self.assertRaisesRegex(ValueError, "история неизменяема"):
            self.memory.learning.observe(raw)

    def test_new_revision_does_not_inherit_validation(self):
        x = self.item("procedure", "offset-check"); self.apply([x])
        for sid in ("session-trial-001", "session-trial-002"):
            self.memory.learning.observe(self.trial("offset-check", sid))
        previous = self.memory.learning.show("offset-check")
        x["base_revision"] = previous["revision"]; x["steps"].append("Check downstream age")
        self.user("Update the procedure")
        self.apply([x])
        current = self.memory.learning.show("offset-check")
        self.assertEqual(current["quality"]["status"], "candidate")
        self.assertEqual(len(current["history"]), 1)
        self.assertEqual(len(current["observations"]), 2)

    def test_archive_restore_pin_and_manual_notes(self):
        self.apply([self.item("procedure", "offset-check")])
        row = self.memory.learning.show("offset-check"); rev = row["revision"]
        note = self.vault / "Память/Процедуры/offset-check.md"
        note.write_text(note.read_text() + "Personal note\n")
        self.memory.learning.manage("offset-check", "pin", rev)
        with self.assertRaises(ValueError):
            self.memory.learning.manage("offset-check", "archive", rev)
        self.memory.learning.manage("offset-check", "unpin", rev)
        self.memory.learning.manage("offset-check", "archive", rev)
        self.assertEqual(self.memory.learning.catalog("pilot", "delivery"), [])
        self.memory.learning.manage("offset-check", "restore", rev)
        self.assertTrue(self.memory.learning.catalog("pilot", "delivery"))
        self.assertIn("Personal note", note.read_text())

    def test_curator_reports_staleness_contradictions_duplicates_without_writes(self):
        a = self.item(); a.update(review_after="2020-01-01", contradictions=["Source A disagrees with B"])
        b = self.item(key="delivery-second")
        self.apply([a, b])
        before = self.memory.db.total_changes
        types = {x["type"] for x in self.memory.learning.curate("pilot")["findings"]}
        self.assertTrue({"stale", "contradiction", "possible-duplicate"}.issubset(types))
        self.assertEqual(before, self.memory.db.total_changes)

    def test_export_requires_validated_revision_user_approval_and_never_overwrites(self):
        self.apply([self.item("procedure", "offset-check")])
        row = self.memory.learning.show("offset-check")
        args = ("offset-check", row["revision"], row["evidence"][0]["event_id"], "Observed result", self.root / "skills")
        with self.assertRaises(ValueError):self.memory.learning.export(*args)
        for sid in ("session-trial-001", "session-trial-002"):
            self.memory.learning.observe(self.trial("offset-check", sid))
        with self.assertRaises(ValueError):self.memory.learning.export(*args)
        self.user("Approve skill offset-check " + row["revision"][:12])
        self.memory.sync(); approval = self.memory.session_events("session-0001")[-1]
        args = ("offset-check", row["revision"], approval["id"], approval["text"], self.root / "skills")
        result = self.memory.learning.export(*args)
        skill = Path(result["skill"])
        self.assertIn("Check offset movement", (skill / "SKILL.md").read_text())
        self.assertTrue((skill / "references/provenance.json").exists())
        with self.assertRaises(ValueError):self.memory.learning.export(*args)

    def test_outbox_recovers_topic_publication_after_write_failure(self):
        x = self.item()
        import work_memory
        original = work_memory.atomic_write
        def fail_topic(path, *args, **kwargs):
            if Path(path).name == "delivery-topic.md":raise OSError("simulated disk failure")
            return original(path, *args, **kwargs)
        with patch("work_memory.atomic_write", side_effect=fail_topic):
            self.apply([x])
        self.assertGreater(self.memory.status()["pending_writes"], 0)
        self.memory.close(); self.memory = Memory(self.config)
        self.assertEqual(self.memory.status()["pending_writes"], 0)
        self.assertTrue((self.vault / "Память/Темы/delivery-topic.md").exists())

    def test_rollback_restores_exact_content_and_keeps_newer_history(self):
        x = self.item(); self.apply([x]); original = self.memory.learning.show(x["key"])
        x.update(base_revision=original["revision"], body="Updated knowledge")
        self.user("Update the topic"); self.apply([x])
        newer = self.memory.learning.show(x["key"])
        restored = self.memory.learning.rollback(x["key"], newer["revision"], original["revision"])
        self.assertEqual(restored["body"], original["body"])
        self.assertIn(newer["revision"], [r["revision"] for r in restored["history"]])
        with self.assertRaises(ValueError):
            self.memory.learning.rollback(x["key"], newer["revision"], original["revision"])

    def test_new_source_snapshot_marks_old_reference_for_review(self):
        a = self.memory.learning.source("pilot", "Protocol", "https://example.org", "Version one")
        x = self.item(); x["evidence"] = [{"source_id": a["source_id"], "quote": "Version one"}]
        self.apply([x])
        self.memory.learning.source("pilot", "Protocol", "https://example.org", "Version two")
        self.assertIn("newer-source", [f["type"] for f in self.memory.learning.curate("pilot")["findings"]])

    def test_invalidated_observation_cannot_keep_procedure_validated(self):
        self.apply([self.item("procedure", "offset-check")])
        first = self.trial("offset-check", "session-trial-001")
        self.memory.learning.observe(first)
        self.memory.learning.observe(self.trial("offset-check", "session-trial-002"))
        with self.memory.db:
            self.memory.db.execute("UPDATE events SET role='system' WHERE id=?", (first["evidence"][0]["event_id"],))
        self.assertEqual(self.memory.learning.show("offset-check")["quality"]["status"], "needs-review")

    def test_short_acknowledgment_does_not_retrieve_unrelated_global_notes(self):
        self.user("Готово, проверили доставку"); self.memory.sync()
        self.assertEqual(self.memory.find("готово", "pilot"), [])
