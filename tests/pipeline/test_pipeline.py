"""Behavioral regression tests for scope, state, review seals and real Git isolation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from policy import PipelineError, check_scope, safe_path, snapshot, scopes_overlap, validate_spec, git
from executors import apply_proposal, build_context, config
from task import Pipeline, lock, write_json
from accept_bootstrap import accept


def spec(task_id='POC-100'):
    return {'id': task_id, 'title': 'Test isolated edit', 'type': 'implementation',
            'status': 'READY', 'priority': 'high', 'executor': {'preferred': 'local', 'fallback': 'cloud'},
            'reviewer': 'cloud', 'depends_on': [], 'allowed_paths': ['apps/api/**'],
            'forbidden_paths': ['apps/api/private/**'], 'context': [],
            'requirements': ['Write one file'], 'acceptance': ['Checks pass'],
            'validation': [{'name': 'compile', 'argv': ['{python}', '-c',
                "compile(open('apps/api/example.py').read(), 'example.py', 'exec')"]}],
            'risk': 'low', 'retry_limit': 1}


class PolicyTests(unittest.TestCase):
    def test_autonomous_execution_policy_is_documented(self):
        root = Path(__file__).resolve().parents[2]
        agents = (root / 'AGENTS.md').read_text(encoding='utf-8')
        pipeline = (root / 'docs/development-pipeline.md').read_text(encoding='utf-8')
        lifecycle = (root / 'docs/task-lifecycle.md').read_text(encoding='utf-8')
        self.assertIn('Autonomous Execution Policy', agents)
        self.assertIn('pre-authorizes its complete safe, reversible lifecycle', agents)
        self.assertIn('diagnose → fix → validate again → review again', pipeline)
        self.assertIn('`drive ID` performs only deterministic controller work', lifecycle)

    def test_scope_and_forbidden_override(self):
        check_scope(spec(), ['apps/api/health.py'])
        for path in ['apps/web/page.py', 'apps/api/private/token.py', 'apps/api/../../secret',
                     'C:/secret', 'apps/api/.env', 'apps/api/.git/config', 'apps/api/CON.txt',
                     'apps/api/a.', 'apps\\api\\a.py']:
            with self.subTest(path=path), self.assertRaises(PipelineError):
                check_scope(spec(), [path])

    def test_shared_requires_explicit_exception(self):
        task = spec()
        task['allowed_paths'] = ['packages/contracts/**']
        with self.assertRaises(PipelineError):
            check_scope(task, ['packages/contracts/api.json'])
        task['shared_paths_approval'] = {'paths': ['packages/contracts/**'], 'reviewer': 'cloud:lead', 'reason': 'Dedicated schema task'}
        check_scope(task, ['packages/contracts/api.json'])

    def test_synthetic_environment_template_requires_explicit_scope(self):
        task = spec()
        task['allowed_paths'] = ['.env.example']
        task['forbidden_paths'] = []
        check_scope(task, ['.env.example'])

        for path in ['.env', '.env.local', 'config/.env', 'config/.env.example']:
            with self.subTest(path=path), self.assertRaises(PipelineError):
                check_scope(task, [path])

        task['allowed_paths'] = ['apps/api/**']
        with self.assertRaises(PipelineError):
            check_scope(task, ['.env.example'])

    def test_overlap_conservative(self):
        self.assertTrue(scopes_overlap(['apps/**'], ['apps/api/a.py']))
        self.assertTrue(scopes_overlap(['apps/w*'], ['apps/web/**']))
        self.assertFalse(scopes_overlap(['apps/api/**'], ['apps/web/**']))

    def test_schema_checks(self):
        validate_spec(spec())
        for key, value in [('id', '../bad'), ('retry_limit', -1), ('validation', []),
                           ('allowed_paths', []), ('status', 'DONE')]:
            task = spec()
            task[key] = value
            with self.subTest(key=key), self.assertRaises(PipelineError):
                validate_spec(task)

    def test_proposal_preflight_no_partial_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            proposal = {'handoff': {}, 'files': [
                {'path': 'apps/api/good.py', 'content': 'valid = True\n'},
                {'path': 'outside.py', 'content': 'bad'}]}
            with self.assertRaises(PipelineError):
                apply_proposal(spec(), folder, proposal)
            self.assertFalse((Path(folder) / 'apps').exists())

    def test_duplicate_case_and_blocker(self):
        with tempfile.TemporaryDirectory() as folder:
            for proposal in [
                {'handoff': {}, 'files': [{'path': 'apps/api/A.py', 'content': ''},
                                           {'path': 'apps/api/a.py', 'content': ''}]},
                {'handoff': {}, 'files': [], 'blocker': 'Missing contract'}]:
                with self.assertRaises(PipelineError):
                    apply_proposal(spec(), folder, proposal)

    def test_symlink_or_junction_escape(self):
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as outside:
            link = Path(folder) / 'escape'
            if os.name == 'nt':
                subprocess.run(['powershell', '-NoProfile', '-Command',
                    'New-Item -ItemType Junction -Path $env:PIPE_TEST_LINK -Target $env:PIPE_TEST_TARGET'],
                    env={**os.environ, 'PIPE_TEST_LINK': str(link), 'PIPE_TEST_TARGET': outside},
                    capture_output=True, check=True)
            else:
                link.symlink_to(outside, target_is_directory=True)
            try:
                with self.assertRaises(PipelineError):
                    safe_path(folder, 'escape/file.txt')
            finally:
                if os.name == 'nt':
                    link.rmdir()
                else:
                    link.unlink()

    def test_missing_context_and_remote_endpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            task = spec()
            task['context'] = ['missing.md']
            with self.assertRaises(PipelineError):
                build_context(task, folder, {})
            (Path(folder) / '.env').write_text('LOCAL_LLM_BASE_URL=https://example.com/v1')
            with self.assertRaises(PipelineError):
                config(folder)

    def test_exclusive_controller(self):
        with tempfile.TemporaryDirectory() as folder:
            with lock(Path(folder)):
                with self.assertRaises(PipelineError):
                    with lock(Path(folder)):
                        pass
            self.assertFalse((Path(folder) / '.pipeline/controller.lock').exists())


class GitLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        git(self.root, 'init', '-b', 'main')
        git(self.root, 'config', 'user.name', 'Pipeline Test')
        git(self.root, 'config', 'user.email', 'pipeline@example.invalid')
        (self.root / '.gitignore').write_text('.pipeline/\n.worktrees/\nreports/\n')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Test genesis')
        self.pipeline = Pipeline(self.root)
        self.spec = spec()
        write_json(self.root / '.pipeline/spec.json', self.spec)
        self.pipeline.create(self.root / '.pipeline/spec.json')

    def tearDown(self):
        # TemporaryDirectory owns this verified test-only tree; no user workspace deletion.
        for r in self.pipeline.state['tasks'].values():
            if r.get('worktree') and Path(r['worktree']).exists():
                git(self.root, 'worktree', 'remove', '--force', r['worktree'])
        self.temp.cleanup()

    def start_edit(self, content='value = 1\n'):
        self.pipeline.start('POC-100')
        r = self.pipeline.get('POC-100')
        path = Path(r['worktree']) / 'apps/api/example.py'
        path.parent.mkdir(parents=True)
        path.write_text(content)
        return r, path

    def approve(self, status='PASS'):
        r = self.pipeline.get('POC-100')
        review = {'task': 'POC-100', 'reviewer': 'cloud:test-fixture', 'status': status,
                  'snapshot': r['validation']['snapshot'], 'blocking': [] if status == 'PASS' else ['Fix value'],
                  'non_blocking': [], 'recommended_actions': [],
                  'tests': {'status': 'pass'}, 'scope': {'status': 'pass'}, 'architecture': {'status': 'pass'}}
        path = self.root / '.pipeline/review.json'
        write_json(path, review)
        self.pipeline.review('POC-100', path)

    def test_complete_worktree_validation_review_merge_cleanup(self):
        r, path = self.start_edit()
        self.assertFalse((self.root / 'apps/api/example.py').exists())
        self.pipeline.validate('POC-100')
        self.approve()
        self.pipeline.finish('POC-100')
        self.assertEqual(r['state'], 'DONE')
        self.assertTrue((self.root / 'apps/api/example.py').exists())
        self.pipeline.cleanup('POC-100')
        self.assertIsNone(r['worktree'])
        self.assertFalse(path.exists())

    def test_finish_stages_a_reviewed_rename(self):
        original = self.root / 'apps/api/original.py'
        original.parent.mkdir(parents=True)
        original.write_text('value = 1\n')
        git(self.root, 'add', 'apps/api/original.py')
        git(self.root, 'commit', '-m', 'Tracked source for rename')
        self.pipeline.get('POC-100')['task']['validation'] = [
            {'name': 'compile renamed', 'argv': ['{python}', '-c',
             "compile(open('apps/api/renamed.py').read(), 'renamed.py', 'exec')"]}
        ]

        self.pipeline.start('POC-100')
        r = self.pipeline.get('POC-100')
        worktree = Path(r['worktree'])
        renamed = worktree / 'apps/api/renamed.py'
        (worktree / 'apps/api/original.py').rename(renamed)

        self.pipeline.validate('POC-100')
        self.approve()
        self.pipeline.finish('POC-100')

        self.assertFalse(original.exists())
        self.assertTrue((self.root / 'apps/api/renamed.py').exists())

    def test_finish_merges_a_reviewed_committed_rename(self):
        original = self.root / 'apps/api/original.py'
        original.parent.mkdir(parents=True)
        original.write_text('value = 1\n')
        git(self.root, 'add', 'apps/api/original.py')
        git(self.root, 'commit', '-m', 'Tracked source for committed rename')
        self.pipeline.get('POC-100')['task']['validation'] = [
            {'name': 'compile renamed', 'argv': ['{python}', '-c',
             "compile(open('apps/api/renamed.py').read(), 'renamed.py', 'exec')"]}
        ]

        self.pipeline.start('POC-100')
        r = self.pipeline.get('POC-100')
        worktree = Path(r['worktree'])
        renamed = worktree / 'apps/api/renamed.py'
        (worktree / 'apps/api/original.py').rename(renamed)
        git(worktree, 'add', '-A', '--', 'apps/api')
        git(worktree, 'commit', '-m', 'Executor rename')

        self.pipeline.validate('POC-100')
        self.approve()
        self.pipeline.finish('POC-100')

        self.assertFalse(original.exists())
        self.assertTrue((self.root / 'apps/api/renamed.py').exists())

    def test_drive_advances_safe_steps_but_requires_implementation_and_review(self):
        result = self.pipeline.drive('POC-100')
        r = self.pipeline.get('POC-100')
        self.assertEqual(result['status'], 'implementation_required')
        self.assertEqual(result['actions'], ['started'])
        self.assertEqual(r['state'], 'ACTIVE')

        path = Path(r['worktree']) / 'apps/api/example.py'
        path.parent.mkdir(parents=True)
        path.write_text('value = 1\n')
        result = self.pipeline.drive('POC-100')
        self.assertEqual(result['status'], 'self_review_required')
        self.assertEqual(result['actions'], ['validated'])
        self.assertEqual(r['state'], 'REVIEW')

        self.approve()
        result = self.pipeline.drive('POC-100')
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['actions'], ['finished', 'cleaned_up'])
        self.assertEqual(r['state'], 'DONE')
        self.assertIsNone(r['worktree'])

    def test_drive_does_not_bypass_dependency_check(self):
        task = spec('POC-101')
        task['status'] = 'BACKLOG'
        task['depends_on'] = ['MISSING-001']
        write_json(self.root / '.pipeline/blocked.json', task)
        self.pipeline.create(self.root / '.pipeline/blocked.json')
        with self.assertRaisesRegex(PipelineError, 'Dependency not DONE'):
            self.pipeline.drive('POC-101')

    def test_failed_gate_cannot_review_or_finish(self):
        r, _ = self.start_edit('this is invalid python !!!')
        with self.assertRaises(PipelineError):
            self.pipeline.validate('POC-100')
        self.assertEqual(r['state'], 'ACTIVE')
        with self.assertRaises(PipelineError):
            self.pipeline.finish('POC-100')

    def test_changed_after_approval_is_rejected(self):
        _, path = self.start_edit()
        self.pipeline.validate('POC-100')
        self.approve()
        path.write_text('value = 2\n')
        with self.assertRaises(PipelineError):
            self.pipeline.finish('POC-100')

    def test_reconcile_rebases_clean_task_and_invalidates_seals(self):
        r, path = self.start_edit()
        wt = Path(r['worktree'])
        git(wt, 'add', 'apps/api/example.py')
        git(wt, 'commit', '-m', 'Task change before integration advance')
        (self.root / 'lead-note.txt').write_text('approved unrelated change\n')
        git(self.root, 'add', 'lead-note.txt')
        git(self.root, 'commit', '-m', 'Advance integration branch')

        result = self.pipeline.reconcile('POC-100')

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(r['base'], git(self.root, 'rev-parse', 'HEAD'))
        self.assertEqual(r['state'], 'ACTIVE')
        self.assertIsNone(r['validation'])
        self.assertIsNone(r['review'])
        self.assertEqual(path.read_text(), 'value = 1\n')
        self.pipeline.validate('POC-100')

    def test_untracked_out_of_scope_is_rejected(self):
        r, _ = self.start_edit()
        (Path(r['worktree']) / 'outside.txt').write_text('outside')
        with self.assertRaises(PipelineError):
            self.pipeline.validate('POC-100')
        self.assertIn('Outside allowed_paths', r['blocker'])

    def test_committed_scope_violation_is_detected(self):
        r, _ = self.start_edit()
        wt = Path(r['worktree'])
        (wt / 'outside.txt').write_text('outside')
        git(wt, 'add', '.')
        git(wt, 'commit', '-m', 'Unexpected out of scope commit')
        with self.assertRaises(PipelineError):
            snapshot(wt, r['base'], self.spec)

    def test_staged_outside_scope_hidden_by_working_copy_is_rejected(self):
        # A staged edit remains commit-able even when working bytes match the base.
        path = self.root / 'outside.txt'
        path.write_text('original')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Lead-owned source')
        r, _ = self.start_edit()
        wt = Path(r['worktree'])
        path = wt / 'outside.txt'
        path.write_text('staged forbidden change')
        git(wt, 'add', 'outside.txt')
        path.write_text('original')
        with self.assertRaises(PipelineError):
            self.pipeline.validate('POC-100')

    def test_feedback_returns_active(self):
        r, _ = self.start_edit()
        self.pipeline.validate('POC-100')
        self.approve('CHANGES_REQUESTED')
        self.assertEqual(r['state'], 'ACTIVE')
        self.assertEqual(self.pipeline.feedback(r)['review']['blocking'], ['Fix value'])

    def test_local_run_requires_opt_in_before_config_or_attempt(self):
        r, _ = self.start_edit()
        with patch('task.config', side_effect=AssertionError('must not read local config')):
            with self.assertRaisesRegex(PipelineError, 'experimental'):
                self.pipeline.run('POC-100')
        self.assertEqual(r['attempts'], 0)
        self.assertEqual(r['state'], 'ACTIVE')

    def test_cloud_lifecycle_without_local_server_config_or_bootstrap(self):
        task = spec('DEV-100')
        task['executor']['preferred'] = 'cloud'
        write_json(self.root / '.pipeline/cloud.json', task)
        self.pipeline.create(self.root / '.pipeline/cloud.json')
        self.pipeline.start('DEV-100')
        with patch('task.config', side_effect=AssertionError('local config forbidden')), \
             patch('task.LocalExecutor.run', side_effect=AssertionError('local call forbidden')):
            result = self.pipeline.run('DEV-100')
        self.assertEqual(result['mode'], 'manual_cloud_handoff')
        r = self.pipeline.get('DEV-100')
        self.assertEqual(r['attempts'], 0)
        p = Path(r['worktree']) / 'apps/api/example.py'
        p.parent.mkdir(parents=True)
        p.write_text('value = 1\n')
        self.pipeline.validate('DEV-100')
        review = {'task': 'DEV-100', 'reviewer': 'cloud:test', 'status': 'PASS',
                  'snapshot': r['validation']['snapshot'], 'blocking': [],
                  'non_blocking': [], 'recommended_actions': [],
                  'tests': {'status': 'pass'}, 'scope': {'status': 'pass'},
                  'architecture': {'status': 'pass'}}
        write_json(self.root / '.pipeline/cloud-review.json', review)
        self.pipeline.review('DEV-100', self.root / '.pipeline/cloud-review.json')
        self.pipeline.finish('DEV-100')
        self.pipeline.cleanup('DEV-100')
        self.assertEqual(r['state'], 'DONE')
        self.assertIsNone(r['worktree'])

    def test_retry_limit_escalates_without_api_call(self):
        r, _ = self.start_edit()
        r['attempts'] = 2
        with self.assertRaises(PipelineError):
            self.pipeline.run('POC-100', experimental_local=True)
        self.assertEqual(r['state'], 'BLOCKED')
        self.assertIn('escalate', r['blocker'])

    def test_local_failure_feedback_and_fix_are_recorded(self):
        r, _ = self.start_edit()
        (self.root / 'agents').mkdir()
        (self.root / 'agents/local-worker.md').write_text('Bounded task')
        seen = []
        def reply(executor, task, context):
            seen.append(context)
            name = 'outside.py' if len(seen) == 1 else 'apps/api/example.py'
            return {'proposal': {'handoff': {'validation': 'host must run'}, 'files': [
                {'path': name, 'content': 'value = 2\n'}]}, 'model': 'test-fixture',
                'elapsed_seconds': 0, 'usage': {}}
        with patch('task.LocalExecutor.run', reply):
            with self.assertRaises(PipelineError):
                self.pipeline.run('POC-100', experimental_local=True)
            self.assertEqual(r['state'], 'ACTIVE')
            self.pipeline.run('POC-100', experimental_local=True)
        self.assertIn('Outside allowed_paths', seen[1]['feedback']['blocker'])
        self.assertEqual(r['attempts'], 2)
        self.assertEqual(r['retry_count'], 1)
        self.assertFalse((Path(r['worktree']) / 'outside.py').exists())

    def test_bootstrap_attestation_cannot_bypass_normal_lifecycle(self):
        with self.assertRaises(PipelineError):
            accept(self.pipeline, 'POC-100', {'status': 'PASS'})

    def test_explicit_dependencies_still_required_without_implicit_poc_gate(self):
        task = spec('ARCH-001')
        task['depends_on'] = ['PIPE-005']
        write_json(self.root / '.pipeline/arch.json', task)
        with self.assertRaises(PipelineError):
            self.pipeline.create(self.root / '.pipeline/arch.json')
        task['depends_on'] = []
        write_json(self.root / '.pipeline/arch.json', task)
        self.pipeline.create(self.root / '.pipeline/arch.json')
        self.pipeline.start('ARCH-001')
        self.assertEqual(self.pipeline.get('ARCH-001')['state'], 'ACTIVE')

    def test_concurrent_overlapping_scope_is_rejected(self):
        self.start_edit()
        task = spec('POC-101')
        write_json(self.root / '.pipeline/other.json', task)
        self.pipeline.create(self.root / '.pipeline/other.json')
        with self.assertRaises(PipelineError):
            self.pipeline.start('POC-101')

    def test_integration_drift_requires_new_validation(self):
        self.start_edit()
        self.pipeline.validate('POC-100')
        self.approve()
        (self.root / 'lead.txt').write_text('new base')
        git(self.root, 'add', '.')
        git(self.root, 'commit', '-m', 'Base advances')
        with self.assertRaises(PipelineError):
            self.pipeline.finish('POC-100')

    def test_cleanup_before_done_is_rejected(self):
        self.start_edit()
        with self.assertRaises(PipelineError):
            self.pipeline.cleanup('POC-100')


if __name__ == '__main__':
    unittest.main()
