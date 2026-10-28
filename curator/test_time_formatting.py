import json
from datetime import date, datetime, time
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from .ai import DRAFT_FIELDS, draft_quick_event
from .emails import render_digest
from .forms import EventCopyForm
from .models import DigestEvent, DigestIssue, Event, Region
from .time_formatting import format_clock, normalize_event_time


EXAMPLES = {
    '5:00 PM - 8:00 PM': '5-8pm',
    '10:00 AM–2:00 PM': '10am-2pm',
    '2:00 PM': '2pm',
    '10:30 AM – 11:30 AM': '10:30-11:30am',
    '10:30 AM-5:00 PM': '10:30am-5pm',
}


class TimeFormattingTests(SimpleTestCase):
    def test_requested_style_and_idempotence(self):
        for source, expected in EXAMPLES.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_event_time(source), expected)
                self.assertEqual(normalize_event_time(expected), expected)

    def test_variants_and_editorial_context(self):
        cases = {
            ' 05:00 p.m. — 08:00 p.m. ': '5-8pm',
            '10 a.m. to 2 p.m.': '10am-2pm',
            '10:30–11:30 AM': '10:30-11:30am',
            'Doors 6:00 PM; music 7:30 PM': 'Doors 6pm; music 7:30pm',
            'Saturday, 6–8 PM CDT': 'Saturday, 6-8pm CDT',
            '11:30 PM–1:00 AM': '11:30pm-1am',
            '9:00 AM - 12:00 PM': '9am-12pm',
            'Noon until Midnight': '12pm-12am',
            '17:00–20:00': '5-8pm',
            '10:00-14:00': '10am-2pm',
            '17:00-8 PM': '5-8pm',
            '00:30': '12:30am',
            '14:00': '2pm',
            '5:00 PM CST - 6:00 PM EST': '5pm CST - 6pm EST',
            '5pm.': '5pm.',
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_event_time(source), expected)
                self.assertEqual(normalize_event_time(expected), expected)

    def test_does_not_invent_times_or_change_other_numbers(self):
        for source in ['', '-', 'Time TBD', 'All day', 'Evening', '5-8', '10:30',
                       '9:00-11:00', '13pm', '25:00', '5:60 PM', 'July 12-14', 'Ages 5-8']:
            with self.subTest(source=source):
                self.assertEqual(normalize_event_time(source), source)

    def test_known_clock_values(self):
        for hour, minute, expected in [(0, 0, '12am'), (12, 0, '12pm'), (14, 0, '2pm'),
                                       (10, 30, '10:30am'), (17, 5, '5:05pm')]:
            self.assertEqual(format_clock(time(hour, minute)), expected)

    @override_settings(OPENAI_API_KEY='test-key')
    @patch('curator.ai.requests.post')
    def test_ai_results_are_normalized_even_when_the_model_uses_another_style(self, post):
        for source, expected in EXAMPLES.items():
            fields = {**dict.fromkeys(DRAFT_FIELDS, ''), 'editorial_time': source}
            post.return_value = Mock(json=lambda: {'output_text': json.dumps({**fields, 'warnings': []})})
            result = draft_quick_event('Event details', timezone_name='America/Chicago', today='2030-07-12')
            self.assertEqual(result['fields']['editorial_time'], expected)


