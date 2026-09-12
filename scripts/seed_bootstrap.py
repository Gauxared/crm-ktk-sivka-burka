"""Create initial task specs/placeholders once. Does not register or complete tasks."""
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def put(name, content):
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(content, encoding='utf-8', newline='\n')


def task(task_id, title, deps, paths, context, requirements, validation, executor='cloud'):
    return {'id': task_id, 'title': title, 'type': 'bootstrap' if task_id.startswith('PIPE-') else 'implementation',
            'status': 'BACKLOG', 'priority': 'high', 'executor': {'preferred': executor, 'fallback': 'cloud'},
            'reviewer': 'cloud', 'depends_on': deps, 'allowed_paths': paths,
            'forbidden_paths': [], 'context': context, 'requirements': requirements,
            'acceptance': ['Requirements satisfied; validation passes; cloud review PASS'],
            'validation': validation, 'risk': 'low' if executor == 'local' else 'medium', 'retry_limit': 1}


def main():
    for folder in ['apps/api', 'apps/web', 'apps/admin', 'apps/bot',
                   'packages/contracts', 'packages/ui', 'packages/config']:
        put(folder + '/.gitkeep', '')
    for name in ['requirements', 'architecture', 'domain-model', 'booking-flow', 'api', 'database']:
        put(f'docs/{name}.md', f'# {name}\n\nStatus: DRAFT PLACEHOLDER. Not an approved product contract.\n'
            'Populate and review in an ARCH task after PIPE-005. No business decisions are implied.\n')
    gate = [{'name': 'pipeline tests', 'argv': ['{python}', '-m', 'unittest', 'discover', '-s', 'tests/pipeline', '-v'], 'timeout': 120}]
    steps = [
        ('Repository and task architecture', ['README.md', 'docs/**', 'agents/**', 'tasks/**', 'decisions/**'],
         ['Define task schema, lifecycle, roles and canonical document placeholders']),
        ('Git isolation and path protection', ['scripts/**', 'tests/pipeline/**'],
         ['Create task branches/worktrees; reject scope violations and shared path edits']),
        ('Local executor abstraction', ['scripts/**', 'agents/**', 'docs/**'],
         ['Connect loopback OpenAI-compatible API; bound context and file writes']),
        ('Validation, review, retry and escalation', ['scripts/**', 'tests/pipeline/**', 'docs/**'],
         ['Enforce passing checks and current review before merge; limit local attempts']),
        ('Real local worker proof of concept', ['reports/**'],
         ['POC-001 locally implemented, validated, reviewed, merged, DONE and cleaned up'])]
    for i, (title, paths, requirements) in enumerate(steps, 1):
        t = task(f'PIPE-{i:03d}', title, [f'PIPE-{i-1:03d}'] if i > 1 else [], paths,
                 ['docs/development-pipeline.md'], requirements, gate)
        put(f"tasks/specs/{t['id']}.json", json.dumps(t, ensure_ascii=False, indent=2) + '\n')
    poc = task('POC-001', 'Implement minimal standard-library health API', ['PIPE-004'],
               ['apps/api/health.py', 'tests/poc/test_health_worker.py'],
               ['docs/poc-health-contract.md', 'tests/poc/test_health_contract.py'],
               ['Implement exactly the supplied approved health contract',
                'Add worker-owned unittest tests; do not edit independent lead tests',
                'Use only standard library; no dependencies or business features'],
               [{'name': 'unit and HTTP contract tests', 'argv': ['{python}', '-m', 'unittest', 'discover', '-s', 'tests/poc', '-v'], 'timeout': 30},
                {'name': 'syntax', 'argv': ['{python}', '-c', "from pathlib import Path; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in [Path('apps/api/health.py'), Path('tests/poc/test_health_worker.py')]]"], 'timeout': 10},
                {'name': 'diff whitespace', 'argv': ['git', 'diff', '--check'], 'timeout': 10}], executor='local')
    poc['forbidden_paths'] = ['tests/poc/test_health_contract.py', 'scripts/**', 'packages/**', '.env*']
    put('tasks/specs/POC-001.json', json.dumps(poc, ensure_ascii=False, indent=2) + '\n')
    put('tasks/TASK_TEMPLATE.md', '# Task format v1\n\nCreate one JSON file in tasks/specs. '
        'Live status is in .pipeline/state.json. All keys below are required. '
        'Validation uses argv arrays, never shell strings; {python} selects the running interpreter. '
        'Context is an explicit list of existing UTF-8 files. retry_limit counts extra calls after the first. '
        'To modify shared paths add shared_paths_approval with paths, reviewer and reason in a dedicated cloud task.\n\n'
        '```json\n' + json.dumps(poc, ensure_ascii=False, indent=2) + '\n```\n')


if __name__ == '__main__':
    main()
