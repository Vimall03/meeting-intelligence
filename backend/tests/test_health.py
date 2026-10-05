import unittest
from contextlib import ExitStack
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.main import app
from app.core.config import settings


class HealthTests(unittest.TestCase):
    def test_liveness_needs_no_auth_or_aws(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/').json(), {'message': 'healthy'})
            result = client.get('/health/live')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json(), {'status': 'ok'})

    def test_readiness_reports_missing_configuration_without_secrets(self):
        with patch.object(settings, 'auth_table_name', ''), TestClient(app) as client:
            result = client.get('/health/ready')
            self.assertEqual(result.status_code, 503)
            self.assertEqual(result.json()['detail']['support_code'], 'MI-CON-009')
            self.assertNotIn('auth_table_name', result.text)

    def test_readiness_and_drain(self):
        with ExitStack() as stack:
            for name in ('aws_region', 'auth_user_pool_id', 'auth_client_id', 'auth_table_name',
                'integration_key_id', 'extension_origin', 'openrouter_api_key'):
                stack.enter_context(patch.object(settings, name, 'configured'))
            client = stack.enter_context(TestClient(app))
            self.assertEqual(client.get('/health/ready').json(), {'status': 'ready'})
            app.state.meetings.draining = True
            self.assertEqual(client.get('/health/ready').status_code, 503)
