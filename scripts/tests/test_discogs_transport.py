import ssl
import unittest
from unittest import mock

from albumproc import discogs


class DiscogsTransport(unittest.TestCase):
    def test_year_after_artist_prefix_is_removed_before_search(self):
        with mock.patch.object(discogs, "search", return_value=[]) as search:
            discogs.fetch_album("Rammstein", "Rammstein - 1997 - Sehnsucht (Anniversary Edition - Remastered, 2023) (24bit-44.1kHz)")
        self.assertEqual(search.call_args_list[0].kwargs["release_title"], "Sehnsucht")

    def test_linked_master_requires_one_search_and_one_master_read(self):
        base = discogs._API
        release = {"id": 1, "title": "Album", "master_id": 9,
                   "tracklist": [{"position": "1", "title": "Song"}]}
        with mock.patch.object(discogs, "search", return_value=[
            {"title": "A - Album", "resource_url": f"{base}/releases/1"},
        ]) as search, mock.patch.object(discogs, "api_get", side_effect=[
            release, {"title": "Album", "year": 2000},
        ]) as get:
            result = discogs.fetch_album("A", "Album", track_titles=["Song"])
        self.assertEqual((result["release_id"], result["master_id"], result["year"]), (1, 9, "2000"))
        search.assert_called_once_with(type="release", per_page=20, artist="A", release_title="Album")
        self.assertEqual(get.call_args_list, [
            mock.call(f"{base}/releases/1"), mock.call(f"{base}/masters/9"),
        ])

    def test_release_accepts_null_optional_descriptions(self):
        release = {
            "id": 1,
            "title": "Album",
            "artists": [{"name": "Artist"}],
            "formats": [{"name": "Vinyl", "descriptions": None}],
            "tracklist": [{"position": "A1", "title": "One", "artists": None}],
        }
        with mock.patch.object(discogs, "search", return_value=[
            {"title": "Artist - Album", "resource_url": "release"},
        ]), mock.patch.object(discogs, "api_get", return_value=release):
            found = discogs.fetch_album("Artist", "Album", track_titles=["One"])
        self.assertEqual((found["release_id"], found["tracklist"][0]["title"]), (1, "One"))

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
