import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2] / 'apps' / 'web' / 'owner'
HTML = (ROOT / 'index.html').read_text(encoding='utf-8')
JS = (ROOT / 'app.js').read_text(encoding='utf-8')
CSS = (ROOT / 'style.css').read_text(encoding='utf-8')

class OwnerContractTests(unittest.TestCase):
    def test_session_routes_and_browser_credentials(self):
        for route in ("'/api/v1/admin/session'", "'POST'", "'GET'", "'DELETE'"):
            self.assertIn(route, JS)
        self.assertIn("credentials: 'include'", JS)
        self.assertIn("'X-Requested-With': 'crm'", JS)
        self.assertIn("'X-CSRF-Token': csrf", JS)

    def test_csrf_is_runtime_only_and_no_public_shell_changes(self):
        self.assertIn('csrfToken', JS)
        self.assertNotIn('localStorage', JS)
        self.assertNotIn('sessionStorage', JS)
        self.assertIn('csrf_token', JS)

    def test_states_and_accessibility(self):
        self.assertIn('status === 401', JS)
        self.assertIn('временно недоступен', JS)
        self.assertIn('aria-live', HTML + JS)
        self.assertIn('for="login"', JS)
        self.assertIn('Заявки', JS)
        self.assertIn('Календарь', JS)
        self.assertIn(':focus-visible', CSS)
        self.assertIn('@media', CSS)

if __name__ == '__main__':
    unittest.main()
