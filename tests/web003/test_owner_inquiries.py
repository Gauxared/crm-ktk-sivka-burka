import pathlib, unittest
ROOT=pathlib.Path(__file__).parents[2]
JS=(ROOT/'apps/web/owner/app.js').read_text(encoding='utf-8')
CSS=(ROOT/'apps/web/owner/style.css').read_text(encoding='utf-8')
class Contract(unittest.TestCase):
 def test_queries(self):
  self.assertIn('/api/v1/admin/inquiries?',JS); self.assertIn('s.cursor=null',JS); self.assertIn('next_cursor',JS)
 def test_states(self):
  for x in ('Загрузка','Заявок нет','Недостаточно прав','Сессия истекла','К списку'): self.assertIn(x,JS)
  self.assertIn('role="alert"',JS); self.assertIn('encodeURIComponent(id)',JS)
 def test_security_layout(self):
  self.assertIn("credentials:'include'",JS); self.assertNotIn('console.',JS); self.assertIn('overflow-wrap:anywhere',CSS); self.assertIn(':focus-visible',CSS)
