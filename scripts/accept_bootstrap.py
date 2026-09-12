"""Lead attestation for PIPE genesis only; ordinary tasks must use worktree lifecycle.

Usage: python scripts/accept_bootstrap.py PIPE-001 reports/runs/bootstrap-review.json
The cloud lead must inspect implementation and checks before supplying this review.
"""
import sys
from pathlib import Path

from policy import require, git, safe_path, PipelineError
from task import Pipeline, lock, read_json, now


def accept(pipeline, task_id, review):
    require(task_id in [f'PIPE-{i:03d}' for i in range(1, 6)], 'Only PIPE genesis steps supported')
    r = pipeline.get(task_id)
    require(r['state'] in ('BACKLOG', 'READY') and r['worktree'] is None, 'Cannot overwrite existing execution')
    pipeline.deps(r['task'])
    pipeline.clean_root()
    head = git(pipeline.root, 'rev-parse', 'HEAD')
    require(review.get('status') == 'PASS' and review.get('reviewer', '').startswith('cloud:')
            and review.get('commit') == head and review.get('tests_status') == 'pass',
            'Require cloud PASS, current commit, and passing check attestation')
    evidence = review.get('evidence', {}).get(task_id)
    require(isinstance(evidence, list) and bool(evidence), 'Missing step-specific evidence')
    for name in evidence:
        require(safe_path(pipeline.root, name).is_file(), f'Missing evidence: {name}')
        git(pipeline.root, 'ls-files', '--error-unmatch', '--', name)
    if task_id == 'PIPE-005':
        poc = pipeline.get('POC-001')
        require(poc['state'] == 'DONE' and poc['worktree'] is None, 'POC must be merged and cleaned up')
        require(any(e['action'] == 'local_applied' for e in poc['history']), 'POC needs actual local implementation')
        git(pipeline.root, 'merge-base', '--is-ancestor', poc['implementation_commit'], 'HEAD')
    r.update(state='DONE', executor='cloud', review=review, blocker=None,
             implementation_commit=head, completed_at=now())
    pipeline.event(r, 'bootstrap_lead_accepted', commit=head, evidence=evidence,
                   note='Genesis lead implementation; not a worker task execution')


if __name__ == '__main__':
    try:
        require(len(sys.argv) == 3, __doc__)
        root = Path(__file__).resolve().parent.parent
        with lock(root):
            accept(Pipeline(root), sys.argv[1], read_json(sys.argv[2]))
        print('Accepted', sys.argv[1])
    except (PipelineError, OSError, ValueError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)
