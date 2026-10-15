import json
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from .ai import AIShorteningError, DRAFT_FIELDS, draft_quick_event
from .models import Event, Region


@override_settings(OPENAI_API_KEY='test-key', OPENAI_EVENT_SHORTEN_INSTRUCTIONS='Use our newsletter voice.')
class QuickEventAITests(TestCase):
    def setUp(self):
        Region.objects.create(name='Bloomington-Normal', slug='bloomington-normal', timezone='America/Chicago')
        self.url = '/admin-dashboard/ai/quick-event/'
        self.user = get_user_model().objects.create_user('draft-editor', is_staff=True)
        self.fields = dict.fromkeys(DRAFT_FIELDS, '')
        self.fields.update(start_date='2026-10-24', editorial_title='Art Fair')

    def test_requires_staff_and_post(self):
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 405)

    @patch('curator.dashboard_views.draft_quick_event')
    def test_validation_and_draft_does_not_save(self, draft):
        self.client.force_login(self.user)
        for text in ['', 'x' * 20001]:
            self.assertEqual(self.client.post(self.url, {'source_text': text}).status_code, 400)
        draft.assert_not_called()
        draft.return_value = {'fields': self.fields, 'warnings': [], 'usage': {}}
        count = Event.objects.count()
        response = self.client.post(self.url, {'source_text': 'Art Fair October 24, 2026'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['fields'], self.fields)
        self.assertEqual(Event.objects.count(), count)
        draft.side_effect = AIShorteningError('Try again.')
        self.assertEqual(self.client.post(self.url, {'source_text': 'Art Fair'}).status_code, 503)

    @patch('curator.ai.requests.post')
    def test_single_request_extracts_details_only_and_returns_usage(self, post):
        post.return_value = Mock(json=lambda: {'output': [{'content': [
            {'type': 'output_text', 'text': json.dumps({**self.fields, 'warnings': []})}
        ]}], 'usage': {'input_tokens': 2000, 'output_tokens': 300, 'total_tokens': 2300}})
        result = draft_quick_event('Art Fair', timezone_name='America/Chicago', today='2026-10-14')
        self.assertEqual(result['fields'], self.fields)
        self.assertEqual(result['usage']['total_tokens'], 2300)
        post.assert_called_once()
        self.assertNotIn('Use our newsletter voice.', post.call_args.kwargs['json']['instructions'])
        self.assertNotIn('editorial_description', result['fields'])
        schema = post.call_args.kwargs['json']['text']['format']['schema']
        self.assertNotIn('editorial_description', schema['properties'])
        self.assertTrue(post.call_args.kwargs['json']['text']['format']['strict'])

    @patch('curator.ai.requests.post')
    def test_invalid_dates_cleared_and_bad_responses_rejected(self, post):
        post.return_value = Mock(json=lambda: {'output_text': json.dumps({**self.fields, 'start_date': '2026-02-30', 'warnings': []})})
        result = draft_quick_event('Fair', timezone_name='America/Chicago', today='2026-10-14')
        self.assertEqual(result['fields']['start_date'], '')
        self.assertTrue(result['warnings'])
        for payload in [{'output_text': '{}'}, {'output_text': 'not json'}, {'status': 'incomplete'}]:
            post.return_value = Mock(json=lambda: payload)
            with self.assertRaises(AIShorteningError):
                draft_quick_event('Fair', timezone_name='America/Chicago', today='2026-10-14')
        post.side_effect = requests.Timeout()
        with self.assertRaisesMessage(AIShorteningError, 'timed out'):
            draft_quick_event('Fair', timezone_name='America/Chicago', today='2026-10-14')
