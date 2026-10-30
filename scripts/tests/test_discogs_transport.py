import ssl
import unittest
from unittest import mock

from albumproc import discogs


class DiscogsTransport(unittest.TestCase):
    def test_custom_context_is_scoped_to_discogs_and_verifies_certificates(self):
        adapter = discogs._SESSION.get_adapter("https://api.discogs.com/releases/1")
        self.assertIsInstance(adapter, discogs._DiscogsAdapter)
        context = adapter.poolmanager.connection_pool_kw["ssl_context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertNotIsInstance(discogs._SESSION.get_adapter("https://example.com/"),
                                 discogs._DiscogsAdapter)

    def test_context_uses_x25519_without_disabling_tls13(self):
        with mock.patch.object(discogs.ssl, "create_default_context") as create:
            discogs._DiscogsAdapter()
        create.assert_called_once_with()
        create.return_value.set_ecdh_curve.assert_called_once_with("X25519")
        self.assertEqual(discogs._SESSION.get_adapter(discogs._API + "/").poolmanager.connection_pool_kw[
            "ssl_context"].maximum_version, ssl.TLSVersion.MAXIMUM_SUPPORTED)

    def test_api_get_uses_session_with_auth_and_timeout(self):
        response = mock.Mock(status_code=200, headers={})
        response.json.return_value = {"id": 1}
        with mock.patch.object(discogs._SESSION, "get", return_value=response) as get:
            self.assertEqual(discogs.api_get(discogs._API + "/releases/1"), {"id": 1})
        get.assert_called_once_with(discogs._API + "/releases/1", headers=discogs._HEADERS,
                                    timeout=discogs._TIMEOUT)
