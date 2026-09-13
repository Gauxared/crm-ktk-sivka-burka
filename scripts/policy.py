"""Task schema and filesystem boundaries; no model output is authority."""
import fnmatch
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath


class PipelineError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PipelineError(message)


def git(root, *args):
    result = subprocess.run(['git', '-c', 'core.quotepath=false', *args], cwd=root,
                            capture_output=True, encoding='utf-8', errors='replace')
    require(result.returncode == 0, result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def relative_path(value):
    require(isinstance(value, str) and bool(value), 'Path must be a nonempty string')
    require('\\' not in value and ':' not in value and '\x00' not in value,
            f'Invalid path: {value!r}')
    p = PurePosixPath(value)
    require(not p.is_absolute() and all(x not in ('', '.', '..') for x in value.split('/')),
            f'Path traversal/normalization denied: {value}')
    require(all(not x.endswith((' ', '.')) for x in p.parts), f'Unsafe Windows path: {value}')
    require(not any(re.fullmatch(r'(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?', x, re.I)
                    for x in p.parts), f'Reserved Windows path: {value}')
    return p


def safe_path(root, value):
    p = relative_path(value)
    require(not any(ord(c) < 32 or c in '<>"|?*' for c in value), f'Invalid filename: {value!r}')
    root = Path(root).resolve()
    target = root.joinpath(*p.parts)
    current = root
    for part in p.parts:
        current /= part
        require(not current.is_symlink() and not current.is_junction(),
                f'Link/reparse path denied: {value}')
    require(target.resolve().is_relative_to(root), f'Path escapes workspace: {value}')
    return target


def matches(path, patterns):
    return any(fnmatch.fnmatchcase(path.lower(), pattern.lower()) for pattern in patterns)


SHARED = ['package.json', '*lock*', 'docker-compose*', 'compose.y*ml',
          'packages/contracts/**', 'packages/config/**', 'database/**', 'schema/**',
          '.github/**', 'scripts/**', 'agents/**', 'tasks/**', 'decisions/**',
          'AGENTS.md', 'pyproject.toml', 'requirements*.txt', 'tsconfig*', '.git*']
SECRETS = ['.env', '.env.*', '**/.env', '**/.env.*', '*.pem', '*.key',
           '**/credentials*', '**/secrets*']


def is_explicit_env_template(task, path):
    """Permit the one synthetic template name only when its task scope names it."""
    return path == '.env.example' and matches(path, task['allowed_paths'])


def check_scope(task, paths):
    approval = task.get('shared_paths_approval', {})
    approved = approval.get('paths', []) if approval.get('reviewer') and approval.get('reason') else []
    for path in paths:
        relative_path(path)
        parts = PurePosixPath(path.lower()).parts
        require(not any(x in ('.git', '.pipeline', '.worktrees') for x in parts),
                f'Controller metadata denied: {path}')
        require(not matches(path, SECRETS) or is_explicit_env_template(task, path),
                f'Secret-like path denied: {path}')
        require(matches(path, task['allowed_paths']), f'Outside allowed_paths: {path}')
        require(not matches(path, task['forbidden_paths']), f'Forbidden path: {path}')
        require(not matches(path, SHARED) or matches(path, approved),
                f'Shared path requires lead approval: {path}')


def validate_spec(task):
    required = {'id', 'title', 'type', 'status', 'priority', 'executor', 'reviewer',
                'depends_on', 'allowed_paths', 'forbidden_paths', 'context',
                'requirements', 'acceptance', 'validation', 'risk', 'retry_limit'}
    require(isinstance(task, dict) and required <= task.keys(),
            f'Missing task fields: {sorted(required - task.keys()) if isinstance(task, dict) else required}')
    require(re.fullmatch(r'[A-Z][A-Z0-9]*-[0-9]{3,}', task['id']) is not None, 'Invalid task ID')
    require(task['status'] in ('BACKLOG', 'READY'), 'Initial status must be BACKLOG/READY')
    require(task['executor'].get('preferred') in ('local', 'cloud'), 'Invalid executor')
    require(task['executor'].get('fallback') == 'cloud', 'Fallback must be cloud')
    require(task['reviewer'] == 'cloud', 'Lead cloud review is required')
    require(type(task['retry_limit']) is int and 0 <= task['retry_limit'] <= 5,
            'retry_limit must be 0..5 additional attempts')
    for key in ('depends_on', 'allowed_paths', 'forbidden_paths', 'context', 'requirements', 'acceptance'):
        require(isinstance(task[key], list) and all(isinstance(x, str) for x in task[key]),
                f'{key} must be a string list')
    require(bool(task['allowed_paths']) and bool(task['requirements']) and bool(task['acceptance']),
            'Scope, requirements and acceptance cannot be empty')
    for pattern in task['allowed_paths'] + task['forbidden_paths']:
        relative_path(pattern)
    for path in task['context']:
        relative_path(path)
        require(not matches(path, SECRETS), 'Secret context denied')
    require(isinstance(task['validation'], list) and bool(task['validation']), 'Validation is required')
    for gate in task['validation']:
        require(isinstance(gate, dict) and isinstance(gate.get('name'), str), 'Gate needs a name')
        require(isinstance(gate.get('argv'), list) and bool(gate['argv']) and
                all(isinstance(x, str) and x for x in gate['argv']), 'Gate requires nonempty argv strings')
        require(type(gate.get('timeout', 60)) is int and 1 <= gate.get('timeout', 60) <= 300,
                'Gate timeout must be 1..300 seconds')


def changed_paths(root, base):
    # -z preserves spaces, unicode and newlines; no renames covers old and new paths.
    def names(*args):
        r = subprocess.run(['git', *args], cwd=root, capture_output=True)
        require(r.returncode == 0, r.stderr.decode('utf-8', 'replace'))
        return [x.decode('utf-8') for x in r.stdout.split(b'\0') if x]
    return sorted(set(names('diff', '--name-only', '--no-renames', '-z', base, '--') +
                      names('diff', '--cached', '--name-only', '--no-renames', '-z', base, '--') +
                      names('ls-files', '--others', '--exclude-standard', '-z')))


def snapshot(root, base, task):
    paths = changed_paths(root, base)
    check_scope(task, paths)
    entries = []
    for name in paths:
        p = safe_path(root, name)
        require(not p.exists() or p.is_file(), f'Only ordinary files supported: {name}')
        entries.append([name, hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else 'DELETED'])
    # Include index/HEAD to detect staged content and history changes as well as working files.
    seal = {'files': entries, 'head': git(root, 'rev-parse', 'HEAD'),
            'index': git(root, 'write-tree'), 'base': base,
            'raw_diff': git(root, 'diff', '--raw', '--no-renames', base, '--')}
    return hashlib.sha256(json.dumps(seal, sort_keys=True).encode()).hexdigest(), paths


def scopes_overlap(a, b):
    def prefix(pattern):
        return re.split(r'[*?\[]', pattern.lower(), maxsplit=1)[0].rstrip('/')
    for left in a:
        for right in b:
            x, y = prefix(left), prefix(right)
            if not x or not y or x.startswith(y) or y.startswith(x):
                return True
    return False
