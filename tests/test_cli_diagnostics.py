import unittest

from acquire_scraper.cli import catalog_http_message
from acquire_scraper.http import HttpError


class CliDiagnosticsTests(unittest.TestCase):
    def test_catalog_404_explains_hidden_admin_auth_failure(self):
        message = catalog_http_message(
            'preflight/request',
            'https://catalog.example/api/admin/catalog/ingest',
            HttpError('HTTP 404', status=404, body='{"error":"Not found."}'),
        )
        self.assertIn('HTTP 404', message)
        self.assertIn('ADMIN_REINDEX_TOKEN', message)
        self.assertIn('MAGIC_CATALOG_IMPORT_TOKEN', message)
        self.assertIn('Not found', message)

    def test_catalog_503_explains_scale_setup(self):
        message = catalog_http_message(
            'preflight/request',
            'https://catalog.example/api/admin/catalog/ingest',
            HttpError('HTTP 503', status=503, body='Run npm run scale:setup and deploy again.'),
        )
        self.assertIn('HTTP 503', message)
        self.assertIn('npm run scale:setup', message)

    def test_catalog_network_error_has_connectivity_hint(self):
        message = catalog_http_message(
            'preflight/request',
            'https://catalog.example/api/admin/catalog/ingest',
            HttpError('Request failed'),
        )
        self.assertIn('network error', message)
        self.assertIn('DNS/TLS/network', message)

    def test_response_excerpt_is_bounded(self):
        message = catalog_http_message(
            'preflight/request',
            'https://catalog.example/api/admin/catalog/ingest',
            HttpError('HTTP 500', status=500, body='x' * 5000),
        )
        self.assertLess(len(message), 1800)
        self.assertIn('Response:', message)


if __name__ == '__main__':
    unittest.main()
