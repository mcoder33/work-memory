import base64
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from background import Background, schema
from skill_reflection import GithubCatalog, REPOSITORY, git_blob
from work_memory import Memory, digest, now


CONTROLS = ['factual-direct', 'authorized-execution', 'privacy', 'authority', 'no-recursive-delegation']


def catalog(files=None, commit='a' * 40):
    files = files or {'skills/dialogue-explore/SKILL.md': '# Synthetic dialogue skill\nversion: 1.0.0\n',
                      'skills/dialogue-explore/references/rules.md': 'Synthetic existing rule.\n'}
    blobs = {p: {'path': p, 'type': 'blob', 'mode': '100644', 'size': len(c.encode()),
                 'sha': git_blob(c.encode())} for p, c in files.items()}
    api = {'': {'full_name': REPOSITORY, 'default_branch': 'main'}, 'commits/main': {'sha': commit},
           'git/trees/' + commit + '?recursive=1': {'truncated': False, 'tree': list(blobs.values())}}
    api.update({'git/blobs/' + blobs[p]['sha']: {'sha': blobs[p]['sha'], 'encoding': 'base64',
                'content': base64.b64encode(content.encode()).decode()} for p, content in files.items()})
    client = GithubCatalog()
    client.api = lambda endpoint: copy.deepcopy(api[endpoint])
    return client, api


class ReflectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'vault').mkdir()
        self.config = {'vault': str(self.root / 'vault'), 'state': str(self.root / 'state'),
                       'sources': [], 'since': '2026-10-15'}
        self.memory = Memory(self.config)
        self.r = self.memory.reflection
        self.client, self.api = catalog()
        self.snapshot = self.client.fetch()
        self.r.refresh(lambda: self.snapshot)
        self.skill = self.snapshot['skills'][0]
        self.seq = 0

    def tearDown(self):
        self.memory.close()
        self.tmp.cleanup()

    def event(self, session, content=None, role='user', parent=None, project='pilot'):
        self.seq += 1
        content = content or ('Synthetic independent observation ' + str(self.seq))
        with self.memory.write():
            self.memory.db.execute('INSERT OR IGNORE INTO sessions(id,project,cwd,source,started,updated,forked_from) VALUES(?,?,?,?,?,?,?)',
                                   (session, project, '/synthetic', str(self.root / 'missing.jsonl'), now(), now(), parent))
            self.memory.db.execute('INSERT INTO events(id,session,role,text,timestamp,source,line) VALUES(?,?,?,?,?,?,?)',
                                   ('event-' + str(self.seq), session, role, content, now(), 'synthetic', self.seq))
        return {'event_id': 'event-' + str(self.seq), 'quote': content}

    def observation(self, refs=None):
        refs = refs or [self.event('origin-a'), self.event('origin-b')]
        return {'version': 1, 'skill': self.skill['id'], 'package_hash': self.r.skill(self.skill['id'])['package_hash'],
                'project': 'pilot', 'reason': 'Synthetic missing instruction example', 'cause': 'instruction-gap',
                'existing_rule_check': 'Existing synthetic rule inspected; proposed cause still needs review.',
                'evidence': refs, 'author': 'synthetic-author'}

    def proposal(self, observation=None):
        item = self.r.observe(observation or self.observation())
        return self.r.revise({'version': 1, 'id': item['id'], 'revision': item['revision'], 'author': 'synthetic-author',
            'files': {self.skill['path']: '# Synthetic candidate\nversion: 1.0.1\n'},
            'expected_effect': 'Expected synthetic behavior; not a real trial.',
            'criteria': {'holdouts': ['case-a', 'case-b'], 'controls': CONTROLS,
                         'success': 'Repair failed behavior or reduce measured duration with preserved success.',
                         'regressions': 'No increased errors, duration or tokens when measured; preserve controls.'},
            'correction': None})

    def evaluation(self, item, case, index, quote=None, metric_win=False, baseline_unknown=False):
        ref = self.event('trial-' + str(index), quote)
        control = case in CONTROLS
        return {'version': 1, 'id': item['id'], 'revision': item['revision'], 'kind': 'evaluation',
            'actor': 'synthetic-runner', 'evidence': [ref], 'case': case, 'lineage': 'trial-' + str(index),
            'environment': 'synthetic:model/effort/tools/dataset-v1',
            'baseline': {'passed': None if baseline_unknown else control or metric_win,
                         'errors': None, 'seconds': 20 if metric_win else None, 'tokens': None,
                         'observed': 'Synthetic baseline observation ' + str(index)},
            'candidate': {'passed': True, 'errors': None, 'seconds': 10 if metric_win else None, 'tokens': None,
                          'observed': 'Synthetic candidate observation ' + str(index)},
            'outcome': 'unchanged' if control else 'improved'}

    def evaluate_all(self, item, **options):
        for i, case in enumerate(['case-a', 'case-b'] + CONTROLS):
            self.r.record(self.evaluation(item, case, i, **options))

    def review(self, item):
        return {'version': 1, 'id': item['id'], 'revision': item['revision'], 'kind': 'review',
                'actor': 'synthetic-reviewer', 'evidence': [self.event('review-session')],
                'verdict': 'accept', 'reason': 'Synthetic technical test review only.',
                'independent': True, 'candidate_hash': item['candidate_hash']}

    def bind(self, skill=None):
        skill = skill or self.skill
        folder = self.root / 'installed' / skill['name']
        prefix = str(Path(skill['path']).parent) + '/'
        for path, entry in skill['files'].items():
            local = folder / path.removeprefix(prefix)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(self.snapshot['blobs'][entry['git_sha']])
        self.config['skill_reflection'] = {'installed': {skill['id']: str(folder)}}
        return folder

    def test_complete_dynamic_new_changed_removed_and_dependency_versions(self):
        before = self.skill['package_hash']
        changed, _ = catalog({'skills/dialogue-explore/SKILL.md': self.skill['body'],
                              'skills/dialogue-explore/references/rules.md': 'Updated dependency',
                              'future/SKILL.md': '# Future dynamic skill'})
        snapshot = changed.fetch()
        result = self.r.refresh(lambda: snapshot)
        self.assertEqual(len(result['changes']['new']), 1)
        self.assertEqual(len(result['changes']['changed']), 1)
        self.assertNotEqual(before, self.r.skill(self.skill['id'])['package_hash'])
        removed, _ = catalog({'future/SKILL.md': '# Future dynamic skill'})
        self.r.refresh(removed.fetch)
        self.assertTrue(self.r.skill(self.skill['id'])['retired'])
        self.assertEqual(self.memory.db.execute('SELECT COUNT(*) FROM reflection_snapshots').fetchone()[0], 3)

    def test_external_repository_dependency_invalidates_packages(self):
        client, _ = catalog({'skills/dialogue-explore/SKILL.md': self.skill['body'],
                             'shared/rule.md': 'An external shared rule'})
        changed = client.fetch()['skills'][0]
        self.assertNotEqual(self.skill['package_hash'], changed['package_hash'])

    def test_truncated_network_and_corrupt_responses_keep_old_active_catalog(self):
        endpoint = 'git/trees/' + 'a' * 40 + '?recursive=1'
        self.api[endpoint]['truncated'] = True
        result = self.r.refresh(self.client.fetch)
        self.assertEqual(result['status'], 'stale')
        self.assertFalse(self.r.skill(self.skill['id'])['retired'])
        self.assertEqual(self.r.run_pass(fetch=self.client.fetch)['analyses'], [])
        self.assertEqual(self.r.refresh(lambda: (_ for _ in ()).throw(OSError('offline')))['status'], 'stale')
        bad = copy.deepcopy(self.snapshot)
        bad['blobs'][next(iter(bad['blobs']))] = b'corrupt'
        self.assertEqual(self.r.refresh(lambda: bad)['status'], 'stale')
        self.assertFalse(self.r.skill(self.skill['id'])['retired'])

    def test_unsafe_symlink_tree_and_blob_mismatch_fail_closed(self):
        endpoint = 'git/trees/' + 'a' * 40 + '?recursive=1'
        self.api[endpoint]['tree'][0]['mode'] = '120000'
        self.assertEqual(self.r.refresh(self.client.fetch)['status'], 'stale')
        self.api[endpoint]['tree'][0]['mode'] = '100644'
        oid = self.api[endpoint]['tree'][0]['sha']
        self.api['git/blobs/' + oid]['content'] = base64.b64encode(b'bad bytes').decode()
        self.assertEqual(self.r.refresh(self.client.fetch)['status'], 'stale')

    def test_installed_identity_is_explicit_and_drift_not_overwritten(self):
        self.assertEqual(self.r.installed(self.skill)['status'], 'unknown')
        folder = self.bind()
        self.assertEqual(self.r.installed(self.skill)['status'], 'matches')
        (folder / 'references/rules.md').write_text('Manual local edit')
        self.assertEqual(self.r.installed(self.skill)['status'], 'local-drift')
        (folder / 'references/rules.md').unlink()
        self.assertEqual(self.r.installed(self.skill)['status'], 'local-drift')
        self.config['skill_reflection']['installed'][self.skill['id']] = str(self.root / 'absent')
        self.assertEqual(self.r.installed(self.skill)['status'], 'not-installed')

    def test_queue_is_persistent_bounded_and_fair_under_repeated_priority(self):
        files = {'skill-' + str(i) + '/SKILL.md': '# Dynamic skill ' + str(i) for i in range(6)}
        client, _ = catalog(files)
        snapshot = client.fetch()
        self.r.refresh(lambda: snapshot)
        visited = set()
        for i in range(14):
            with self.memory.write():
                self.memory.db.execute('UPDATE reflection_skills SET priority=2 WHERE id LIKE ?', (REPOSITORY + ':skill-0/%',))
            result = self.r.run_pass(1, fetch=lambda: snapshot)
            self.assertEqual(len(result['analyses']), 1)
            self.assertEqual(result['analyses'][0]['status'], 'no-evidence')
            visited.add(result['analyses'][0]['skill'])
        self.assertEqual(visited, {s['id'] for s in snapshot['skills']})
        self.memory.close()
        self.memory = Memory(self.config)
        self.r = self.memory.reflection
        self.assertEqual(self.r.status()['passes'], 14)
        with self.assertRaises(ValueError):self.r.run_pass(0)

    def test_exact_evidence_scopes_dedup_and_fork_lineage(self):
        original = self.event('parent', 'Synthetic original report')
        fork = self.event('child', 'Synthetic child continuation', parent='parent')
        item = self.r.observe(self.observation([original, fork, original]))
        self.assertEqual(item['episodes'], ['session:parent'])
        self.assertEqual(len(item['evidence']), 2)
        self.assertEqual(self.r.observe(self.observation([original, fork]))['id'], item['id'])
        with self.assertRaises(ValueError):self.proposal(self.observation([original, fork]))
        copied = self.event('other', original['quote'])
        item = self.r.observe(self.observation([original, copied]))
        self.assertEqual(len(item['episodes']), 1)
        foreign = self.event('foreign', project='other')
        with self.assertRaises(ValueError):self.r.observe(self.observation([foreign]))
        bad = {'event_id': original['event_id'], 'quote': 'not the actual quotation'}
        with self.assertRaises(ValueError):self.r.observe(self.observation([bad]))

    def test_documents_cannot_claim_independent_episodes(self):
        a = self.memory.learning.source('pilot', 'Synthetic source', 'synthetic:one', 'Synthetic source observation A')
        b = self.memory.learning.source('pilot', 'Synthetic source', 'synthetic:two', 'Synthetic source observation B')
        refs = [{'source_id': a['source_id'], 'quote': 'Synthetic source observation A'},
                {'source_id': b['source_id'], 'quote': 'Synthetic source observation B'}]
        item = self.r.observe(self.observation(refs))
        self.assertEqual(item['episodes'], ['source:unknown'])
        with self.assertRaises(ValueError):self.proposal(self.observation(refs))

    def test_revision_invalidation_preserves_history_and_old_results(self):
        item = self.proposal()
        self.r.record(self.evaluation(item, 'case-a', 1))
        client, _ = catalog({'skills/dialogue-explore/SKILL.md': self.skill['body'],
                             'skills/dialogue-explore/references/rules.md': 'New upstream rule'})
        self.r.refresh(client.fetch)
        self.assertEqual(self.r.show(item['id'])['state'], 'needs-review')
        with self.assertRaises(ValueError):self.r.record(self.evaluation(item, 'case-b', 2))
        new = self.r.revise({'version': 1, 'id': item['id'], 'revision': item['revision'], 'author': item['author'],
                             'files': item['files'], 'criteria': item['criteria'], 'expected_effect': item['expected_effect'],
                             'correction': None})
        self.assertNotEqual(item['revision'], new['revision'])
        self.assertEqual(len(new['history']), 2)
        self.assertFalse(self.r.evaluations_pass(new, [r for r in new['records'] if r['revision'] == new['revision']]))
        with self.assertRaises(ValueError):self.r.record(self.evaluation(item, 'case-b', 3))

    def test_actual_results_unknown_metrics_and_independent_review(self):
        item = self.proposal()
        self.evaluate_all(item)
        bad = self.review(item)
        bad['actor'] = 'synthetic-author'
        with self.assertRaises(ValueError):self.r.record(bad)
        accepted = self.r.record(self.review(item))
        self.assertEqual(accepted['state'], 'reviewed')
        self.assertTrue(all(r['baseline']['seconds'] is None for r in accepted['records'] if r['kind'] == 'evaluation'))

    def test_metric_improvement_with_preserved_success_is_eligible(self):
        item = self.proposal()
        self.evaluate_all(item, metric_win=True)
        self.assertEqual(self.r.record(self.review(item))['state'], 'reviewed')

    def test_copied_trials_unknown_controls_and_regression_never_certify(self):
        item = self.proposal()
        first = self.evaluation(item, 'case-a', 1, quote='A copied synthetic trial')
        self.r.record(first)
        with self.assertRaises(ValueError):
            self.r.record(self.evaluation(item, 'case-b', 2, quote='A copied synthetic trial'))
        # Unknown baseline in a control cannot certify safety.
        self.r.record(self.evaluation(item, 'case-b', 3))
        for i, case in enumerate(CONTROLS, 4):
            self.r.record(self.evaluation(item, case, i, baseline_unknown=True))
        with self.assertRaises(ValueError):self.r.record(self.review(item))
        records = [r for r in self.r.show(item['id'])['records'] if r['kind'] == 'evaluation']
        self.assertFalse(self.r.evaluations_pass(item, records))

    def test_invalid_measurements_and_regression_state(self):
        item = self.proposal()
        raw = self.evaluation(item, 'case-a', 1)
        raw['candidate']['seconds'] = -1
        with self.assertRaises(ValueError):self.r.record(raw)
        raw['candidate']['seconds'] = float('nan')
        with self.assertRaises(ValueError):self.r.record(raw)
        raw['candidate']['seconds'] = None
        raw['outcome'] = 'regressed'
        self.assertEqual(self.r.record(raw)['state'], 'needs-review')

    def test_manual_no_change_and_conflict_protected_notes(self):
        item = self.r.observe(self.observation())
        path = self.root / 'vault/Память/Изменения навыков' / (item['id'] + '.md')
        path.write_text(path.read_text().replace('Synthetic missing instruction example', 'Human edit'))
        result = {'version': 1, 'id': item['id'], 'revision': item['revision'], 'kind': 'decision',
                  'actor': 'synthetic-analyst', 'evidence': item['evidence'], 'outcome': 'no-change',
                  'reason': 'The existing rule is adequate; synthetic no-change decision.'}
        self.r.record(result)
        self.assertIn('Human edit', path.read_text())
        self.assertEqual(self.r.show(item['id'])['state'], 'no-change')
        self.assertTrue(any(i['key'].startswith('conflict:') for i in self.memory.status()['issues']))
        self.assertEqual(self.memory.context('instruction', 'pilot')['skill_reflection']['candidates'][0]['id'], item['id'])

    def test_observation_snapshot_mismatch_is_not_rebound(self):
        raw = self.observation()
        raw['package_hash'] = 'old'
        with self.assertRaises(ValueError):self.r.observe(raw)

    def test_background_refreshes_without_pending_session_and_no_inference(self):
        self.config['skill_reflection'] = {'enabled': True}
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(invoke=lambda *_: self.fail('No inference expected'))
        self.assertEqual(result['reason'], 'no-idle-pending-session')
        self.assertEqual(len(result['skill_reflection']['analyses']), 1)
        self.assertEqual(result['skill_reflection']['analyses'][0]['status'], 'no-evidence')

    def test_background_versions_are_strict_and_tools_remain_disabled(self):
        self.assertNotIn('skill_observations', schema()['properties'])
        self.assertEqual(schema(2)['properties']['schema_version']['enum'], [2])
        self.assertFalse(schema(2)['additionalProperties'])
        self.assertEqual(schema(3)['properties']['schema_version']['enum'], [3])
        with self.assertRaises(ValueError):schema(4)

    def test_concurrent_catalog_refresh_does_not_overwrite_new_head(self):
        other = Memory(self.config)
        client, _ = catalog({'future/SKILL.md': '# Future'}, commit='b' * 40)
        snapshot = client.fetch()
        def fetched():
            other.reflection.refresh(lambda: snapshot)
            return self.snapshot
        try:
            result = self.r.refresh(fetched)
            self.assertTrue(result['superseded_refresh'])
            self.assertEqual(self.r.meta()['commit'], 'b' * 40)
        finally:other.close()

    def test_handoff_scope_and_authority_are_gated(self):
        item = self.proposal()
        raw = {'version': 1, 'id': item['id'], 'revision': item['revision'], 'authority': self.event('authority')}
        with self.assertRaises(ValueError):self.r.handoff(raw)
        self.evaluate_all(item)
        self.r.record(self.review(item))
        self.bind()
        with self.assertRaises(ValueError):self.r.handoff(raw)
        self.assertFalse((self.root / 'state/skill-handoffs').exists())


    def helper_ready(self):
        item = self.proposal()
        self.evaluate_all(item)
        self.r.record(self.review(item))
        # Synthetic HOME makes the adapter use an isolated helper target/store.
        home = self.root / 'home'
        folder = home / '.codex/skills/dialogue-explore'
        prefix = str(Path(self.skill['path']).parent) + '/'
        for path, entry in self.skill['files'].items():
            target = folder / path.removeprefix(prefix)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.snapshot['blobs'][entry['git_sha']])
        self.config['skill_reflection'] = {'installed': {self.skill['id']: str(folder)}}
        authority = self.event('authority', 'Apply exact synthetic candidate ' + item['id'] + ' revision ' + item['revision'])
        with patch('pathlib.Path.home', return_value=home):
            handoff = self.r.handoff({'version': 1, 'id': item['id'], 'revision': item['revision'], 'authority': authority})
        helper_id = 'b' * 32
        root = home / '.local/state/codex-dialogue-kit/retrospective'
        candidate_dir = root / 'candidates' / helper_id
        candidate_dir.mkdir(parents=True)
        analysis = {'analyst_id': 'native-analyst'}
        analysis_bytes = json.dumps(analysis).encode()
        native = {'manifest.json': {'id': helper_id, 'status': 'applied',
                    'baseline': {'target': handoff['target'], 'sha256': handoff['baseline_sha256']},
                    'candidate_sha256': handoff['candidate_sha256'], 'analysis_sha256': hashlib.sha256(analysis_bytes).hexdigest()},
                  'analysis.json': analysis,
                  'validation.json': {'runner_id': 'native-runner', 'syntax_passed': True,
                    'baseline_sha256': handoff['baseline_sha256'], 'candidate_sha256': handoff['candidate_sha256']},
                  'review.json': {'reviewer_id': 'native-reviewer', 'analyst_id': 'native-analyst', 'independent': True,
                    'verdict': 'accept', 'candidate_sha256': handoff['candidate_sha256'],
                    'boundaries_preserved': {k: True for k in ('permissions', 'root-only-delegation', 'privacy', 'scope')}},
                  'post-apply.json': {'passed': True, 'candidate_sha256': handoff['candidate_sha256']}}
        for name, payload in native.items():
            (candidate_dir / name).write_text(json.dumps(payload))
        content = next(iter(item['files'].values()))
        (candidate_dir / 'candidate.md').write_text(content)
        (root / 'versions').mkdir()
        backup = root / 'versions' / (handoff['baseline_sha256'] + '.md')
        backup.write_bytes(self.snapshot['blobs'][self.skill['files'][self.skill['path']]['git_sha']])
        (folder / 'SKILL.md').write_text(content)
        receipt = {'version': 1, 'id': item['id'], 'revision': item['revision'], 'helper_id': helper_id}
        return home, folder, candidate_dir, backup, receipt

    def test_helper_sidecar_receipt_monitoring_and_rollback_provenance(self):
        home, installed, folder, backup, receipt = self.helper_ready()
        with patch('pathlib.Path.home', return_value=home):
            item = self.r.receipt(receipt)
            self.assertEqual(item['state'], 'monitoring')
            self.assertEqual(item['records'][-1]['kind'], 'application')
            applied_hash = self.r.installed(self.skill)['hash']
            ref = self.event('followup-real')
            result = {'version': 1, 'id': item['id'], 'revision': item['revision'], 'kind': 'followup',
                      'actor': 'followup-observer', 'evidence': [ref], 'lineage': 'followup-real',
                      'outcome': 'unknown', 'reason': 'Technical fixture; no behavioral usefulness claim.',
                      'installed_hash': applied_hash}
            self.assertEqual(self.r.record(result)['state'], 'monitoring')
            manifest = json.loads((folder / 'manifest.json').read_text())
            manifest['status'] = 'rolled-back'
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            (installed / 'SKILL.md').write_bytes(backup.read_bytes())
            self.assertEqual(self.r.receipt(receipt)['state'], 'needs-review')

    def test_helper_receipt_keeps_needs_review_after_dependency_changed(self):
        home, installed, folder, backup, receipt = self.helper_ready()
        client, _ = catalog({'skills/dialogue-explore/SKILL.md': self.skill['body'],
                             'skills/dialogue-explore/references/rules.md': 'Updated upstream dependency'})
        self.r.refresh(client.fetch)
        with patch('pathlib.Path.home', return_value=home):
            item = self.r.receipt(receipt)
        self.assertEqual(item['state'], 'needs-review')
        self.assertTrue(any(r['kind'] == 'application' for r in item['records']))

    def test_helper_receipt_refuses_corrupt_backup_pending_apply_and_late_edits(self):
        home, installed, folder, backup, receipt = self.helper_ready()
        with patch('pathlib.Path.home', return_value=home):
            original = backup.read_bytes()
            backup.write_bytes(b'corrupted backup')
            with self.assertRaises(ValueError):self.r.receipt(receipt)
            backup.write_bytes(original)
            manifest_path = folder / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['status'] = 'applied-pending-verification'
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(ValueError):self.r.receipt(receipt)
            manifest['status'] = 'applied'
            manifest_path.write_text(json.dumps(manifest))
            (installed / 'references/rules.md').write_text('A later manual edit')
            with self.assertRaises(ValueError):self.r.receipt(receipt)
        self.assertEqual(self.r.show(receipt['id'])['state'], 'ready')
        self.assertEqual((installed / 'references/rules.md').read_text(), 'A later manual edit')

    def test_reflection_note_publication_recovers_after_interruption(self):
        raw = self.observation()
        original = self.memory.replay_outbox
        attempts = 0
        def fail_after_commit():
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                raise OSError('Synthetic interruption after commit')
            return original()
        with patch.object(self.memory, 'replay_outbox', side_effect=fail_after_commit):
            with self.assertRaises(OSError):self.r.observe(raw)
        self.memory.close()
        self.memory = Memory(self.config)
        self.r = self.memory.reflection
        self.assertEqual(self.memory.status()['pending_writes'], 0)
        self.assertEqual(len(self.r.catalog('pilot')['candidates']), 1)
        item = self.r.catalog('pilot')['candidates'][0]
        self.assertTrue((self.root / 'vault' / item['path']).is_file())

    def test_background_v2_observation_and_summary_commit_together(self):
        # Use a genuine synthetic ingest log, so Background source race guards apply.
        log = self.root / 'sessions/synthetic.jsonl'
        log.parent.mkdir()
        log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'background-session', 'cwd': '/projects/pilot',
            'source': 'vscode', 'timestamp': now()}, 'timestamp': now()}) + '\n' +
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Review dialogue-explore synthetic behavior'}]}, 'timestamp': now()}) + '\n')
        self.config.update(sources=[str(log.parent)], projects={'/projects/pilot': 'pilot'}, skill_reflection={'enabled': True})
        def respond(prompt, directory):
            data = json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
            event = data['events'][0]
            return {'schema_version': 2, 'goal': 'Synthetic review', 'summary': 'Synthetic unverified observation',
                    'outcome': 'No installed changes', 'next_steps': [], 'knowledge': [], 'learning': [],
                    'skill_observations': [{'skill': self.skill['id'], 'reason': 'Possible synthetic issue, not verified.',
                        'cause': 'unknown', 'existing_rule_check': 'Package supplied; cause unknown.',
                        'evidence': [{'event_id': event['id'], 'source_id': None, 'quote': event['text']}]}]}, None
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(allow_active=True, invoke=respond)
        self.assertEqual(result['status'], 'applied', result)
        item = self.r.catalog('pilot')['candidates'][0]
        self.assertEqual(item['state'], 'observed')
        self.assertTrue(self.r.show(item['id'])['background_proposal'])
        self.assertFalse(self.memory.packet('background-session')['pending'])

    def test_versioned_extension_rollback_on_bad_evidence(self):
        # Use existing synthetic ingest helper for a valid summary packet.
        log = self.root / 'sessions/synthetic.jsonl'
        log.parent.mkdir()
        log.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'bad-extension', 'cwd': '/projects/pilot',
            'source': 'vscode', 'timestamp': now()}, 'timestamp': now()}) + '\n' +
            json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': 'Synthetic request'}]}, 'timestamp': now()}) + '\n')
        self.config.update(sources=[str(log.parent)], projects={'/projects/pilot': 'pilot'})
        self.memory.sync()
        result = self.memory.packet('bad-extension')['result_template']
        result.update(goal='Synthetic goal', summary='Must roll back', outcome='Unverified')
        raw = self.observation()
        raw['evidence'] = [{'event_id': 'missing', 'quote': 'Missing evidence'}]
        result['skill_reflection'] = {'version': 1, 'observations': [raw]}
        with self.assertRaises(ValueError):self.memory.consolidate(result)
        self.assertTrue(self.memory.packet('bad-extension')['pending'])
        self.assertEqual(self.r.catalog('pilot')['candidates'], [])


    def historic_reflection_response(self, prompt, directory):
        data = json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
        self.assertIn('skills/dialogue-explore/references/rules.md', data['skill_packages'][0]['files'])
        ref = data['evidence'][0]
        return {'schema_version': 3, 'outcome': 'observations', 'reason': 'Synthetic diagnosis remains unknown.',
                'skill_observations': [{'skill': self.skill['id'], 'reason': 'Possible synthetic historic issue.',
                    'cause': 'unknown', 'existing_rule_check': 'Synthetic existing rules supplied; cause still unknown.',
                    'evidence': [{'event_id': ref['event_id'], 'source_id': None, 'quote': ref['quote']}]}]}, None

    def historic_setup(self):
        self.event('processed-session', 'A synthetic dialogue-explore issue for historical reflection')
        with self.memory.write():
            self.memory.db.execute('UPDATE sessions SET through_seq=(SELECT MAX(seq) FROM events)')
        self.config['skill_reflection'] = {'enabled': True}

    def test_historic_reflection_without_pending_session_has_guarded_receipt(self):
        self.historic_setup()
        self.assertFalse(self.memory.packet()['pending'])
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(allow_active=True, invoke=self.historic_reflection_response)
        self.assertEqual(result['status'], 'applied', result)
        self.assertEqual(result['mode'], 'skill-reflection')
        self.assertEqual(result['receipt']['observations'], 1)
        self.assertEqual(self.r.pending_work(), [])
        self.assertEqual(self.r.catalog('pilot')['candidates'][0]['state'], 'observed')
        self.assertFalse(self.memory.packet()['pending'])
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            repeated = Background(self.config).run(allow_active=True, invoke=lambda *_: self.fail('Completed evidence must not be re-analyzed'))
        self.assertEqual(repeated['status'], 'skipped')

    def test_historic_no_change_is_reasoned_receipt_without_candidate(self):
        self.historic_setup()
        def no_change(prompt, directory):
            return {'schema_version': 3, 'outcome': 'no-change', 'reason': 'Synthetic existing instruction already covers it.',
                    'skill_observations': []}, None
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(allow_active=True, invoke=no_change)
        self.assertEqual(result['receipt']['outcome'], 'no-change')
        self.assertEqual(self.r.pending_work(), [])
        self.assertEqual(self.r.catalog('pilot')['candidates'], [])

    def test_historic_failure_keeps_work_pending_and_consumes_daily_attempt(self):
        self.historic_setup()
        def bad(prompt, directory):
            result, usage = self.historic_reflection_response(prompt, directory)
            result['skill_observations'][0]['evidence'][0]['quote'] = 'Fabricated quotation'
            return result, usage
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config, {'max_runs_per_day': 1}).run(allow_active=True, invoke=bad)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(len(self.r.pending_work()), 1)
            self.assertEqual(self.r.catalog('pilot')['candidates'], [])
            repeated = Background(self.config, {'max_runs_per_day': 1}).run(allow_active=True,
                        invoke=lambda *_: self.fail('Daily cap must include reflection inference'))
            self.assertEqual(repeated['reason'], 'daily-limit')
            self.assertEqual(repeated['skill_reflection']['catalog']['status'], 'fresh')

    def test_historic_source_or_catalog_change_invalidates_reasoning(self):
        self.historic_setup()
        def changed(prompt, directory):
            result = self.historic_reflection_response(prompt, directory)
            (self.root / 'missing.jsonl').write_text('Synthetic resumed source')
            return result
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(allow_active=True, invoke=changed)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('Source session changed', result['error'])
        self.assertEqual(len(self.r.pending_work()), 1)
        self.assertEqual(self.r.catalog('pilot')['candidates'], [])

    def test_package_budget_reports_omissions_without_truncated_instructions(self):
        client, _ = catalog({'skills/dialogue-explore/SKILL.md': '# Small synthetic skill',
                             'skills/dialogue-explore/references/rules.md': 'Large synthetic rule. ' * 200})
        self.r.refresh(client.fetch)
        package = self.r.package(self.skill['id'], chars=1000)
        self.assertEqual(package['files']['skills/dialogue-explore/SKILL.md'], '# Small synthetic skill')
        self.assertEqual(package['omitted'], ['skills/dialogue-explore/references/rules.md'])


    def test_system_injected_and_legacy_invalid_work_do_not_reach_model(self):
        self.event('system-session', 'dialogue-explore system metadata', role='system')
        self.event('injected-session', '# AGENTS.md instructions dialogue-explore')
        result = self.r.run_pass(fetch=lambda: self.snapshot)
        self.assertEqual(result['analyses'][0]['status'], 'no-evidence')
        self.assertEqual(self.r.pending_work(), [])
        bad_ref = {'event_id': 'event-1', 'quote': 'dialogue-explore system metadata', 'project': 'pilot', 'episode': 'system-session'}
        legacy = {'skill': self.skill['id'], 'package_hash': self.skill['package_hash'], 'work_id': 'legacy-invalid',
                  'evidence': [bad_ref], 'historical_use': 'unknown'}
        with self.memory.write():
            self.memory.db.execute('INSERT INTO reflection_work VALUES(?,?,?,?,?,?)',
                ('legacy-invalid', self.skill['id'], self.skill['package_hash'], json.dumps(legacy), 'pending-analysis', now()))
        self.assertEqual(self.r.pending_work(), [])
        self.assertEqual(self.memory.db.execute("SELECT status FROM reflection_work WHERE id='legacy-invalid'").fetchone()[0], 'invalid-evidence')

    def test_historic_reflection_obeys_idle_and_project_filter(self):
        self.historic_setup()
        source = self.root / 'missing.jsonl'
        source.write_text('Synthetic recently active journal')
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(invoke=lambda *_: self.fail('Active chat must block reflection inference'))
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(len(self.r.pending_work()), 1)
        self.config['include_projects'] = ['another-project']
        self.assertEqual(self.r.pending_work(), [])
        self.assertEqual(self.r.relevant_evidence(self.skill), [])

    def test_installed_executable_bit_is_part_of_drift_identity(self):
        folder = self.bind()
        path = folder / 'SKILL.md'
        before = self.r.installed(self.skill)['hash']
        path.chmod(0o755)
        installed = self.r.installed(self.skill)
        self.assertEqual(installed['status'], 'local-drift')
        self.assertNotEqual(installed['hash'], before)

    def test_historic_budget_compacts_evidence_and_reduces_batch(self):
        files = {'first/SKILL.md': '# first ' + ('synthetic content ' * 280),
                 'second/SKILL.md': '# second ' + ('synthetic content ' * 280)}
        client, _ = catalog(files)
        snapshot = client.fetch()
        self.r.refresh(lambda: snapshot)
        for index in range(6):
            self.event('long-' + str(index), 'first second ' + str(index) + ' ' + ('q' * 1380))
        with self.memory.write():
            self.memory.db.execute('UPDATE sessions SET through_seq=(SELECT MAX(seq) FROM events)')
        self.config['skill_reflection'] = {'enabled': True}
        observed = []
        def response(prompt, directory):
            payload = json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
            self.assertTrue(all('evidence' not in work for work in payload['work']))
            self.assertLess(len(json.dumps(payload)), 40000)
            observed.extend(payload['work'])
            return {'schema_version': 3, 'outcome': 'insufficient-data', 'reason': 'Synthetic budget smoke only.',
                    'skill_observations': []}, None
        with patch('skill_reflection.GithubCatalog.fetch', return_value=snapshot):
            result = Background(self.config).run(allow_active=True, invoke=response)
        self.assertEqual(result['status'], 'applied', result)
        self.assertTrue(observed)

    def test_oversize_single_work_is_deferred_and_smaller_work_proceeds(self):
        long_path = ('nested' * 40 + '/') * 12 + 'huge/SKILL.md'
        client, _ = catalog({long_path: '# Synthetic ' + 'content ' * 100,
                             'small/SKILL.md': '# Small synthetic instruction'})
        snapshot = client.fetch()
        self.r.refresh(lambda: snapshot)
        for index in range(6):
            self.event('budget-episode-' + str(index), 'huge small ' + str(index) + ' ' + 'q' * 1470)
        self.r.run_pass(2, fetch=lambda: snapshot)
        work = self.r.pending_work(limit=20)
        oversize = next(item for item in work if ':small/' not in item['skill'])
        with self.memory.write():
            self.memory.db.execute("UPDATE reflection_work SET updated='2000' WHERE id=?", (oversize['work_id'],))
        self.config['skill_reflection'] = {'enabled': True}
        worker = Background(self.config, {'input_chars': 12000})
        def response(prompt, directory):
            return {'schema_version': 3, 'outcome': 'no-change', 'reason': 'Synthetic bounded candidate selection.',
                    'skill_observations': []}, None
        result = worker.reflect(self.memory, {}, self.r.pending_work(limit=20), response)
        self.assertEqual(result['status'], 'applied', result)
        state = self.memory.db.execute('SELECT status FROM reflection_work WHERE id=?', (oversize['work_id'],)).fetchone()[0]
        self.assertEqual(state, 'deferred-budget')
        self.assertEqual(self.r.retry_work(oversize['work_id'])['status'], 'pending-analysis')

    def test_historic_source_snapshots_feed_proposals_without_claiming_independence(self):
        self.memory.learning.source('pilot', 'Synthetic skill observation', 'synthetic:reflection',
                                    'A synthetic dialogue-explore observation in a retained source snapshot.')
        self.config['skill_reflection'] = {'enabled': True}
        def response(prompt, directory):
            data = json.loads(prompt.split('BEGIN_UNTRUSTED_PACKET\n')[1].split('\nEND_UNTRUSTED_PACKET')[0])
            ref = data['evidence'][0]
            return {'schema_version': 3, 'outcome': 'observations', 'reason': 'Synthetic source-only diagnostic input.',
                    'skill_observations': [{'skill': self.skill['id'], 'reason': 'Synthetic source-based lead.',
                        'cause': 'unknown', 'existing_rule_check': 'Cause and historical skill use remain unknown.',
                        'evidence': [{'event_id': None, 'source_id': ref['source_id'], 'quote': ref['quote']}]}]}, None
        with patch('skill_reflection.GithubCatalog.fetch', return_value=self.snapshot):
            result = Background(self.config).run(allow_active=True, invoke=response)
        self.assertEqual(result['status'], 'applied', result)
        item = self.r.show(self.r.catalog('pilot')['candidates'][0]['id'])
        self.assertEqual(item['episodes'], ['source:unknown'])
        self.assertFalse(self.memory.packet()['pending'])


class GithubTransportTest(unittest.TestCase):
    def test_initial_endpoint_has_no_trailing_slash_and_absolute_gh(self):
        calls = []
        def run(argv, stdout, stderr, timeout):
            calls.append(argv)
            stdout.write(b'{"full_name":"mcoder33/codex-skills"}')
            return subprocess.CompletedProcess(argv, 0)
        with patch('skill_reflection.subprocess.run', side_effect=run):
            GithubCatalog(executable='/synthetic/gh').api('')
        self.assertEqual(calls[0], ['/synthetic/gh', 'api', 'repos/mcoder33/codex-skills'])
        with self.assertRaises(ValueError):GithubCatalog(executable='gh')


if __name__ == '__main__':
    unittest.main()
