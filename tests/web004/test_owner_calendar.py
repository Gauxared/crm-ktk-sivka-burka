import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[2]
JS = (ROOT / 'apps/web/owner/app.js').read_text(encoding='utf-8')
CSS = (ROOT / 'apps/web/owner/style.css').read_text(encoding='utf-8')


class OwnerCalendarContract(unittest.TestCase):
    def test_uses_club_timezone_for_explicit_utc_month_bounds(self):
        self.assertIn("'/api/v1/public/catalog'", JS)
        self.assertIn('club_timezone', JS)
        self.assertIn('startOfClubDate', JS)
        self.assertIn("params.set('from', bounds.from)", JS)
        self.assertIn("params.set('to', bounds.to)", JS)
        self.assertNotIn('getTimezoneOffset', JS)
        self.assertIn('Календарь временно недоступен', JS)

    def test_reads_every_calendar_page_without_a_day_limit(self):
        self.assertIn("'/api/v1/admin/visits?'", JS)
        self.assertIn("params.set('limit', '100')", JS)
        self.assertIn('next_cursor', JS)
        self.assertIn('seen.has(next)', JS)
        self.assertIn('} while(cursor);', JS)
        self.assertIn('dayVisits.map(visit => visitCard', JS)
        self.assertNotIn('dayVisits.slice', JS)
        self.assertIn('overflow: visible', CSS)
        self.assertNotIn('max-height', CSS)

    def test_month_navigation_and_group_detail_are_read_only(self):
        for text in ('Предыдущий месяц', 'Следующий месяц', 'Текущий месяц', '← К календарю'):
            self.assertIn(text, JS)
        self.assertIn("href=\"#inquiries\"", JS)
        self.assertIn("href=\"#calendar\"", JS)
        self.assertIn("'/api/v1/admin/visits/' + encodeURIComponent(id)", JS)
        self.assertIn('participations.map(participationCard)', JS)
        self.assertIn('Заявки в общей прогулке', JS)
        self.assertNotIn("method: 'POST',\n      credentials", JS)

    def test_safe_accessible_states_and_responsive_cells(self):
        for text in ('Загрузка прогулок', 'прогулок нет', 'Сессия истекла', 'Недостаточно прав'):
            self.assertIn(text, JS)
        self.assertIn('aria-label="Навигация по месяцам"', JS)
        self.assertIn('aria-live="polite"', JS)
        self.assertIn('.calendar-day', CSS)
        self.assertIn('min-height: 170px', CSS)
        self.assertIn('@media (max-width: 560px)', CSS)
        self.assertNotIn('console.', JS)
        self.assertNotIn('localStorage', JS)


if __name__ == '__main__':
    unittest.main()
