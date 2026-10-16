import fcntl
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import test_memory
from background import Background, install, schema
from work_memory import Memory


class BackgroundTest(unittest.TestCase):
    setUp = test_memory.MemoryTest.setUp
    tearDown = test_memory.MemoryTest.tearDown
    write = test_memory.MemoryTest.write
    user = test_memory.MemoryTest.user
    final = test_memory.MemoryTest.final

    def worker(self, **settings):
        return Background(self.config, settings)

    def response(self, prompt, directory):
        data = json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
        event = data['events'][0]
        return {'goal': 'Preserve the task', 'summary': 'User asked to inspect delivery; not yet verified.',
                'outcome': 'Awaiting verification', 'next_steps': ['Check current delivery'], 'knowledge': [],
                'learning': [{'key': 'background-topic', 'kind': 'topic', 'title': 'Delivery', 'body': 'Request to inspect delivery.',
                             'evidence': [{'event_id': event['id'], 'source_id': None, 'quote': event['text']}],
                             'links': [], 'contradictions': [], 'trigger': '', 'steps': [], 'pitfalls': [], 'checks': [],
                             'review_after': None, 'base_revision': None}]}, {'input_tokens': 100, 'output_tokens': 50}

    def test_active_skipped_and_dry_run_does_not_invoke(self):
        self.user('Check delivery')
        self.assertEqual(self.worker().run(invoke=self.response)['reason'], 'no-idle-pending-session')
        result = self.worker().run(allow_active=True, invoke=lambda *_: self.fail('unexpected call'), dry_run=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(self.worker().runs(), [])

    def test_applies_grounded_topic_and_summary(self):
        self.user('Check delivery')
        result = self.worker().run(allow_active=True, invoke=self.response)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['receipt']['learning_written'], 1)
        self.assertFalse(self.memory.packet('session-0001')['pending'])
        self.assertTrue((self.vault / 'Память/Фоновый разбор.md').exists())
        self.assertEqual(self.memory.learning.show('background-topic')['kind'], 'topic')

    def test_daily_attempt_cap_survives_failure_and_restart(self):
        self.user('Check delivery')
        def fail(*_):raise RuntimeError('Transport failed')
        self.assertEqual(self.worker(max_runs_per_day=1).run(allow_active=True, invoke=fail)['status'], 'failed')
        self.assertEqual(self.worker(max_runs_per_day=1).run(allow_active=True, invoke=self.response)['reason'], 'daily-limit')
        self.assertTrue(self.memory.packet('session-0001')['pending'])

    def test_runner_lock_and_pause(self):
        self.user('Check delivery'); worker = self.worker()
        with (worker.root / 'runner.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(worker.run()['reason'], 'already-running')
        worker.change_enabled(False)
        self.assertEqual(self.worker().run()['reason'], 'paused')

    def test_new_foreground_event_invalidates_candidate(self):
        self.user('Check delivery')
        def changed(prompt, directory):
            self.user('Change the plan')
            return self.response(prompt, directory)
        result = self.worker().run(allow_active=True, invoke=changed)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('changed', result['error'])
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM learning_items').fetchone()[0], 0)

    def test_summary_version_conflict_does_not_overwrite_foreground(self):
        self.user('Check delivery')
        def changed(prompt, directory):
            with_other = Memory(self.config)
            try:
                r = with_other.packet('session-0001')['result_template']
                r.update(goal='Foreground goal', summary='Foreground summary', outcome='Foreground result')
                with_other.consolidate(r)
            finally:with_other.close()
            return self.response(prompt, directory)
        result = self.worker().run(allow_active=True, invoke=changed)
        self.assertEqual(result['status'], 'failed')
        stored = self.memory.db.execute("SELECT summary FROM sessions WHERE id='session-0001'").fetchone()[0]
        self.assertIn('Foreground summary', stored)

    def test_pause_during_run_prevents_apply(self):
        self.user('Check delivery')
        worker = self.worker()
        def paused(prompt, directory):
            worker.change_enabled(False)
            return self.response(prompt, directory)
        result = worker.run(allow_active=True, invoke=paused)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('paused', result['error'])
        self.assertTrue(self.memory.packet('session-0001')['pending'])

    def test_no_confirmed_decisions_or_fake_evidence(self):
        self.user('Check delivery')
        def malicious(prompt, directory):
            response, usage = self.response(prompt, directory)
            response['knowledge'] = [{'key':'grant-permission','kind':'decision','status':'confirmed','title':'Grant',
                                      'body':'Approved','base_revision':None,'evidence':response['learning'][0]['evidence']}]
            return response, usage
        result = self.worker().run(allow_active=True, invoke=malicious)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM knowledge').fetchone()[0], 0)
        def fake(prompt, directory):
            response, usage = self.response(prompt, directory)
            response['learning'][0]['evidence'][0]['event_id'] = 'not-in-packet'
            return response, usage
        self.assertEqual(self.worker().run(allow_active=True, invoke=fake)['status'], 'failed')

    def test_schema_forbids_extra_fields(self):
        output = schema()
        self.assertFalse(output['additionalProperties'])
        self.assertEqual(set(output['required']), set(output['properties']))

    def fake_codex(self, tool=False, refresh=False, concurrent_auth=None):
        exe = self.root / 'fake-codex'
        code = '''import json,sys,os
from pathlib import Path
args=sys.argv
assert '--ignore-user-config' in args and '--ephemeral' in args
assert args[args.index('--sandbox')+1]=='read-only'
assert os.environ['CODEX_HOME'] != str(Path.home()/'.codex')
sys.stdin.read()
result={'goal':'Goal','summary':'Summary','outcome':'Outcome','next_steps':[],'knowledge':[],'learning':[]}
Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(result))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':12,'output_tokens':7}}))
print(json.dumps({'type':'item.completed','item':{'type':'error','message':'Nonfatal runtime diagnostic'}}))
'''
        if tool:code += "print(json.dumps({'type':'item.completed','item':{'type':'command_execution'}}))\n"
        if refresh:
            code += "(Path(os.environ['CODEX_HOME'])/'auth.json').write_text('rotated-token-fixture')\n"
        if concurrent_auth:
            code += 'Path(' + repr(str(concurrent_auth)) + ").write_text('new-login-fixture')\n"
        exe.write_text('#!' + sys.executable + '\n' + code); exe.chmod(0o700)
        return str(exe)

    def test_isolated_subprocess_structured_response_and_tool_rejection(self):
        self.user('Check delivery')
        worker = self.worker(codex=self.fake_codex())
        result = worker.run(allow_active=True)
        self.assertEqual(result['status'], 'applied')
        self.assertEqual(result['usage']['output_tokens'], 7)
        self.user('Another request')
        worker = self.worker(codex=self.fake_codex(tool=True))
        result = worker.run(allow_active=True)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('attempted a tool', result['error'])

    def test_install_does_not_change_existing_hooks_or_collector(self):
        home = self.root / 'home';home.mkdir()
        hook = home / '.codex/hooks.json';hook.parent.mkdir();hook.write_text('{"original":true}')
        with patch('pathlib.Path.home',return_value=home), patch('background.sys.platform','darwin'), patch('background.subprocess.run') as run:
            run.return_value.returncode = 1
            result = install(self.config, {'codex':'/test/codex'})
        self.assertEqual(hook.read_text(),'{"original":true}')
        self.assertTrue(Path(result['cli']).exists())
        import plistlib
        job=plistlib.loads(Path(result['plist']).read_bytes())
        self.assertEqual(job['StartInterval'],3600)
        self.assertNotIn('RunAtLoad',job)
        self.assertTrue(any('background.py' in x for x in job['ProgramArguments']))

    def test_timeout_terminates_child(self):
        exe = self.root / 'slow-codex'
        exe.write_text('#!' + sys.executable + '\nimport sys,time\nsys.stdin.read()\ntime.sleep(60)\n')
        exe.chmod(0o700)
        directory = self.root / 'run'; directory.mkdir()
        with patch('background.time.monotonic', side_effect=[0, 11]):
            with self.assertRaises(TimeoutError):
                self.worker(codex=str(exe), timeout_seconds=10).invoke('Synthetic task', directory)

    def test_project_filter_restricts_existing_sessions(self):
        self.user('Check delivery')
        self.memory.sync()
        config = {**self.config, 'include_projects':['different-project']}
        self.assertEqual(Background(config).run(allow_active=True)['reason'], 'no-idle-pending-session')

    def test_protected_learning_change_is_rejected_at_commit(self):
        self.user('Check delivery'); self.memory.sync()
        packet=self.memory.packet('session-0001'); r=packet['result_template']
        ref={'event_id':packet['events'][0]['id'],'quote':'Check delivery'}
        r.update(goal='Goal',summary='Summary',outcome='Pending',learning=[{
            'key':'existing-topic','kind':'topic','title':'Existing','body':'Original','project':'pilot',
            'evidence':[ref],'links':[],'contradictions':[],'base_revision':None}])
        self.memory.consolidate(r)
        self.user('A second request')
        def changed(prompt,directory):
            response,usage=self.response(prompt,directory)
            data=json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
            old=data['existing_learning'][0]
            response['learning']=[{**old,'body':'New candidate text'}]
            self.memory.learning.manage(old['key'],'pin',old['base_revision'])
            return response,usage
        result=self.worker().run(allow_active=True,invoke=changed)
        self.assertEqual(result['status'],'failed')
        self.assertIn('protected',result['error'])
        self.assertEqual(self.memory.learning.show('existing-topic')['body'],'Original')

    def test_idle_other_session_prevents_global_run(self):
        self.user('Check delivery');self.memory.sync()
        os.utime(self.log,(1,1))
        other=self.source/'other.jsonl'
        other.write_text(json.dumps({'type':'session_meta','payload':{'id':'other-session-0001',
            'cwd':'/other','timestamp':'2026-10-16T00:00:00Z'}})+'\n')
        self.assertEqual(self.worker().run()['reason'],'no-idle-pending-session')

    def test_auth_refresh_preserves_a_concurrent_login(self):
        auth_home=self.root/'auth-home';auth_home.mkdir();auth=auth_home/'auth.json'
        directory=self.root/'invoke';directory.mkdir()
        for concurrent in (False,True):
            auth.write_text('original-token-fixture')
            exe=self.fake_codex(refresh=True, concurrent_auth=auth if concurrent else None)
            with patch.dict(os.environ, {'CODEX_HOME':str(auth_home)}):
                self.worker(codex=exe).invoke('Synthetic packet',directory)
            self.assertEqual(auth.read_text(),'new-login-fixture' if concurrent else 'rotated-token-fixture')
