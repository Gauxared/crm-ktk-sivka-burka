import json
from pathlib import Path
import subprocess
import unittest


APP = Path(__file__).parents[2] / 'apps/web/owner/app.js'


class CalendarTimezoneRuntime(unittest.TestCase):
    def test_actual_calendar_code_builds_dst_safe_utc_bounds(self):
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ending = '  boot();\n}());\n';
if (!source.endsWith(ending)) throw new Error('Unexpected app ending');
const instrumented = source.slice(0, -ending.length) +
  '  globalThis.__calendarTest = { startOfClubDate, monthBounds };\n}());\n';
const context = {
  document: { querySelector: () => null }, Intl, Date, Map, Set, URLSearchParams
};
vm.runInNewContext(instrumented, context);
const api = context.__calendarTest;
const result = {
  bangkok: api.startOfClubDate(2026, 9, 1, 'Asia/Bangkok'),
  berlin: api.monthBounds(2026, 3, 'Europe/Berlin')
};
process.stdout.write(JSON.stringify(result));
"""
        result = subprocess.run(
            ['node', '-e', harness, str(APP)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual(payload['bangkok'], '2026-08-31T17:00:00.000Z')
        self.assertEqual(payload['berlin']['from'], '2026-02-28T23:00:00.000Z')
        self.assertEqual(payload['berlin']['to'], '2026-03-31T22:00:00.000Z')

    def test_actual_calendar_code_follows_every_bound_cursor(self):
        harness = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const ending = '  boot();\n}());\n';
const instrumented = source.slice(0, -ending.length) +
  '  globalThis.__calendarTest = { visitPages };\n}());\n';
const urls = [];
const pages = [
  { items: [{ id: 'first' }], next_cursor: 'cursor-one' },
  { items: [{ id: 'second' }], next_cursor: 'cursor-two' },
  { items: [{ id: 'third' }], next_cursor: null }
];
const context = {
  document: { querySelector: () => null }, Intl, Date, Map, Set, URLSearchParams,
  fetch: async url => {
    urls.push(String(url));
    const page = pages.shift();
    return { ok: true, json: async () => ({ data: page }) };
  }
};
vm.runInNewContext(instrumented, context);
context.__calendarTest.visitPages({
  from: '2026-09-01T00:00:00.000Z', to: '2026-10-01T00:00:00.000Z'
}).then(items => process.stdout.write(JSON.stringify({ urls, items }))).catch(error => {
  process.stderr.write(String(error)); process.exit(1);
});
"""
        result = subprocess.run(
            ['node', '-e', harness, str(APP)],
            capture_output=True,
            text=True,
            encoding='utf-8',
            check=True,
        )
        payload = json.loads(result.stdout)
        self.assertEqual([item['id'] for item in payload['items']], ['first', 'second', 'third'])
        self.assertEqual(len(payload['urls']), 3)
        self.assertNotIn('cursor=', payload['urls'][0])
        self.assertIn('cursor=cursor-one', payload['urls'][1])
        self.assertIn('cursor=cursor-two', payload['urls'][2])
        for url in payload['urls']:
            self.assertIn('from=2026-09-01T00%3A00%3A00.000Z', url)
            self.assertIn('to=2026-10-01T00%3A00%3A00.000Z', url)
            self.assertIn('limit=100', url)


if __name__ == '__main__':
    unittest.main()
