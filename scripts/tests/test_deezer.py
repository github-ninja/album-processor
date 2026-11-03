"""deezer-downloader: search, download and picking its albums up, with its HTTP endpoints mocked."""

from unittest import mock

from albumproc import config, deezer_dl, jobs, pipeline
from tests.helpers import TempLibraryCase, make_flac


def reply(payload) -> mock.Mock:
    return mock.Mock(status_code=200, json=mock.Mock(return_value=payload))


def done(task_id: int, result: str) -> list[mock.Mock]:
    """A queued task that finished with *result* at the first look."""
    return [reply({"task_id": task_id}), reply([{"id": task_id, "state": "mission accomplished", "result": result}])]


class DeezerDownloader(TempLibraryCase):
    def setUp(self):
        super().setUp()
        self.base = self.downloads / "deezer"
        for patcher in (mock.patch.object(config, "DEEZER_DOWNLOADER_DIR", self.base),
                        mock.patch.object(config, "DEEZER_DOWNLOADER_URL", "http://deezer:5000"),
                        mock.patch.object(deezer_dl, "_track_count", return_value=1),
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

    def test_missing_tracks_are_fetched_again(self):
        album = self.base / "albums" / "Arovane - Lilies"
        make_flac(album / "01 - Arovane Pulse.flac")
        make_flac(self.base / "incomplete" / "Arovane - Lilies" / "01 - Arovane Pulse.flac")  # an earlier try
        make_flac(album / "02 - Arovane Tides.flac")
        one = "[&#39;albums/Arovane - Lilies/01 - Arovane Pulse.flac&#39;]"
        two = "[&#39;albums/Arovane - Lilies/01 - Arovane Pulse.flac&#39;, &#39;albums/Arovane - Lilies/02 - Arovane Tides.flac&#39;]"
        with mock.patch.object(deezer_dl.requests, "request",
                               side_effect=done(7, one) + done(8, two)) as request, \
                mock.patch.object(deezer_dl, "_track_count", return_value=2):
            self.assertEqual(deezer_dl.download(2), album)
        self.assertEqual([c.args[1] for c in request.call_args_list].count("http://deezer:5000/download"), 2)
        self.assertFalse((self.base / "incomplete" / "Arovane - Lilies").exists())

    def test_empty_or_partial_download_is_not_an_album(self):
        make_flac(self.base / "albums" / "Arovane - Lilies" / "01 - Arovane Pulse.flac")
        with mock.patch.object(deezer_dl.requests, "request", side_effect=done(7, "[]") * 3):
            with self.assertRaisesRegex(deezer_dl.DeezerError, "ни одного трека"):
                deezer_dl.download(2)
        result = "[&#39;albums/Arovane - Lilies/01 - Arovane Pulse.flac&#39;]"
        with mock.patch.object(deezer_dl.requests, "request", side_effect=done(7, result) * 3), \
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

    def test_discovered_partial_album_cannot_bypass_download_validation(self):
        album = self.base / 'albums' / 'Blotnik Brothers - Pragmatic Response'
        track = make_flac(album / '02 - Ergo.flac')
        task = {'args': '{&#39;album_id&#39;: 153422}', 'state': 'mission accomplished',
                'result': repr([str(track.relative_to(self.base))])}
        with mock.patch.object(deezer_dl, 'busy', return_value=False), \
                mock.patch.object(deezer_dl, '_queue', return_value=[task]), \
                mock.patch.object(deezer_dl, '_track_count', return_value=4), \
                mock.patch.object(jobs, 'prepare') as prepare:
            candidate, = pipeline.find_new_albums()
            with self.assertRaisesRegex(deezer_dl.DeezerError, '1 из 4'):
                pipeline.process_album(candidate, pipeline.RunState())
        prepare.assert_not_called()
        self.assertFalse(album.exists())
        self.assertTrue((self.base / 'incomplete' / album.name / track.name).exists())
        self.assertEqual(list(self.music.glob('Blotnik Brothers/*')), [])

    def test_unknown_total_blocks_explicit_import_without_moving_source(self):
        album = self.base / 'albums' / 'A - B'
        make_flac(album / 'one.flac')
        job = jobs.open_job(album); job['deezer_album_id'] = 1; jobs.save(job)
        with mock.patch.object(deezer_dl, 'busy', return_value=False), \
                mock.patch.object(deezer_dl, '_track_count', return_value=None), \
                mock.patch.object(jobs, 'prepare') as prepare:
            with self.assertRaisesRegex(deezer_dl.DeezerError, 'число треков'):
                pipeline.process_album(pipeline.candidate_from_path(album), pipeline.RunState())
        prepare.assert_not_called()
        self.assertTrue(album.is_dir())

    def test_missing_identity_does_not_guess_from_folder_name(self):
        album = self.base / 'albums' / 'A - B'; make_flac(album / 'one.flac')
        with mock.patch.object(deezer_dl, '_queue', return_value=[]):
            with self.assertRaisesRegex(deezer_dl.DeezerError, 'album ID'):
                deezer_dl.ensure_complete(album)
        self.assertTrue(album.is_dir())

    def test_complete_download_keeps_identity_after_queue_restart(self):
        album = self.base / 'albums' / 'A - B'; make_flac(album / 'one.flac')
        deezer_dl.ensure_complete(album, 123)
        with mock.patch.object(deezer_dl, '_queue', side_effect=AssertionError('queue not needed')):
            deezer_dl.ensure_complete(album)
        self.assertEqual(jobs.open_job(album)['deezer_album_id'], 123)

    def test_unknown_total_is_not_treated_as_successful_download(self):
        album = self.base / 'albums' / 'A - B'; track = make_flac(album / 'one.flac')
        with mock.patch.object(deezer_dl, '_track_count', return_value=None), \
                mock.patch.object(deezer_dl, '_run_task', return_value=[str(track.relative_to(self.base))]):
            with self.assertRaisesRegex(deezer_dl.DeezerError, 'число треков'):
                deezer_dl.download(1)
        self.assertTrue(track.exists())


class CandidateFromPath(TempLibraryCase):
    def test_only_a_downloads_subfolder_names_the_artist(self):
        cases = {self.downloads / "Plaid" / "Spokes": "Plaid",
                 self.downloads / "Spokes": None,
                 self.downloads / "deezer" / "albums" / "Plaid - Spokes": None,
                 self.downloads / "deezer" / "Plaid - Spokes": None}
        for path, artist in cases.items():
            path.mkdir(parents=True)
            self.assertEqual(pipeline.candidate_from_path(path).artist, artist, path)
