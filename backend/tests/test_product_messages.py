import unittest
from unittest.mock import AsyncMock

from app.api.routes import atlassian, google_drive
from app.services.atlassian_service import AtlassianOAuthError
from app.services.google_drive_service import GoogleDriveOAuthError


class ProductMessagesTests(unittest.IsolatedAsyncioTestCase):
    async def test_provider_error_descriptions_are_not_rendered(self):
        for callback, support in [
            (atlassian.callback, 'MI-ATL-003'),
            (google_drive.oauth_callback, 'MI-DRV-003'),
        ]:
            response = await callback(None, error='access_denied', error_description='secret provider details')
            content = response.body.decode()
            self.assertIn(support, content)
            self.assertNotIn('secret provider details', content)
            self.assertNotIn('access_denied', content)

    async def test_signin_exceptions_have_safe_coded_pages(self):
        for callback, error, support in [
            (atlassian.callback, AtlassianOAuthError, 'MI-ATL-002'),
            (google_drive.oauth_callback, GoogleDriveOAuthError, 'MI-DRV-002'),
        ]:
            service = AsyncMock()
            service.complete_authorization.side_effect = error('secret token diagnostics')
            response = await callback(service, code='test', state='test')
            content = response.body.decode()
            self.assertIn(support, content)
            self.assertNotIn('secret token diagnostics', content)

    def test_api_errors_preserve_machine_codes_and_add_safe_support_codes(self):
        for mapper, error, code in [
            (atlassian._http_error, AtlassianOAuthError, 'atlassian_error'),
            (google_drive._http_error, GoogleDriveOAuthError, 'google_drive_oauth_error'),
        ]:
            result = mapper(error('secret provider details'))
            self.assertEqual(result.detail['code'], code)
            self.assertRegex(result.detail['support_code'], r'^MI-[A-Z]+-\d{3}$')
            self.assertIn(result.detail['support_code'], result.detail['message'])
            self.assertNotIn('secret provider details', result.detail['message'])
