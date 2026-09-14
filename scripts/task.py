"""Small single-controller task runner. Run from the primary checkout."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from policy import (PipelineError, require, git, validate_spec, safe_path,
                    snapshot, scopes_overlap)
from executors import config, build_context, LocalExecutor, CloudExecutor, apply_proposal


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


@contextmanager
def lock(root):
    path = root / '.pipeline/controller.lock'
    path.parent.mkdir(exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise PipelineError('Controller locked; inspect .pipeline/controller.lock and process before recovery')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump({'pid': os.getpid(), 'started_at': now()}, f)
        yield
    finally:
        path.unlink()


class Pipeline:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.state_path = self.root / '.pipeline/state.json'
        self.state = read_json(self.state_path) if self.state_path.exists() else {'version': 1, 'tasks': {}}

    def save(self):
        write_json(self.state_path, self.state)

    def event(self, record, action, **details):
        record['updated_at'] = now()
        record['history'].append({'at': record['updated_at'], 'action': action, **details})
        self.save()

    def get(self, task_id):
        require(task_id in self.state['tasks'], f'Unknown task: {task_id}')
        return self.state['tasks'][task_id]

    def deps(self, task):
        for dep in task['depends_on']:
            require(self.state['tasks'].get(dep, {}).get('state') == 'DONE', f'Dependency not DONE: {dep}')

    def clean_root(self):
        require(not git(self.root, 'status', '--porcelain'), 'Integration checkout must be clean; commit lead changes first')
        require((self.root / '.git').is_dir(), 'Use primary checkout, not a task worktree')

    def worktree(self, r):
        expected = self.root / '.worktrees' / r['task']['id']
        require(r.get('worktree') == str(expected), 'Worktree state path mismatch')
        require(expected.exists() and not expected.is_symlink() and not expected.is_junction(), 'Invalid worktree')
        require(git(expected, 'branch', '--show-current') == r['branch'], 'Unexpected worktree branch')
        return expected

    def logdir(self, task_id):
        return self.root / 'reports/runs' / task_id

    def create(self, spec_path):
        task = read_json(spec_path)
        validate_spec(task)
        require(task['id'] not in self.state['tasks'], 'Task already registered')
        if task['status'] == 'READY':
            self.deps(task)
        r = {'task': task, 'state': task['status'], 'executor': task['executor']['preferred'],
             'branch': None, 'worktree': None, 'attempts': 0, 'retry_count': 0,
             'review': None, 'validation': None, 'blocker': None, 'history': [], 'created_at': now()}
        self.state['tasks'][task['id']] = r
        self.event(r, 'created', state=r['state'])
        return r

    def ready(self, task_id):
        r = self.get(task_id)
        require(r['state'] == 'BACKLOG', 'Only BACKLOG can become READY')
        self.deps(r['task'])
        r['state'] = 'READY'
        self.event(r, 'ready')

    def start(self, task_id):
        r = self.get(task_id)
        require(r['state'] == 'READY', 'Task must be READY')
        self.deps(r['task'])
        self.clean_root()
        for other in self.state['tasks'].values():
            if other['worktree'] and other['state'] != 'DONE':
                require(not scopes_overlap(r['task']['allowed_paths'], other['task']['allowed_paths']),
                        f"Write scope locked by {other['task']['id']}")
        branch = 'codex/' + task_id
        wt = self.root / '.worktrees' / task_id
        require(not wt.exists(), 'Worktree path already exists')
        require(not (self.root / '.worktrees').is_symlink() and
                not (self.root / '.worktrees').is_junction(), 'Worktree parent may not be a link')
        base = git(self.root, 'rev-parse', 'HEAD')
        target = git(self.root, 'branch', '--show-current')
        require(bool(target), 'Integration checkout must have a branch')
        git(self.root, 'worktree', 'add', '-b', branch, str(wt), base)
        r.update(state='ACTIVE', branch=branch, worktree=str(wt), base=base, target=target)
        self.event(r, 'started', base=base, branch=branch, worktree=str(wt), executor=r['executor'])

    def feedback(self, r):
        return {'review': r.get('review'), 'validation': r.get('validation'), 'blocker': r.get('blocker')}

    def reconcile(self, task_id):
        """Rebase one clean task branch after an unrelated approved integration merge."""
        r = self.get(task_id)
        require(r['state'] in ('ACTIVE', 'REVIEW'), 'Reconcile requires ACTIVE or REVIEW')
        self.clean_root()
        require(git(self.root, 'branch', '--show-current') == r['target'],
                'Wrong integration branch')
        wt = self.worktree(r)
        require(not git(wt, 'status', '--porcelain'),
                'Task worktree must have no unstaged, staged, or untracked source changes')
        git(wt, 'rebase', r['target'])
        r.update(base=git(self.root, 'rev-parse', 'HEAD'), state='ACTIVE', review=None,
                 validation=None, blocker=None)
        self.event(r, 'reconciled', base=r['base'], target=r['target'])
        return {'status': 'ok', 'base': r['base']}

    def run(self, task_id, experimental_local=False):
        r = self.get(task_id)
        require(r['state'] == 'ACTIVE', 'Run requires ACTIVE')
        if r['executor'] == 'cloud':
            return self.handoff(task_id)
        require(experimental_local, 'Local execution is experimental; use run ID --experimental-local or handoff ID')
        if r['attempts'] >= 1 + r['task']['retry_limit']:
            r.update(state='BLOCKED', blocker='Local attempt limit reached; escalate to cloud')
            self.event(r, 'escalated', reason=r['blocker'])
            raise PipelineError(r['blocker'])
        wt = self.worktree(r)
        cfg = config(self.root)
        context = build_context(r['task'], wt, self.feedback(r))
        # Include only prior changed permitted files on fix attempts.
        _, current = snapshot(wt, r['base'], r['task'])
        for name in current:
            p = safe_path(wt, name)
            if p.is_file():
                require(p.stat().st_size <= 32000, f'Retry source too large: {name}')
                context['files'][name] = p.read_text(encoding='utf-8')
        r['attempts'] += 1
        r['retry_count'] = max(0, r['attempts'] - 1)
        attempt = self.logdir(task_id) / f"attempt-{r['attempts']:02d}"
        r.update(review=None, validation=None, blocker=None)
        self.event(r, 'local_started', attempt=r['attempts'], model=cfg['MODEL'])
        try:
            executor = LocalExecutor(cfg, (self.root / 'agents/local-worker.md').read_text(encoding='utf-8'), attempt)
            result = executor.run(r['task'], context)
            write_json(attempt / 'result.json', result)
            files = apply_proposal(r['task'], wt, result['proposal'])
            write_json(attempt / 'handoff.json', result['proposal']['handoff'])
            self.event(r, 'local_applied', files=files, model=result['model'],
                       elapsed_seconds=result['elapsed_seconds'], usage=result['usage'])
            return result
        except Exception as exc:
            r['blocker'] = f'{type(exc).__name__}: {exc}'
            if r['attempts'] >= 1 + r['task']['retry_limit']:
                r['state'] = 'BLOCKED'
            write_json(attempt / 'failure.json', {'at': now(), 'error': r['blocker']})
            self.event(r, 'local_failed', reason=r['blocker'])
            raise PipelineError(r['blocker']) from exc

    def drive(self, task_id):
        """Advance only deterministic lifecycle steps for an authorized task.

        Implementation and review remain deliberate Codex responsibilities in the
        task worktree.  This method never fabricates either kind of evidence.
        """
        r = self.get(task_id)
        actions = []
        if r['state'] == 'BLOCKED':
            return {'status': 'blocked', 'task': task_id, 'blocker': r['blocker']}

        if r['state'] == 'BACKLOG':
            self.ready(task_id)
            actions.append('ready')
            r = self.get(task_id)

        if r['state'] == 'READY':
            self.start(task_id)
            actions.append('started')
            r = self.get(task_id)

        if r['state'] == 'ACTIVE':
            require(git(self.root, 'branch', '--show-current') == r['target'],
                    'Wrong integration branch')
            if git(self.root, 'rev-parse', 'HEAD') != r['base']:
                wt = self.worktree(r)
                if not git(wt, 'status', '--porcelain'):
                    self.reconcile(task_id)
                    actions.append('reconciled')
                    r = self.get(task_id)
                else:
                    return {'status': 'reconcile_required', 'task': task_id, 'actions': actions,
                            'worktree': r['worktree'],
                            'next': 'Finish or safely preserve the current worktree changes, then reconcile and revalidate.'}
            _, paths = snapshot(self.worktree(r), r['base'], r['task'])
            if not paths:
                return {'status': 'implementation_required', 'task': task_id, 'actions': actions,
                        'worktree': r['worktree'],
                        'next': 'Implement the accepted task in allowed_paths, then run drive again.'}
            self.validate(task_id)
            actions.append('validated')
            r = self.get(task_id)

        if r['state'] == 'REVIEW':
            review = r.get('review')
            if not review:
                return {'status': 'self_review_required', 'task': task_id, 'actions': actions,
                        'worktree': r['worktree'],
                        'next': 'Inspect the actual diff, acceptance criteria and validation evidence; write a self-review, then submit it with review.'}
            require(review.get('status') == 'PASS', 'Current PASS review required')
            self.finish(task_id)
            actions.append('finished')
            self.cleanup(task_id)
            actions.append('cleaned_up')
            r = self.get(task_id)

        require(r['state'] == 'DONE', f'Unexpected drive state: {r["state"]}')
        return {'status': 'done', 'task': task_id, 'actions': actions,
                'merge_commit': r.get('merge_commit')}

    def validate(self, task_id):
        r = self.get(task_id)
        require(r['state'] in ('ACTIVE', 'REVIEW'), 'Validation requires ACTIVE or REVIEW')
        wt = self.worktree(r)
        r.update(state='ACTIVE', review=None, validation=None)
        self.event(r, 'validation_started')
        result = {'task': task_id, 'at': now(), 'status': 'fail', 'gates': []}
        try:
            before, paths = snapshot(wt, r['base'], r['task'])
            require(bool(paths), 'Task has no changes')
            result['files'] = paths
            result['scope'] = 'pass'
            for gate in r['task']['validation']:
                argv = [sys.executable if x == '{python}' else x for x in gate['argv']]
                try:
                    proc = subprocess.run(argv, cwd=wt, capture_output=True, encoding='utf-8',
                                          errors='replace', timeout=gate.get('timeout', 60), shell=False,
                                          env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
                    row = {'name': gate['name'], 'argv': gate['argv'], 'exit_code': proc.returncode,
                           'stdout': proc.stdout[-20000:], 'stderr': proc.stderr[-20000:]}
                except subprocess.TimeoutExpired:
                    row = {'name': gate['name'], 'exit_code': -1, 'stderr': 'Gate timed out'}
                result['gates'].append(row)
            after, _ = snapshot(wt, r['base'], r['task'])
            require(before == after, 'Validation changed source/index; inspect diff then revalidate')
            require(all(x['exit_code'] == 0 for x in result['gates']), 'Required validation gate failed')
            result.update(status='pass', snapshot=after)
            r.update(state='REVIEW', blocker=None)
        except Exception as exc:
            result['error'] = str(exc)
            r['blocker'] = str(exc)
        r['validation'] = result
        write_json(self.logdir(task_id) / 'validation.json', result)
        # Retain every gate run, not just the current one.
        write_json(self.logdir(task_id) / f"validation-{len(r['history']):03d}.json", result)
        self.event(r, 'validated', status=result['status'])
        require(result['status'] == 'pass', result.get('error', 'Validation failed'))
        return result

    def review(self, task_id, review_path):
        r = self.get(task_id)
        require(r['state'] == 'REVIEW', 'Review requires passing validation')
        review = read_json(review_path)
        require(review.get('task') == task_id and review.get('reviewer', '').startswith('cloud:'),
                'Review must identify task and cloud reviewer')
        require(review.get('status') in ('PASS', 'CHANGES_REQUESTED', 'BLOCKED', 'ESCALATE'), 'Invalid review status')
        require(all(isinstance(review.get(k), list) for k in ('blocking', 'non_blocking', 'recommended_actions')),
                'Review must include structured findings')
        current, _ = snapshot(self.worktree(r), r['base'], r['task'])
        require(review.get('snapshot') == current == r['validation'].get('snapshot'), 'Stale validation/review snapshot')
        if review['status'] == 'PASS':
            require(not review['blocking'] and all(review.get(k, {}).get('status') == 'pass'
                    for k in ('tests', 'scope', 'architecture')), 'PASS requires passing assessments and no blockers')
        else:
            require(bool(review['blocking']), 'Non-PASS requires concrete blockers')
            r['blocker'] = '; '.join(review['blocking'])
            r['state'] = 'ACTIVE' if review['status'] == 'CHANGES_REQUESTED' else 'BLOCKED'
        r['review'] = review
        write_json(self.logdir(task_id) / f"review-{len(r['history']):03d}.json", review)
        self.event(r, 'reviewed', status=review['status'], reviewer=review['reviewer'])

    def finish(self, task_id):
        r = self.get(task_id)
        require(r['state'] == 'REVIEW' and (r.get('review') or {}).get('status') == 'PASS', 'Current PASS review required')
        self.clean_root()
        require(git(self.root, 'branch', '--show-current') == r['target'], 'Wrong integration branch')
        require(git(self.root, 'rev-parse', 'HEAD') == r['base'], 'Integration HEAD changed; lead must reconcile base and revalidate')
        wt = self.worktree(r)
        current, paths = snapshot(wt, r['base'], r['task'])
        require(current == r['review']['snapshot'] == r['validation']['snapshot'], 'Changes after review; validate/review again')
        require(bool(paths), 'Nothing to merge')
        # The current snapshot has already been scope-validated and sealed.
        # Stage it as a whole so Git handles both halves of an uncommitted
        # rename, while a rename committed by the executor needs no pathspec.
        git(wt, 'add', '-A', '--', '.')
        if git(wt, 'diff', '--cached', '--name-only'):
            git(wt, 'commit', '-m', f"{task_id}: {r['task']['title']}\n\nExecutor: {r['executor']}\nReviewer: {r['review']['reviewer']}")
        commit = git(wt, 'rev-parse', 'HEAD')
        r['implementation_commit'] = commit
        self.event(r, 'implementation_committed', commit=commit)
        try:
            git(self.root, 'merge', '--no-ff', '--no-edit', r['branch'], '-m', f'Merge {task_id}: reviewed pipeline task')
        except PipelineError as exc:
            r.update(state='BLOCKED', blocker='Merge failed; lead recovery required: ' + str(exc))
            self.event(r, 'merge_failed', reason=r['blocker'])
            raise
        r.update(state='DONE', blocker=None, merge_commit=git(self.root, 'rev-parse', 'HEAD'), completed_at=now())
        self.event(r, 'merged', commit=r['merge_commit'])

    def cleanup(self, task_id):
        r = self.get(task_id)
        require(r['state'] == 'DONE', 'Cleanup requires DONE')
        wt = self.worktree(r)
        require(not git(wt, 'status', '--porcelain', '--ignored'), 'Worktree has changes/ignored files; inspect and remove generated files explicitly')
        git(self.root, 'merge-base', '--is-ancestor', r['implementation_commit'], 'HEAD')
        git(self.root, 'worktree', 'remove', str(wt))
        r['worktree'] = None
        self.event(r, 'cleaned_up', retained_branch=r['branch'])

    def handoff(self, task_id):
        r = self.get(task_id)
        require(r['state'] in ('ACTIVE', 'BLOCKED'), 'Handoff requires active or blocked task')
        wt = self.worktree(r)
        context = build_context(r['task'], wt, self.feedback(r))
        result = CloudExecutor().run(r['task'], context)
        result['worktree'] = str(wt)
        write_json(self.logdir(task_id) / 'cloud-handoff.json', result)
        r.update(executor='cloud', state='ACTIVE')
        self.event(r, 'cloud_handoff', reason=r['blocker'])
        return result

    def resume(self, task_id, reason):
        r = self.get(task_id)
        require(r['state'] == 'BLOCKED' and bool(reason.strip()), 'Resume requires BLOCKED and reason')
        self.worktree(r)
        r.update(state='ACTIVE', blocker=None, review=None, validation=None)
        self.event(r, 'resumed', reason=reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('create').add_argument('spec')
    for name in ('ready', 'start', 'validate', 'finish', 'cleanup', 'handoff', 'reconcile', 'drive'):
        sub.add_parser(name).add_argument('id')
    run = sub.add_parser('run')
    run.add_argument('id')
    run.add_argument('--experimental-local', action='store_true',
                     help='Explicitly opt into one bounded local generation experiment')
    status = sub.add_parser('status')
    status.add_argument('id', nargs='?')
    review = sub.add_parser('review')
    review.add_argument('id')
    review.add_argument('file')
    resume = sub.add_parser('resume')
    resume.add_argument('id')
    resume.add_argument('--reason', required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        require((root / '.git').is_dir(), 'Run controller from the primary checkout')
        with lock(root):
            pipeline = Pipeline(root)
            if args.command == 'status':
                result = pipeline.get(args.id) if args.id else {
                    k: {'state': v['state'], 'executor': v['executor'], 'attempts': v['attempts'],
                        'blocker': v['blocker']} for k, v in pipeline.state['tasks'].items()}
            elif args.command == 'create':
                result = pipeline.create(args.spec)
            elif args.command == 'review':
                result = pipeline.review(args.id, args.file)
            elif args.command == 'resume':
                result = pipeline.resume(args.id, args.reason)
            elif args.command == 'run':
                result = pipeline.run(args.id, experimental_local=args.experimental_local)
            else:
                result = getattr(pipeline, args.command)(args.id)
            print(json.dumps(result if result is not None else {'status': 'ok'}, ensure_ascii=False, indent=2))
    except (PipelineError, OSError, ValueError, KeyError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
