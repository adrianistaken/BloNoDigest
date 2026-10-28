import base64
import json
from io import BytesIO
from unittest.mock import Mock, patch

import requests
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from PIL import Image

from .ai import AIShorteningError, DRAFT_FIELDS, draft_quick_event
from .forms import QuickEventDraftForm
from .models import Event, Region


@override_settings(OPENAI_API_KEY='test-key', OPENAI_EVENT_SHORTEN_INSTRUCTIONS='Use our newsletter voice.')
class QuickEventAITests(TestCase):
    def setUp(self):
        Region.objects.create(name='Bloomington-Normal', slug='bloomington-normal', timezone='America/Chicago')
        self.url = '/admin-dashboard/ai/quick-event/'
        self.user = get_user_model().objects.create_user('draft-editor', is_staff=True)
        self.fields = dict.fromkeys(DRAFT_FIELDS, '')
        self.fields.update(start_date='2026-10-24', editorial_title='Art Fair')

    def screenshot(self, image_format='PNG', **save_options):
        data = BytesIO()
        Image.new('RGB', (120, 80), 'white').save(data, format=image_format, **save_options)
        return SimpleUploadedFile('screenshot.' + image_format.lower(), data.getvalue(),
                                  content_type=QuickEventDraftForm.IMAGE_TYPES.get(image_format, 'image/png'))

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
        self.assertEqual(post.call_args.kwargs['json']['input'], 'Art Fair')

    @patch('curator.ai.requests.post')
    def test_screenshot_only_and_with_text_extracts_editable_fields_without_saving(self, post):
        self.client.force_login(self.user)
        fields = {**self.fields, 'editorial_time': '2–5 PM',
                  'editorial_location': 'Library', 'editorial_price': '$5'}
        post.return_value = Mock(json=lambda: {'output_text': json.dumps({**fields, 'warnings': []})})
        count = Event.objects.count()
        for image_format in ['PNG', 'JPEG', 'WEBP']:
            for text in ['', 'Additional event details']:
                with self.subTest(image_format=image_format, text=text):
                    upload = self.screenshot(image_format)
                    expected_data = upload.read()
                    upload.seek(0)
                    # The server must detect the format instead of trusting a MIME header.
                    upload.content_type = 'application/octet-stream'
                    response = self.client.post(self.url, {'source_text': text, 'source_image': upload})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['fields'], {**fields, 'editorial_time': '2-5pm'})
                    payload = post.call_args.kwargs['json']
                    self.assertFalse(payload['store'])
                    content = payload['input'][0]['content']
                    self.assertEqual(content[0]['type'], 'input_text')
                    self.assertTrue(content[0]['text'])
                    if text:
                        self.assertEqual(content[0]['text'], text)
                    self.assertEqual(content[1]['type'], 'input_image')
                    self.assertEqual(content[1]['detail'], 'high')
                    prefix, encoded = content[1]['image_url'].split(',', 1)
                    self.assertEqual(prefix, f'data:{QuickEventDraftForm.IMAGE_TYPES[image_format]};base64')
                    self.assertEqual(base64.b64decode(encoded), expected_data)
                    self.assertNotIn('editorial_description', response.json()['fields'])
        self.assertEqual(post.call_count, 6)
        self.assertEqual(Event.objects.count(), count)

    @patch('curator.dashboard_views.draft_quick_event')
    def test_bad_images_are_rejected_before_ai_even_with_valid_text(self, draft):
        self.client.force_login(self.user)
        jpeg = self.screenshot('JPEG').read()
        uploads = [
            (SimpleUploadedFile('empty.png', b'', content_type='image/png'), 'empty'),
            (SimpleUploadedFile('fake.png', b'not an image', content_type='image/png'), 'readable'),
            (SimpleUploadedFile('truncated.jpg', jpeg[:-30], content_type='image/jpeg'), 'readable'),
            (self.screenshot('BMP'), 'PNG, JPEG, or WebP'),
            (SimpleUploadedFile('large.png', b'x' * (QuickEventDraftForm.MAX_IMAGE_BYTES + 1),
                                content_type='image/png'), '10 MB'),
            (self.screenshot(save_all=True, append_images=[Image.new('RGB', (120, 80), 'black')]), 'still'),
        ]
        for upload, error in uploads:
            with self.subTest(name=upload.name, error=error):
                response = self.client.post(self.url, {'source_text': 'Art Fair', 'source_image': upload})
                self.assertEqual(response.status_code, 400)
                self.assertIn(error, response.json()['error'])
        with patch.object(QuickEventDraftForm, 'MAX_IMAGE_PIXELS', 100):
            response = self.client.post(self.url, {'source_image': self.screenshot()})
            self.assertEqual(response.status_code, 400)
            self.assertIn('Crop', response.json()['error'])
        response = self.client.post(self.url, {'source_image': [self.screenshot(), self.screenshot()]})
        self.assertEqual(response.status_code, 400)
        self.assertIn('one screenshot', response.json()['error'])
        response = self.client.post(self.url, {'source_text': 'x' * 20001, 'source_image': self.screenshot()})
        self.assertEqual(response.status_code, 400)
        draft.assert_not_called()

    @patch('curator.dashboard_views.draft_quick_event')
    def test_screenshot_endpoint_requires_staff_and_csrf(self, draft):
        self.user.is_staff = False
        self.user.save()
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {'source_image': self.screenshot()}).status_code, 302)
        self.user.is_staff = True
        self.user.save()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self.assertEqual(client.post(self.url, {'source_image': self.screenshot()}).status_code, 403)
        draft.assert_not_called()

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