class EventTimeIntegrationTests(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name='Local', slug='bloomington-normal')
        self.client.force_login(get_user_model().objects.create_user('time-editor', is_staff=True))
        self.event = Event.objects.create(
            region=self.region, canonical_title='Art fair',
            starts_at=datetime(2030, 7, 12, 17, tzinfo=ZoneInfo('America/Chicago')),
        )
        self.issue = DigestIssue.objects.create(
            region=self.region, title='Weekend', target_start_date=date(2030, 7, 12),
            target_end_date=date(2030, 7, 14),
        )
        self.entry = DigestEvent.objects.create(digest_issue=self.issue, event=self.event, section='top_picks')

    def test_quick_creation_saves_all_examples_in_the_requested_style(self):
        for source, expected in EXAMPLES.items():
            response = self.client.post('/admin-dashboard/events/new/quick/', {
                'start_date': '2030-07-12', 'editorial_title': source, 'editorial_time': source,
            })
            self.assertEqual(response.status_code, 302)
            event = Event.objects.get(editorial_title=source)
            self.assertEqual(event.editorial_time, expected)
            self.assertEqual(event.display_time, expected)
            self.assertContains(self.client.get(response.url), expected)

    def test_existing_data_is_formatted_in_forms_pages_emails_and_copied_events(self):
        # Simulate records stored before the formatter existed, bypassing save().
        Event.objects.filter(pk=self.event.pk).update(editorial_time='10:30 AM – 11:30 AM')
        self.event.refresh_from_db()
        self.assertEqual(EventCopyForm(instance=self.event).initial['editorial_time'], '10:30-11:30am')
        self.assertEqual(self.event.display_time, '10:30-11:30am')
        for url in [f'/admin-dashboard/events/{self.event.pk}/',
                    '/admin-dashboard/events/?filter=&status=',
                    f'/admin-dashboard/copy-desk/?event={self.event.pk}']:
            self.assertContains(self.client.get(url), '10:30-11:30am')
        copied = self.client.get('/admin-dashboard/events/new/quick/choices/', {'week': '2030-07-12'}).json()
        self.assertEqual(copied['events'][0]['fields']['editorial_time'], '10:30-11:30am')
        html, text = render_digest(self.issue, '#')
        self.assertIn('10:30-11:30am', html)
        self.assertIn('10:30-11:30am', text)

    def test_copy_desk_save_returns_the_formatted_value(self):
        response = self.client.post('/admin-dashboard/copy-desk/', {
            'event_id': self.event.pk,
            f'event-{self.event.pk}-editorial_time': '10:30 AM to 5:00 PM',
        }, HTTP_X_REQUESTED_WITH='XMLHttpRequest')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['editorial_time'], '10:30am-5pm')
        self.event.refresh_from_db()
        self.assertEqual(self.event.editorial_time, '10:30am-5pm')

    def test_model_saves_and_legacy_digest_overrides_use_the_same_formatter(self):
        self.event.editorial_time = '5:00 PM–8:00 PM'
        self.event.save(update_fields=['editorial_time'])
        self.event.refresh_from_db()
        self.assertEqual(self.event.editorial_time, '5-8pm')
        self.entry.custom_time = '10 AM–2 PM'
        self.entry.save(update_fields=['custom_time'])
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.custom_time, '10am-2pm')
        DigestEvent.objects.filter(pk=self.entry.pk).update(custom_time='2:00 PM')
        self.entry.refresh_from_db()
        self.assertEqual(self.entry.display_time, '2pm')
        self.entry.custom_time = '-'
        self.assertEqual(self.entry.display_time, '')

    def test_imported_manual_unknown_and_hidden_times(self):
        with timezone.override('America/Chicago'):
            self.assertEqual(self.event.display_time, '5pm')
            self.assertEqual(self.entry.display_time, '5pm')
        self.event.time_is_known = False
        self.assertEqual(self.event.display_time, '')
        self.event.editorial_time = '2:00 PM'
        self.assertEqual(self.event.display_time, '2pm')
        self.event.editorial_time = '-'
        self.assertEqual(self.event.display_time, '')
        response = self.client.post('/admin-dashboard/events/new/', {
            'canonical_title': 'Manual event', 'start_date': '2030-07-12',
            'start_time': '10:30', 'status': 'approved',
        })
        self.assertEqual(response.status_code, 302)
        with timezone.override('America/Chicago'):
            self.assertEqual(Event.objects.get(canonical_title='Manual event').display_time, '10:30am')
