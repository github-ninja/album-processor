"""Staged imports, restart recovery and source preservation."""
from pathlib import Path
from unittest import mock
from albumproc import config, covers, discogs, files, jobs, pipeline, quality, upconvert
from tests.helpers import TempLibraryCase, make_flac
REAL_FETCH = discogs.fetch_album


class ImportJobs(TempLibraryCase):
    def setUp(self):
        super().setUp()
        self.source = self.downloads / 'A' / 'Album'
        make_flac(self.source / 'one.flac', seconds=4, artist='A', albumartist='A', album='Album',
                  title='One', tracknumber='1')
        self.candidate = pipeline.AlbumCandidate(self.source, 'A')
        for patcher in (mock.patch.object(discogs, 'fetch_album', return_value={
                'artist': 'A', 'title': 'Album', 'year': '2000', 'genre': 'IDM', 'tracklist': [],
                'release_id': 1, 'master_id': 2}),
                mock.patch.object(covers, 'fetch_deezer_cover', return_value=None),
                mock.patch.object(upconvert, 'check_upconvert', return_value=None)):
            patcher.start(); self.addCleanup(patcher.stop)

    def test_failure_preserves_source_byte_for_byte(self):
        before = (self.source / 'one.flac').read_bytes()
        with mock.patch.object(pipeline, 'process_tracks', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                pipeline.process_album(self.candidate, pipeline.RunState())
        self.assertEqual((self.source / 'one.flac').read_bytes(), before)
        self.assertEqual(jobs.open_job(self.source)['status'], 'failed')

    def test_waiting_decision_is_durable_and_source_is_unmodified(self):
        old = self.music / 'A' / '1999 - Album'
        make_flac(old / 'old.flac', seconds=4)
        before = (self.source / 'one.flac').read_bytes()
        with mock.patch.object(config, 'REPLACE_ACTION', 'ask'):
            state = pipeline.RunState()
            self.assertIsNone(pipeline.process_album(self.candidate, state))
        self.assertEqual((self.source / 'one.flac').read_bytes(), before)
        self.assertEqual(jobs.pending(), state.deferred)
        self.assertEqual(state.deferred[0]['path'], str(self.source))
        self.assertEqual(jobs.open_job(self.source)['metadata']['release_id'], 1)

    def test_retry_after_commit_does_not_reimport_or_duplicate(self):
        with mock.patch.object(quality, 'remember', side_effect=OSError('state unavailable')):
            with self.assertRaises(OSError):
                pipeline.process_album(self.candidate, pipeline.RunState())
        self.assertTrue(self.source.exists())
        with mock.patch.object(pipeline, '_process_album') as process:
            self.assertTrue(pipeline.process_album(self.candidate, pipeline.RunState()))
        process.assert_not_called()
        self.assertFalse(self.source.exists())
        self.assertTrue((self.music / 'A' / '2000 - Album' / '01. A - One.flac').exists())

    def test_failed_publication_restores_old_album(self):
        old = self.music / 'A' / '1999 - Album'
        make_flac(old / 'old.flac')
        work = self.tmp / 'prepared'; work.mkdir(); (work / 'new').touch()
        original_rename = Path.rename
        def fail(path, target):
            if path == work: raise OSError('cannot publish')
            return original_rename(path, target)
        with mock.patch.object(Path, 'rename', fail):
            with self.assertRaises(OSError):
                pipeline.move_to_library(work, pipeline.AlbumInfo('A', '2000', 'Album'))
        self.assertTrue((old / 'old.flac').exists())
        self.assertTrue((work / 'new').exists())

    def test_changed_source_revokes_previous_approval(self):
        job = jobs.open_job(self.source)
        job.update(status='waiting', source_manifest=jobs.manifest(self.source), approve_replace=True)
        jobs.save(job)
        (self.source / 'new.txt').write_text('a changed download')
        old = self.music / 'A' / '1999 - Album'; make_flac(old / 'old.flac', seconds=4)
        with mock.patch.object(config, 'REPLACE_ACTION', 'ask'):
            self.assertIsNone(pipeline.process_album(self.candidate, pipeline.RunState()))
        self.assertFalse(jobs.open_job(self.source)['approve_replace'])

    def test_track_plan_does_not_write_files(self):
        before = jobs.manifest(self.source)
        plan = pipeline.plan_tracks(self.source, pipeline.AlbumInfo('A', '2000', 'Album'))
        self.assertEqual(len(plan), 1)
        self.assertEqual(jobs.manifest(self.source), before)

    def test_corrupt_job_does_not_prevent_other_pending_jobs(self):
        job = jobs.open_job(self.source)
        job.update(status='waiting', deferred={'path': str(self.source), 'kind': 'replace'})
        jobs.save(job)
        (jobs.directory() / 'broken.json').write_text('{broken')
        self.assertEqual(jobs.pending(), [job['deferred']])

    def test_identity_survives_move_and_avoids_another_album_search(self):
        self.assertTrue(pipeline.process_album(self.candidate, pipeline.RunState()))
        record = jobs.open_job(self.source)
        self.assertEqual(jobs.identity_for(record['measured']['audio'])['release_id'], 1)

    def test_lookup_distinguishes_unavailable_from_unmatched(self):
        with mock.patch.object(discogs, 'fetch_album', REAL_FETCH), mock.patch.object(discogs, 'search', return_value=[]):
            self.assertEqual(discogs.lookup_album('A', 'Album')[1], 'unmatched')
        with mock.patch.object(discogs, 'fetch_album', REAL_FETCH), mock.patch.object(discogs, 'search', side_effect=discogs.requests.Timeout()):
            self.assertEqual(discogs.lookup_album('A', 'Album')[1], 'unavailable')

    def test_reused_download_path_retains_previous_identification(self):
        self.assertTrue(pipeline.process_album(self.candidate, pipeline.RunState()))
        previous = jobs.open_job(self.source)
        self.source.mkdir(parents=True)
        (self.source / 'new.txt').touch()
        self.assertEqual(jobs.open_job(self.source)['status'], 'new')
        self.assertEqual(jobs.identity_for(previous['measured']['audio'])['release_id'], 1)

    def test_changed_publication_is_not_accepted_during_recovery(self):
        with mock.patch.object(quality, 'remember', side_effect=OSError('state unavailable')):
            with self.assertRaises(OSError):
                pipeline.process_album(self.candidate, pipeline.RunState())
        job = jobs.open_job(self.source)
        (Path(job['destination']) / 'unexpected.txt').touch()
        with self.assertRaisesRegex(RuntimeError, 'published album changed'):
            pipeline.process_album(self.candidate, pipeline.RunState())
        self.assertTrue(self.source.is_dir())

    def test_archives_are_recorded_before_any_move(self):
        old = self.music / 'A' / '1999 - Album'
        track = make_flac(old / 'old.flac')
        expected = track.read_bytes()
        original = files.safe_rename

        def interrupt(source, target):
            result = original(source, target)
            if source == old:
                raise KeyboardInterrupt()
            return result

        with mock.patch.object(files, 'safe_rename', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            pipeline.process_album(self.candidate, pipeline.RunState())
        job = jobs.open_job(self.source)
        self.assertEqual(job['status'], 'committing')
        self.assertFalse(old.exists())
        self.assertEqual((Path(job['backups'][0][1]) / track.name).read_bytes(), expected)
        with mock.patch.object(config, 'REPLACE_ACTION', 'ask'):
            self.assertIsNone(pipeline.process_album(self.candidate, pipeline.RunState()))
        self.assertEqual(track.read_bytes(), expected)

    def test_cleanup_can_resume_after_partial_deletion_and_preserve_a_new_download(self):
        original = jobs.shutil.rmtree

        def interrupt(path):
            if Path(path).name.startswith('.albumproc-cleanup-'):
                (path / 'one.flac').unlink()
                raise KeyboardInterrupt()
            return original(path)

        with mock.patch.object(jobs.shutil, 'rmtree', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            pipeline.process_album(self.candidate, pipeline.RunState())
        self.assertEqual(jobs.recoverable_sources(), [self.source])
        self.source.mkdir()
        fresh = self.source / 'new.txt'
        fresh.write_text('new download')
        self.assertTrue(pipeline.process_album(self.candidate, pipeline.RunState()))
        self.assertEqual(fresh.read_text(), 'new download')
        self.assertFalse(list(self.source.parent.glob('.albumproc-cleanup-*')))

    def test_stale_decision_does_not_approve_changed_source(self):
        old = self.music / 'A' / '1999 - Album'
        make_flac(old / 'old.flac')
        with mock.patch.object(config, 'REPLACE_ACTION', 'ask'):
            state = pipeline.RunState()
            self.assertIsNone(pipeline.process_album(self.candidate, state))
            (self.source / 'new.txt').touch()
            with self.assertRaisesRegex(ValueError, 'изменились'):
                pipeline.process_album(self.candidate, pipeline.RunState(), state.deferred[0])
        self.assertTrue(old.exists())

    def test_replace_decision_is_for_the_library_version_shown(self):
        old = self.music / 'A' / '1999 - Album'
        make_flac(old / 'old.flac')
        with mock.patch.object(config, 'REPLACE_ACTION', 'ask'):
            state = pipeline.RunState()
            self.assertIsNone(pipeline.process_album(self.candidate, state))
            (old / 'new.txt').touch()
            other = pipeline.RunState()
            self.assertIsNone(pipeline.process_album(self.candidate, other, state.deferred[0]))
            self.assertNotEqual(state.deferred[0]['copies'], other.deferred[0]['copies'])
            self.assertTrue(pipeline.process_album(self.candidate, pipeline.RunState(), other.deferred[0]))
        self.assertFalse(self.source.exists())

    def test_audio_nested_in_an_empty_disc_folder_is_retained(self):
        extra = make_flac(self.source / 'CD2' / 'unexpected' / 'extra.flac')
        original = extra.read_bytes()
        with self.assertRaisesRegex(ValueError, 'unsupported subfolders'):
            pipeline.process_album(self.candidate, pipeline.RunState())
        self.assertEqual(extra.read_bytes(), original)

    def test_refuses_to_copy_a_parent_of_the_library(self):
        with self.assertRaises(ValueError):
            pipeline.process_album(pipeline.AlbumCandidate(self.tmp, None), pipeline.RunState())
        self.assertFalse(jobs.directory().exists())
