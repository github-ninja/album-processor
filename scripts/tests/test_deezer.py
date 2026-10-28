"""deezer-downloader: search, download and picking its albums up, with its HTTP endpoints mocked."""

from unittest import mock

from albumproc import config, deezer_dl, pipeline
from tests.helpers import TempLibraryCase, make_flac


def reply(payload) -> mock.Mock:
    return mock.Mock(status_code=200, json=mock.Mock(return_value=payload))


class DeezerDownloader(TempLibraryCase):
    def setUp(self):
        super().setUp()
        self.base = self.downloads / "deezer"
        for patcher in (mock.patch.object(config, "DEEZER_DOWNLOADER_DIR", self.base),
                        mock.patch.object(config, "DEEZER_DOWNLOADER_URL", "http://deezer:5000"),
                        mock.patch.object(deezer_dl.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_search_marks_albums_already_in_the_library(self):
        make_flac(self.music / "Arovane" / "1999 - Atol Scrap" / "01.flac")
        found = [{"album": "Atol Scrap", "album_id": 1, "artist": "Arovane"},
                 {"album": "Lilies", "album_id": "2", "artist": "Arovane"}]
        with mock.patch.object(deezer_dl.requests, "request", return_value=reply(found)) as request:
            albums = deezer_dl.search("Arovane")
        self.assertEqual([(a["id"], a["in_library"]) for a in albums], [(1, True), (2, False)])
        self.assertEqual(request.call_args.kwargs["json"], {"type": "album", "query": "Arovane"})

    def test_download_waits_for_the_task_and_returns_the_album_folder(self):
        album = self.base / "albums" / "Arovane - Lilies"
        make_flac(album / "01 - Arovane Pulse.flac")
        result = "[&#39;albums/Arovane - Lilies/01 - Arovane Pulse.flac&#39;]"
        replies = [reply({"task_id": 7}),
                   reply([{"id": 7, "state": "waiting"}]), reply([{"id": 7, "state": "active"}]),
                   reply([{"id": 7, "state": "mission accomplished", "result": result}])]
        with mock.patch.object(deezer_dl.requests, "request", side_effect=replies), \
                mock.patch.object(deezer_dl, "_track_count", return_value=1):
            self.assertEqual(deezer_dl.download(2), album)

    def test_empty_or_partial_download_is_not_an_album(self):
        make_flac(self.base / "albums" / "Arovane - Lilies" / "01 - Arovane Pulse.flac")
        done = lambda result: [reply({"task_id": 7}), reply([{"id": 7, "state": "mission accomplished",  # noqa: E731
                                                              "result": result}])]
        with mock.patch.object(deezer_dl.requests, "request", side_effect=done("[]")):
            with self.assertRaisesRegex(deezer_dl.DeezerError, "ни одного трека"):
                deezer_dl.download(2)
        result = "[&#39;albums/Arovane - Lilies/01 - Arovane Pulse.flac&#39;]"
        with mock.patch.object(deezer_dl.requests, "request", side_effect=done(result)), \
                mock.patch.object(deezer_dl, "_track_count", return_value=12):
            with self.assertRaisesRegex(deezer_dl.DeezerError, "только 1 из 12"):
                deezer_dl.download(2)
        self.assertTrue((self.base / "incomplete" / "Arovane - Lilies").is_dir())
        self.assertEqual(pipeline.find_new_albums(), [])

    def test_failed_download_is_explained(self):
        replies = [reply({"task_id": 7}), reply([{"id": 7, "state": "failed", "exception": "no FLAC rights"}])]
        with mock.patch.object(deezer_dl.requests, "request", side_effect=replies):
            with self.assertRaisesRegex(deezer_dl.DeezerError, "no FLAC rights"):
                deezer_dl.download(2)

    def test_new_albums_include_deezer_ones_but_not_while_downloading(self):
        make_flac(self.base / "albums" / "Arovane - Lilies" / "01.flac")
        make_flac(self.base / "songs" / "Arovane - Pulse.flac")  # single tracks are not an album
        make_flac(self.downloads / "Plaid" / "Spokes" / "01.flac")
        with mock.patch.object(deezer_dl, "busy", return_value=False):
            self.assertEqual(sorted(c.path.name for c in pipeline.find_new_albums()), ["Arovane - Lilies", "Spokes"])
        with mock.patch.object(deezer_dl, "busy", return_value=True):
            self.assertEqual([c.path.name for c in pipeline.find_new_albums()], ["Spokes"])


class CandidateFromPath(TempLibraryCase):
    def test_only_a_downloads_subfolder_names_the_artist(self):
        cases = {self.downloads / "Plaid" / "Spokes": "Plaid",
                 self.downloads / "Spokes": None,
                 self.downloads / "deezer" / "albums" / "Plaid - Spokes": None,
                 self.downloads / "deezer" / "Plaid - Spokes": None}
        for path, artist in cases.items():
            path.mkdir(parents=True)
            self.assertEqual(pipeline.candidate_from_path(path).artist, artist, path)
