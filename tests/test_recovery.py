"""Process-kill recovery on isolated journals, databases and vaults."""
import contextlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import work_memory
from work_memory import Memory


CHILD = r'''
import json,os,signal,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import work_memory as w
config=json.loads(sys.argv[2]);mode=sys.argv[3];point=sys.argv[4]
def stop():os.kill(os.getpid(),signal.SIGKILL)
def target(path):
    path=Path(path)
    return (path.parent.name == "checkpoints") if mode == "checkpoint" else path.name == "session-recovery-0001.md"
original_write=w.atomic_write
def write(path,text,expected=None):
    if target(path) and point == "before_publish":stop()
    result=original_write(path,text,expected)
    if target(path) and point == "after_publish":stop()
    return result
w.atomic_write=write
original_replace=w.os.replace
def replace(source,destination):
    if target(destination) and point == "before_replace":stop()
    result=original_replace(source,destination)
    if target(destination) and point == "after_replace":stop()
    return result
w.os.replace=replace
original_center=w.Memory.render_center
def center(self):
    result=original_center(self)
    if point == "before_commit" and mode != "checkpoint":stop()
    return result
w.Memory.render_center=center
original_event=w.Memory.add_event
def event(self,*args,**kwargs):
    result=original_event(self,*args,**kwargs)
    if point == "before_commit" and mode == "checkpoint":stop()
    return result
w.Memory.add_event=event
original_index=w.Memory.index
index_calls=0
def index(self):
    global index_calls
    index_calls+=1
    if point == "before_index" and index_calls == 2:stop()
    return original_index(self)
w.Memory.index=index
original_connect=w.sqlite3.connect
class Connection(w.sqlite3.Connection):
    def execute(self,sql,parameters=()):
        result=super().execute(sql,parameters)
        if point == "before_ack" and sql.startswith("DELETE FROM file_outbox"):
            if (mode == "checkpoint" and parameters[0] == "checkpoint") or (mode != "checkpoint" and parameters[1].endswith("session-recovery-0001.md")):stop()
        return result
def connect(*args,**kwargs):
    kwargs["factory"]=Connection
    return original_connect(*args,**kwargs)
w.sqlite3.connect=connect
m=w.Memory(config)
if mode == "sync":m.sync()
elif mode == "consolidate":m.consolidate(json.loads(Path(sys.argv[5]).read_text()))
else:m.checkpoint("session-recovery-0001","Report saved",[])
raise SystemExit("Fault point not reached")
'''


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="work-memory-fault-")
        self.root = Path(self.tmp.name)
        self.vault = self.root / "vault"
        self.logs = self.root / "logs"
        self.vault.mkdir()
        self.logs.mkdir()
        self.log = self.logs / "rollout-2026-10-16-fixture.jsonl"
        self.config = {"vault": str(self.vault), "state": str(self.root / "state"),
                       "sources": [str(self.logs)], "since": "2026-10-16"}
        self.note = self.vault / "Память/Сессии/session-recovery-0001.md"
        self.append("session_meta", {"id": "session-recovery-0001", "cwd": "/synthetic/project",
                                     "source": "vscode", "timestamp": "2026-10-16T00:00:00Z"})
        self.user("Initial request")
        with contextlib.closing(Memory(self.config)) as memory:
            memory.sync()

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, kind, payload):
        with self.log.open("a") as handle:
            handle.write(json.dumps({"type": kind, "payload": payload, "timestamp": "2026-10-16T01:00:00Z"}) + "\n")

    def user(self, text):
        self.append("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})

    def kill_at(self, point, mode="sync", result=None):
        result_path = self.root / "result.json"
        if result is not None:
            result_path.write_text(json.dumps(result))
        completed = subprocess.run([sys.executable, "-c", CHILD, str(Path(work_memory.__file__).parent),
                                    json.dumps(self.config), mode, point, str(result_path)],
                                   capture_output=True, text=True, timeout=15)
        self.assertEqual(completed.returncode, -9, completed.stdout + completed.stderr)

    def assert_recovered(self, expected_events):
        with contextlib.closing(Memory(self.config)) as memory:
            state = memory.sync()
            self.assertEqual(state["events"], expected_events)
            self.assertEqual(state["pending_writes"], 0)
            self.assertFalse(state["issues"])
            self.assertEqual(memory.db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertIn("Latest request", self.note.read_text())
            self.assertTrue(memory.find("Latest request", "project"))
            snapshot = {p: p.read_bytes() for p in self.vault.rglob("*.md")}
            self.assertEqual(memory.sync()["added_events"], 0)
            self.assertEqual(snapshot, {p: p.read_bytes() for p in self.vault.rglob("*.md")})

    def test_sync_kill_before_database_commit(self):
        before = self.note.read_bytes()
        self.user("Intermediate request")
        self.kill_at("before_commit")
        self.assertEqual(self.note.read_bytes(), before)
        self.user("Latest request")
        self.assert_recovered(3)

    def test_sync_kill_after_commit_before_file_write(self):
        self.user("Intermediate request")
        self.kill_at("before_publish")
        self.user("Latest request")
        self.assert_recovered(3)

    def test_sync_kill_during_file_replace(self):
        self.user("Intermediate request")
        self.kill_at("before_replace")
        self.user("Latest request")
        self.assert_recovered(3)

    def test_original_regression_file_written_but_not_acknowledged(self):
        self.user("Intermediate request")
        self.kill_at("after_publish")
        self.assertIn("Intermediate request", self.note.read_text())
        self.user("Latest request")
        self.assert_recovered(3)

    def test_sync_kill_immediately_after_rename(self):
        self.user("Intermediate request")
        self.kill_at("after_replace")
        self.user("Latest request")
        self.assert_recovered(3)

    def test_sync_kill_inside_acknowledgement_transaction(self):
        self.user("Intermediate request")
        self.kill_at("before_ack")
        self.user("Latest request")
        self.assert_recovered(3)

    def test_index_recovers_after_all_files_acknowledged(self):
        self.user("Intermediate request")
        self.kill_at("before_index")
        with contextlib.closing(Memory(self.config)) as memory:
            self.assertTrue(memory.find("Intermediate request", "project"))
        self.user("Latest request")
        self.assert_recovered(3)

    def test_personal_notes_survive_on_both_sides_of_publication(self):
        for point in ("before_publish", "after_publish"):
            with self.subTest(point=point):
                self.user("Intermediate " + point)
                self.kill_at(point)
                with self.note.open("a") as handle:
                    handle.write("\nHuman appendix " + point + "\n")
                with contextlib.closing(Memory(self.config)) as memory:
                    self.assertEqual(memory.status()["pending_writes"], 0)
                    self.assertFalse(memory.status()["issues"])
                self.assertIn("Human appendix " + point, self.note.read_text())

    def test_manual_metadata_survives_replay(self):
        self.user("Intermediate request")
        self.kill_at("after_publish")
        self.note.write_text(self.note.read_text().replace('project: "project"', 'project: "human-project"'))
        with contextlib.closing(Memory(self.config)) as memory:
            self.assertEqual(memory.status()["pending_writes"], 0)
        self.assertIn('project: "human-project"', self.note.read_text())

    def test_real_human_edit_blocks_only_its_note_and_can_resume(self):
        self.user("Intermediate request")
        self.kill_at("after_publish")
        published = self.note.read_text()
        changed = published.replace("Initial request", "Human correction")
        self.note.write_text(changed)
        self.user("Latest request")
        with contextlib.closing(Memory(self.config)) as memory:
            state = memory.sync()
            self.assertEqual(state["events"], 3)
            self.assertEqual(state["pending_writes"], 1)
            self.assertTrue(any(i["key"].startswith("publication:") for i in state["issues"]))
        self.assertEqual(self.note.read_text(), changed)
        self.note.write_text(published + "\nHuman correction moved outside the generated block\n")
        self.assert_recovered(3)
        self.assertIn("Human correction moved outside", self.note.read_text())

    def test_consolidation_survives_commit_and_publication_interrupts(self):
        for point in ("before_commit", "before_publish", "after_publish", "before_ack", "before_index"):
            with self.subTest(point=point):
                self.user("New goal " + point)
                with contextlib.closing(Memory(self.config)) as memory:
                    memory.sync()
                    result = memory.packet("session-recovery-0001")["result_template"]
                    result.update(goal="Goal " + point, summary="Summary " + point, outcome="Saved", next_steps=[])
                    evidence = memory.session_events("session-recovery-0001")[-1]
                    key = "recovery-knowledge-" + point.replace("_", "-")
                    result["knowledge"] = [{"key": key, "kind": "question", "status": "open", "project": "project",
                                            "title": "Knowledge " + point, "body": "Question " + point,
                                            "evidence": [{"event_id": evidence["id"], "quote": evidence["text"]}],
                                            "supersedes": [], "base_revision": None}]
                self.kill_at(point, mode="consolidate", result=result)
                with contextlib.closing(Memory(self.config)) as memory:
                    receipt = memory.consolidate(result)
                    self.assertEqual(memory.consolidate(result), receipt)
                    self.assertFalse(memory.packet("session-recovery-0001")["pending"])
                    self.assertEqual(memory.status()["pending_writes"], 0)
                    self.assertFalse(memory.status()["issues"])
                    self.assertIn("Summary " + point, self.note.read_text())
                    self.assertIn("Question " + point, (self.vault / f"Память/Знания/{key}.md").read_text())
                    self.assertEqual(memory.show(key)["history"], [])

    def test_first_file_creation_survives_interrupted_publication(self):
        fresh = self.root / "fresh"
        self.vault = fresh / "vault"
        self.logs = fresh / "logs"
        self.vault.mkdir(parents=True)
        self.logs.mkdir()
        old_log = self.log
        self.log = self.logs / old_log.name
        self.log.write_bytes(old_log.read_bytes())
        self.config.update(vault=str(self.vault), state=str(fresh / "state"), sources=[str(self.logs)])
        self.note = self.vault / "Память/Сессии/session-recovery-0001.md"
        self.kill_at("after_publish")
        self.user("Latest request")
        self.assert_recovered(2)

    def test_checkpoint_source_and_event_recover_together(self):
        for point in ("before_commit", "before_publish", "after_publish", "before_ack"):
            # Distinct report per iteration keeps the source publication new.
            with self.subTest(point=point):
                child = CHILD.replace('"Report saved"', repr("Report " + point))
                completed = subprocess.run([sys.executable, "-c", child, str(Path(work_memory.__file__).parent),
                                            json.dumps(self.config), "checkpoint", point, "unused"],
                                           capture_output=True, text=True, timeout=15)
                self.assertEqual(completed.returncode, -9, completed.stderr)
                with contextlib.closing(Memory(self.config)) as memory:
                    result = memory.checkpoint("session-recovery-0001", "Report " + point, [])
                    self.assertTrue(Path(result["checkpoint"]).exists())
                    count = memory.status()["events"]
                    memory.checkpoint("session-recovery-0001", "Report " + point, [])
                    self.assertEqual(memory.status()["events"], count)
                    self.assertEqual(memory.status()["pending_writes"], 0)

    def test_failed_filesystem_write_retries_without_losing_source_data(self):
        self.user("Latest request")
        original = work_memory.atomic_write
        def disk_full(path, text, expected=None):
            if Path(path) == self.note:
                raise OSError("synthetic disk full")
            return original(path, text, expected)
        with contextlib.closing(Memory(self.config)) as memory:
            with patch("work_memory.atomic_write", disk_full):
                state = memory.sync()
            self.assertEqual(state["events"], 2)
            self.assertEqual(state["pending_writes"], 1)
        self.assert_recovered(2)

    def test_parallel_recovery_and_sync_serialize_without_duplicates(self):
        self.user("Intermediate request")
        self.kill_at("after_publish")
        self.user("Latest request")
        config = self.root / "config.json"
        config.write_text(json.dumps(self.config))
        processes = [subprocess.Popen([sys.executable, str(Path(work_memory.__file__)), "--config", str(config), "sync"],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
        for process in processes:
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, stdout + stderr)
            self.assertEqual(json.loads(stdout)["pending_writes"], 0)
        self.assert_recovered(3)


if __name__ == "__main__":
    unittest.main()
