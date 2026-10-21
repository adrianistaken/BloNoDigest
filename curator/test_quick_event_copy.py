from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Event, Region


class QuickEventCopyTests(TestCase):
    def setUp(self):
        self.region = Region.objects.create(name='Local', slug='bloomington-normal', timezone='America/Chicago')
        self.user = User.objects.create_user('copy-editor', is_staff=True)
        self.client.force_login(self.user)
        self.url = '/admin-dashboard/events/new/quick/choices/'

    def event(self, **kwargs):
        return Event.objects.create(**{
            'region': self.region, 'canonical_title': 'Original event',
            'starts_at': timezone.now() - timedelta(days=1), **kwargs,
        })

    def test_staff_get_only(self):
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.user.is_staff = False
        self.user.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_copy_and_create_keeps_original(self):
        original = self.event(editorial_title='Newsletter title', editorial_description='Edited description',
                              editorial_time='Evening', editorial_location='Park', editorial_price='Free',
                              source_url='https://example.com/event')
        fields = self.client.get(self.url).json()['events'][0]['fields']
        self.assertEqual(fields['editorial_title'], 'Newsletter title')
        self.assertEqual(fields['editorial_description'], 'Edited description')
        self.assertEqual(fields['editorial_time'], 'Evening')
        self.assertEqual(fields['editorial_location'], 'Park')
        self.assertEqual(fields['editorial_price'], 'Free')
        self.assertEqual(fields['source_url'], original.source_url)
        self.assertEqual(Event.objects.count(), 1)
        fields['start_date'] = '2030-07-12'
        self.assertEqual(self.client.post('/admin-dashboard/events/new/quick/', fields).status_code, 302)
        self.assertEqual(Event.objects.count(), 2)
        original.refresh_from_db()
        self.assertEqual(original.canonical_title, 'Original event')
        self.assertEqual(original.status, Event.Status.NEEDS_REVIEW)
        self.assertEqual(Event.objects.exclude(pk=original.pk).get().status, Event.Status.APPROVED)

    def test_week_local_boundaries_and_filters(self):
        ct = ZoneInfo('America/Chicago')
        start = datetime(2026, 10, 19, tzinfo=ct)
        self.event(starts_at=start - timedelta(seconds=1))
        wanted = self.event(starts_at=start, status=Event.Status.APPROVED, editorial_title='Autumn fair')
        last = self.event(starts_at=start + timedelta(days=7, seconds=-1))
        self.event(starts_at=start + timedelta(days=7))
        params = {'week': '2026-10-21'}
        response = self.client.get(self.url, params).json()
        self.assertEqual([e['id'] for e in response['events']], [last.pk, wanted.pk])
        self.assertEqual(response['events'][1]['fields']['start_date'], '2026-10-19')
        self.assertEqual(response['events'][1]['fields']['editorial_time'], '12:00 AM')
        self.assertEqual(self.client.get(self.url, {**params, 'status': 'approved', 'q': 'Autumn'}).json()['count'], 1)
        self.assertEqual(self.client.get(self.url, {**params, 'status': 'not_approved'}).json()['count'], 1)
        self.assertEqual(self.client.get(self.url, {'week': 'bad'}).status_code, 400)
        self.assertEqual(self.client.get(self.url, {'status': 'bad'}).status_code, 400)

    def test_recent_pagination_region_and_source_fallbacks(self):
        for i in range(21):
            self.event(starts_at=timezone.now() - timedelta(days=i + 1), description='Source description',
                       time_is_known=False, venue_name='Library', price_text='$5')
        self.event(starts_at=timezone.now() + timedelta(days=1))
        other = Region.objects.create(name='Other', slug='other')
        self.event(region=other)
        data = self.client.get(self.url).json()
        self.assertEqual(data['count'], 21)
        self.assertEqual(len(data['events']), 20)
        fields = data['events'][0]['fields']
        self.assertEqual(fields['editorial_description'], 'Source description')
        self.assertEqual(fields['editorial_title'], 'Original event')
        self.assertEqual(fields['editorial_time'], '')
        self.assertEqual(fields['editorial_location'], 'Library')
        self.assertEqual(fields['editorial_price'], '$5')
        self.assertEqual(len(self.client.get(self.url, {'page': 2}).json()['events']), 1)
        self.assertEqual(self.client.get(self.url, {'q': 'absent'}).json()['count'], 0)
