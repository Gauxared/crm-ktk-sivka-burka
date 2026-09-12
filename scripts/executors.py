"""Runtime-independent, bounded executor adapters using only the standard library."""
import json
import os
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from policy import require, safe_path, check_scope, PipelineError


def config(root):
    values = {}
    env_file = Path(root) / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            if line.strip() and not line.lstrip().startswith('#'):
                key, sep, value = line.partition('=')
                require(bool(sep), 'Invalid .env line')
                values[key.strip()] = value.strip()
    keys = ['BASE_URL', 'MODEL', 'API_KEY', 'CONTEXT_LIMIT', 'MAX_OUTPUT', 'TIMEOUT']
    defaults = ['http://127.0.0.1:6996/v1', 'qwen3.8-27b|IQ3_XXS|10934860704', '', '16384', '4096', '180']
    result = {k: os.environ.get('LOCAL_LLM_' + k, values.get('LOCAL_LLM_' + k, d))
              for k, d in zip(keys, defaults)}
    result['REASONING_EFFORT'] = os.environ.get('LOCAL_LLM_REASONING_EFFORT',
                                               values.get('LOCAL_LLM_REASONING_EFFORT', ''))
    require(result['REASONING_EFFORT'] in ('', 'off', 'low', 'medium', 'high', 'xhigh'),
            'Unsupported LOCAL_LLM_REASONING_EFFORT')
    url = urlparse(result['BASE_URL'])
    require(url.scheme == 'http' and url.hostname in ('127.0.0.1', 'localhost', '::1')
            and not url.username and not url.password and not url.query and not url.fragment,
            'Local executor only accepts a loopback HTTP endpoint')
    for key in ('CONTEXT_LIMIT', 'MAX_OUTPUT', 'TIMEOUT'):
        result[key] = int(result[key])
        require(result[key] > 0, f'{key} must be positive')
    require(result['MAX_OUTPUT'] < result['CONTEXT_LIMIT'], 'Output must fit context')
    return result


def build_context(task, worktree, feedback):
    files = {}
    # Explicit context includes existing implementation. New worker-written files are
    # included on retries by the caller, never recursively read unrelated directories.
    for name in task['context']:
        p = safe_path(worktree, name)
        require(p.is_file(), f'Missing context file: {name}')
        require(p.stat().st_size <= 32000, f'Context file too large: {name}')
        files[name] = p.read_text(encoding='utf-8')
    return {'task': task, 'files': files, 'feedback': feedback}


class AgentExecutor:
    def run(self, task, context):
        raise NotImplementedError


class CloudExecutor(AgentExecutor):
    def run(self, task, context):
        return {'mode': 'manual_cloud_handoff', 'task': task['id'], 'context': context,
                'instruction': 'Lead works in the assigned worktree, then validates and requests review.'}


class LocalExecutor(AgentExecutor):
    def __init__(self, settings, system_prompt, log_dir):
        self.settings, self.system_prompt, self.log_dir = settings, system_prompt, Path(log_dir)

    def run(self, task, context):
        c = self.settings
        user = json.dumps(context, ensure_ascii=False)
        # Conservative byte ceiling: no tokenizer dependency, reserve chat framing/output.
        budget = c['CONTEXT_LIMIT'] - c['MAX_OUTPUT'] - 512
        require(len((self.system_prompt + user).encode('utf-8')) <= budget,
                'Bounded context byte budget exceeded; narrow task/context or explicitly reconfigure')
        payload = {'model': c['MODEL'], 'messages': [
            {'role': 'system', 'content': self.system_prompt}, {'role': 'user', 'content': user}],
            'max_tokens': c['MAX_OUTPUT'], 'stream': False, 'temperature': 0.2}
        if c.get('REASONING_EFFORT'):
            payload['reasoning_effort'] = c['REASONING_EFFORT']
        self.log_dir.mkdir(parents=True, exist_ok=True)
        (self.log_dir / 'request.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
        headers = {'Content-Type': 'application/json'}
        if c['API_KEY']:
            headers['Authorization'] = 'Bearer ' + c['API_KEY']
        req = urllib.request.Request(c['BASE_URL'].rstrip('/') + '/chat/completions',
                                     data=json.dumps(payload).encode(), headers=headers, method='POST')
        # Bypass machine proxies; reject redirects to avoid leaking context or credentials.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        started = time.monotonic()
        with opener.open(req, timeout=c['TIMEOUT']) as response:
            raw = response.read(2_000_001)
        require(len(raw) <= 2_000_000, 'Response exceeds 2 MB')
        data = json.loads(raw)
        (self.log_dir / 'response.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        elapsed = time.monotonic() - started
        choice = data['choices'][0]
        require(choice.get('finish_reason') == 'stop', 'Model response incomplete; inspect response log')
        text = choice['message'].get('content', '')
        # One fenced object is tolerated; no recovery from truncated/ambiguous JSON.
        if text.strip().startswith('```'):
            lines = text.strip().splitlines()
            require(lines[-1] == '```', 'Unclosed JSON fence')
            text = '\n'.join(lines[1:-1])
        result = json.loads(text)
        return {'proposal': result, 'model': data.get('model', c['MODEL']),
                'elapsed_seconds': round(elapsed, 3), 'usage': data.get('usage'),
                'timings': data.get('timings'), 'system_fingerprint': data.get('system_fingerprint')}


def apply_proposal(task, worktree, proposal):
    require(isinstance(proposal, dict), 'Expected JSON object')
    require(not proposal.get('blocker'), f"Worker blocked: {proposal.get('blocker')}")
    require(isinstance(proposal.get('handoff'), dict), 'Missing worker handoff')
    files = proposal.get('files')
    require(isinstance(files, list) and 1 <= len(files) <= 12, 'Expected 1..12 file replacements')
    pending, seen = [], set()
    for item in files:
        require(isinstance(item, dict) and set(item) == {'path', 'content'}, 'Invalid file envelope')
        name, content = item['path'], item['content']
        check_scope(task, [name])
        require(name.lower() not in seen, f'Duplicate path: {name}')
        seen.add(name.lower())
        require(isinstance(content, str) and len(content.encode('utf-8')) <= 100000
                and '\x00' not in content, f'Invalid/oversized text: {name}')
        p = safe_path(worktree, name)
        require(not p.exists() or p.is_file(), f'Not a regular file: {name}')
        pending.append((p, content))
    require(sum(len(v.encode('utf-8')) for _, v in pending) <= 250000, 'Proposal exceeds 250 KB')
    # All authorization/path/content checks complete before the first write.
    for path, content in pending:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8', newline='\n')
    return [item['path'] for item in files]
