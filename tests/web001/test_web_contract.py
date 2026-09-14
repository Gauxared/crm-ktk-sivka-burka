from pathlib import Path
import unittest

ROOT = Path(__file__).parents[2]
HTML = (ROOT / 'apps/web/index.html').read_text(encoding='utf-8')
JS = (ROOT / 'apps/web/script.js').read_text(encoding='utf-8')

class WebContractTests(unittest.TestCase):
    def test_public_protocol_and_fields(self):
        for s in ("API+'catalog'", "API+'submission-tokens'", "API+'inquiries'",'Idempotency-Key','submission_token','participants_count','service_option_id','utm_campaign'):
            self.assertIn(s, JS)
        for s in ('service_option_id','name','contact_kind','contact_value','participants_count','date','experience','comment'):
            self.assertIn(f'name="{s}"', HTML)

    def test_envelopes_nested_options_and_retry_identity(self):
        for s in ('apiData', 'payload.data', 'services.flatMap', 'service.options', 'submission_token', 'receipt.received', 'idempotencyKey'):
            self.assertIn(s, JS)
        self.assertIn("catalog?.catalog_version", JS)
        self.assertIn("o.id", JS)

    def test_catalog_driven_rules_and_location(self):
        for s in ('visit_rules', 'location_link', 'Правила визита', 'Открыть расположение'):
            self.assertIn(s, JS)
        self.assertIn('id="location"', HTML)

    def test_required_non_gate_information(self):
        for s in ('инструктаж','новичк','дет','галоп','110 кг','не автоматический'):
            self.assertIn(s.lower(), HTML.lower())

    def test_no_hardcoded_commercial_catalog(self):
        self.assertNotIn('S-01', HTML + JS)
        self.assertNotIn('3 500 ₽', HTML + JS)
        self.assertNotIn('5 000 ₽', HTML + JS)

if __name__ == '__main__': unittest.main()
