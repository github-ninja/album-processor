"""Failure and collision regressions; only temporary files and mocked networks."""
import asyncio
import json
import unittest
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC
from mutagen.id3 import ID3, APIC
from mutagen.mp4 import MP4

from albumproc import cli, config, cue, deezer_dl, discogs, files, library, opinion, quality, releases, tags, upconvert
from tests.helpers import TempLibraryCase, make_flac, make_image, make_m4a


class FileSafety(TempLibraryCase):
    def test_safe_rename_never_overwrites(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        a.write_bytes(b'first'); b.write_bytes(b'second')
        with self.assertRaises(FileExistsError):
            files.safe_rename(a, b)
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b'first', b'second'))

    def test_track_swap_and_rollback(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        a.write_bytes(b'first'); b.write_bytes(b'second')
        files.rename_files([(a, b), (b, a)])
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b'second', b'first'))
        original = files.safe_rename
        count = 0
        def fail_once(src, dst):
            nonlocal count
            count += 1
            if count == 4:
                raise OSError('simulated disk failure')
            return original(src, dst)
        with mock.patch.object(files, 'safe_rename', side_effect=fail_once):
            with self.assertRaises(OSError):
                files.rename_files([(a, b), (b, a)])
        self.assertEqual((a.read_bytes(), b.read_bytes()), (b'second', b'first'))
        self.assertFalse(list(self.tmp.glob('.rename-*')))

    def test_duplicate_destinations_fail_before_moving(self):
        a, b = self.tmp / 'a', self.tmp / 'b'
        a.touch(); b.touch()
        with self.assertRaises(ValueError):
            files.rename_files([(a, self.tmp / 'c'), (b, self.tmp / 'c')])
        self.assertTrue(a.exists() and b.exists())

    def test_audio_in_unknown_subfolder_is_not_deleted(self):
        audio = make_flac(self.tmp / 'bonus' / 'song.flac')
        with self.assertRaises(ValueError):
            files.remove_non_music_files(self.tmp)
        self.assertTrue(audio.exists())

    def test_repeated_backups_do_not_nest_or_overwrite(self):
        path = self.music / 'A' / 'Album'; path.mkdir(parents=True)
        (path / 'old').touch(); first = files.move_aside(path)
        path.mkdir(); (path / 'new').touch(); second = files.move_aside(path)
        self.assertNotEqual(first, second)
        self.assertTrue((first / 'old').exists() and (second / 'new').exists())

    def test_lock_rejects_second_writer_and_releases_after_failure(self):
        with self.assertRaisesRegex(ValueError, 'failed'):
            with files.processor_lock():
                with self.assertRaisesRegex(RuntimeError, 'уже работает'):
                    with files.processor_lock():
                        self.fail('second writer entered')
                raise ValueError('failed')
        with files.processor_lock():
            pass


class CueSafety(TempLibraryCase):
    def test_duplicate_numbers_and_equal_indices_are_rejected(self):
        image = make_flac(self.tmp / 'image.flac', seconds=3)
        for tracks in ([cue.CueTrack(1, start=0), cue.CueTrack(1, start=75)],
                       [cue.CueTrack(1, start=0), cue.CueTrack(2, start=0)]):
            with self.subTest(tracks=tracks), self.assertRaises(ValueError):
                cue.split_image(image, cue.CueSheet(tracks=tracks))
        self.assertEqual(list(self.tmp.glob('*.flac')), [image])

    def test_tagging_failure_keeps_image_without_partial_tracks(self):
        image = make_flac(self.tmp / 'image.flac', seconds=3)
        sheet = cue.CueSheet(tracks=[cue.CueTrack(1, start=0), cue.CueTrack(2, start=75)])
        before = image.read_bytes()
        with mock.patch.object(cue, '_write_tags', side_effect=[None, OSError('disk full')]):
            with self.assertRaises(OSError):
                cue.split_image(image, sheet)
        self.assertEqual(image.read_bytes(), before)
        self.assertEqual(list(self.tmp.glob('*.flac')), [image])
        self.assertFalse(list(self.tmp.glob('.*.splitting.flac')))


class TagSafety(TempLibraryCase):
    def meta(self):
        return tags.TrackMetadata('A; B', 'Song', 'Album', '2000', 'G1; G2', 'A; B', 1, 1,
                                  release_types=('album',), remixers=('Remixer',), compilation=True)

    def test_mp3_picture_survives_tag_cleanup(self):
        path = self.tmp / 'track.mp3'; pic = make_image((32, 32))
        audio = ID3(); audio.add(APIC(type=3, mime='image/jpeg', desc='', data=pic)); audio.save(path)
        tags.write_tags(path, self.meta())
        audio = ID3(path)
        self.assertEqual(audio.getall('APIC')[0].data, pic)
        self.assertEqual(audio['TXXX:RELEASETYPE'].text, ['album'])
        self.assertEqual(audio['TPE1'].text, ['A', 'B'])

    def test_aac_cover_and_multiple_values_survive_without_transcoding(self):
        path = make_m4a(self.tmp / 'track.m4a', codec='aac')
        pic = make_image((32, 32)); tags.embed_picture(path, pic)
        tags.write_tags(path, self.meta())
        audio = MP4(path)
        self.assertEqual(audio.info.codec, 'mp4a.40.2')
        self.assertEqual(bytes(audio['covr'][0]), pic)
        self.assertEqual(audio['\xa9ART'], ['A', 'B'])
        self.assertEqual(tags.read_tags(path)['genre'], 'G1; G2')
        self.assertEqual(audio['----:com.apple.iTunes:RELEASETYPE'], [b'album'])

    def test_write_errors_propagate(self):
        path = make_flac(self.tmp / 'track.flac')
        with mock.patch.dict(tags._TAG_WRITERS, {'.flac': mock.Mock(side_effect=OSError('disk full'))}):
            with self.assertRaises(OSError):
                tags.write_tags(path, self.meta())


class StateAndNetwork(TempLibraryCase):
    def test_corrupt_state_is_not_silently_overwritten(self):
        config.state_dir().mkdir()
        for path, load in ((quality.store_path(), quality._load), (releases.state_path(), releases.load_state)):
            path.write_text('{broken')
            with self.assertRaises(ValueError): load()
            self.assertEqual(path.read_text(), '{broken')

    def test_cached_passport_refreshes_track_names_without_remeasuring(self):
        album = self.music / 'A' / '2000 - Album'
        path = make_flac(album / 'before.flac', seconds=4)
        passport = quality.passport(album)
        path.rename(album / 'after.flac')
        with mock.patch.object(quality, 'measure_album') as measure:
            refreshed = quality.passport(album)
        measure.assert_not_called()
        self.assertEqual(refreshed['tracks'][0]['file'], 'after.flac')
        self.assertEqual(refreshed['tracks'][0]['dr'], passport['tracks'][0]['dr'])

    def test_unreachable_downloader_is_not_idle(self):
        with mock.patch.object(deezer_dl, '_queue', side_effect=deezer_dl.DeezerError('offline')):
            self.assertTrue(deezer_dl.busy())

    def test_transient_discogs_error_retries_but_auth_error_does_not(self):
        response = mock.Mock(status_code=200, headers={}); response.json.return_value = {'ok': True}
        with mock.patch.object(discogs._SESSION, 'get', side_effect=[discogs.requests.Timeout(), response]) as get, \
                mock.patch.object(discogs.time, 'sleep'):
            self.assertEqual(discogs.api_get('https://api.discogs.com/test'), {'ok': True})
        self.assertEqual(get.call_count, 2)
        error = mock.Mock(status_code=401, headers={}); error.raise_for_status.side_effect = discogs.requests.HTTPError()
        with mock.patch.object(discogs._SESSION, 'get', return_value=error) as get:
            with self.assertRaises(discogs.requests.HTTPError): discogs.api_get('https://api.discogs.com/test')
        get.assert_called_once()

    def test_failed_release_check_does_not_advance_cursor(self):
        state = {'schedule': 'daily', 'last_check': '2026-10-01', 'artists': {'A': {'id': 1, 'genres': [106]}}, 'notified': []}
        with mock.patch.object(releases, 'load_state', return_value=state), \
                mock.patch.object(releases, 'library_artists', return_value={'A': ['Album']}), \
                mock.patch.object(releases, 'albums_of', side_effect=OSError('offline')), \
                mock.patch.object(releases, 'save_state'):
            _, new_state = releases.check()
        self.assertEqual(new_state['last_check'], '2026-10-01')

    def test_broken_album_does_not_abort_the_queue_or_lose_report(self):
        a = make_flac(self.downloads / 'A' / 'one.flac').parent
        b = make_flac(self.downloads / 'B' / 'two.flac').parent
        report = self.tmp / 'report.json'
        with mock.patch.object(cli.pipeline, 'process_album', side_effect=[OSError('broken'), True]) as process, \
                mock.patch.object(config, 'DEFERRED_REPORT_FILE', str(report)):
            self.assertEqual(cli._process([str(a), str(b)]), 1)
        self.assertEqual(process.call_count, 2)
        self.assertTrue(report.exists())

    def test_empty_gemini_reply_uses_fallback(self):
        empty = mock.Mock(status_code=200); empty.json.return_value = {'candidates': []}
        good = mock.Mock(status_code=200); good.json.return_value = {'candidates': [{'content': {'parts': [{'text': 'OK'}]}}]}
        with mock.patch.object(config, 'GEMINI_API_KEY', 'test'), \
                mock.patch.object(config, 'GEMINI_MODEL', 'first'), \
                mock.patch.object(config, 'GEMINI_FALLBACK_MODEL', 'second'), \
                mock.patch.object(opinion.requests, 'post', side_effect=[empty, good]):
            self.assertEqual(opinion.ask_gemini({}), 'OK')

    def test_antiphase_audio_does_not_disappear_from_spectrum(self):
        path = make_flac(self.tmp / 'stereo.flac', seconds=3)
        before, rate = sf.read(path, dtype='float32')
        expected, _ = upconvert._average_spectrum(path)
        before[:, 1] *= -1
        sf.write(path, before, rate, subtype='PCM_16')
        actual, _ = upconvert._average_spectrum(path)
        np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-7)


class LibrarySafety(TempLibraryCase):
    def album(self):
        album = self.music / 'A' / '2000 - Album'
        make_flac(album / 'one.flac', album='Album', date='2000', artist='A', albumartist='A',
                  title='One', tracknumber='1', tracktotal='99', disctotal='9')
        return album

    def entry(self, album, **changes):
        return {'path': str(album), 'artist': 'A', 'old_artist_folder': 'A', 'new_artist_folder': 'A',
                'old_folder': album.name, 'new_folder': album.name, 'old_album': 'Album', 'new_album': 'Album',
                'old_year': '2000', 'new_year': '2000', **changes}

    def test_destination_conflict_is_rejected_before_tag_updates(self):
        album = self.album(); (album.parent / '2001 - Other').mkdir()
        entry = self.entry(album, new_folder='2001 - Other', new_album='Other', new_year='2001')
        self.assertEqual(library.apply_library_fixes([entry]), 1)
        self.assertEqual(FLAC(album / 'one.flac')['album'], ['Album'])

    def test_stale_plan_does_not_overwrite_manual_tag_edits(self):
        album = self.album()
        entry = self.entry(album, tag_fixes=[['one.flac', 'artist', ['Original'], ['Replacement']]])
        self.assertEqual(library.apply_library_fixes([entry]), 1)
        self.assertEqual(FLAC(album / 'one.flac')['artist'], ['A'])

    def test_path_escape_is_rejected(self):
        album = self.album()
        entry = self.entry(album, new_artist_folder='../elsewhere')
        self.assertEqual(library.apply_library_fixes([entry]), 1)
        self.assertTrue((album / 'one.flac').exists())

    def test_destination_cannot_escape_through_artist_symlink(self):
        album = self.album(); outside = self.tmp / 'outside'; outside.mkdir()
        (self.music / 'Alias').symlink_to(outside, target_is_directory=True)
        entry = self.entry(album, new_artist_folder='Alias')
        self.assertEqual(library.apply_library_fixes([entry]), 1)
        self.assertTrue((album / 'one.flac').exists())
        self.assertEqual(list(outside.iterdir()), [])

    def test_totals_are_computed_from_tracks_not_trusted_from_old_tags(self):
        album = self.album()
        fixes = library._plan_tag_fixes(album, files.album_audio_files(album), {}, {}, None)
        wanted = {field: new for rel, field, old, new in fixes}
        self.assertEqual(wanted['tracktotal'], ['1'])
        self.assertEqual(wanted['disctotal'], ['1'])

    def test_canonical_album_title_wins_over_local_whitespace_cleanup(self):
        album = self.album(); path = album / 'one.flac'; audio = FLAC(path)
        audio['album'] = ['Album ']; audio.save()
        entry = self.entry(album, old_album='Album ', new_album='Canonical', new_folder='2000 - Canonical',
                           tag_fixes=[['one.flac', 'album', ['Album '], ['Album']]])
        self.assertEqual(library.apply_library_fixes([entry]), 0)
        self.assertEqual(FLAC(album.parent / '2000 - Canonical' / 'one.flac')['album'], ['Canonical'])


class DownloadPaths(TempLibraryCase):
    def test_reported_tracks_must_exist_and_are_counted_once(self):
        base = self.downloads / 'deezer'
        make_flac(base / 'albums' / 'A - Album' / 'one.flac')
        names = ['albums/A - Album/one.flac', 'albums/A - Album/one.flac', 'albums/A - Album/missing.flac']
        with mock.patch.object(config, 'DEEZER_DOWNLOADER_DIR', base):
            self.assertEqual(deezer_dl._existing_tracks(names), names[:1])
            for invalid in ('../outside.flac', '/tmp/outside.flac', 'songs/one.flac'):
                with self.subTest(path=invalid), self.assertRaises(deezer_dl.DeezerError):
                    deezer_dl._existing_tracks([invalid])


class BotLifecycle(unittest.IsolatedAsyncioTestCase):
    async def test_poller_is_cancelled_and_awaited_on_stop(self):
        import bot
        task = asyncio.create_task(asyncio.sleep(3600))
        app = mock.Mock(bot_data={'release_poller': task})
        await bot._on_stop(app)
        self.assertTrue(task.cancelled())
        self.assertNotIn('release_poller', app.bot_data)

    async def test_cancelling_processor_reaps_the_child(self):
        import bot
        import tempfile
        from pathlib import Path
        created = asyncio.Event(); processes = []
        create = asyncio.create_subprocess_exec
        async def spawn(*args, **kwargs):
            proc = await create(*args, **kwargs)
            processes.append(proc); created.set()
            return proc
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / 'sleeper.py'; script.write_text('import time\ntime.sleep(60)\n')
            with mock.patch.object(bot, 'PROCESSOR_SCRIPT', str(script)), \
                    mock.patch.object(bot, '_processor_lock', asyncio.Lock()), \
                    mock.patch.object(bot.asyncio, 'create_subprocess_exec', side_effect=spawn):
                task = asyncio.create_task(bot._run_processor([], {}))
                await asyncio.wait_for(created.wait(), timeout=5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=10)
        self.assertIsNotNone(processes[0].returncode)


class ConvertedLibrary(TempLibraryCase):
    def test_alac_conversion_and_renames_are_one_idempotent_plan(self):
        album = self.music / 'A' / '2000 - Album'
        make_m4a(album / '01.m4a', **{'\xa9ART': ['A', 'B'], 'aART': ['A'], '\xa9nam': ['One'],
                                     '\xa9alb': ['Album'], '\xa9day': ['2000'], '\xa9gen': ['IDM'],
                                     'trkn': [(1, 1)]})
        (album / 'Cover.jpg').write_bytes(make_image((1000, 1000)))
        with mock.patch.object(discogs, 'fetch_album', return_value=None), \
                mock.patch.object(discogs, 'canonical_artist_name', return_value=None), \
                mock.patch('albumproc.covers.fetch_deezer_cover', return_value=None):
            plan = library.plan_library_fixes()
            self.assertEqual(library.apply_library_fixes(plan), 0)
            self.assertEqual(library.plan_library_fixes(), [])
        path = album / '01. A & B - One.flac'
        self.assertTrue(path.exists())
        self.assertEqual(FLAC(path)['artist'], ['A', 'B'])
        self.assertFalse((album / '01.m4a').exists())


class ImportSafety(TempLibraryCase):
    def test_duplicate_track_names_preserve_both_sources(self):
        from albumproc import pipeline
        album = self.downloads / 'Album'
        a = make_flac(album / 'a.flac', artist='A', title='One', tracknumber='1')
        b = make_flac(album / 'b.flac', artist='A', title='One', tracknumber='1')
        before = (a.read_bytes(), b.read_bytes())
        info = pipeline.AlbumInfo(artist='A', album='Album', year='2000', genre='IDM')
        with self.assertRaisesRegex(ValueError, 'same file name'):
            pipeline.process_tracks(album, info)
        self.assertEqual((a.read_bytes(), b.read_bytes()), before)

    def test_symlink_album_is_rejected_before_processing(self):
        from albumproc import pipeline
        album = self.downloads / 'Album'
        outside = make_flac(self.tmp / 'outside.flac')
        album.mkdir(); (album / 'linked.flac').symlink_to(outside)
        with mock.patch.object(pipeline.cue, 'split_album') as split:
            with self.assertRaisesRegex(ValueError, 'symbolic'):
                pipeline.process_album(pipeline.AlbumCandidate(album, 'A'), pipeline.RunState())
        split.assert_not_called()
        self.assertTrue(outside.is_file())

    def test_different_artist_cannot_supply_album_metadata(self):
        from albumproc import pipeline
        album = self.downloads / 'Album'
        make_flac(album / 'one.flac', artist='A', albumartist='A', album='Album', date='2000')
        found = {'artist': 'Unrelated', 'title': 'Wrong', 'year': '2025', 'genre': 'Wrong', 'tracklist': []}
        with mock.patch.object(discogs, 'fetch_album', return_value=found):
            info = pipeline.resolve_album_info(pipeline.AlbumCandidate(album, 'A'))
        self.assertFalse(info.from_discogs)
        self.assertEqual((info.artist, info.album, info.year), ('A', 'Album', '2000'))
        self.assertFalse(pipeline._is_part_of('', 'Unrelated'))

    def test_quality_query_does_not_measure_other_albums(self):
        target = self.music / 'A' / '2000 - Wanted'
        make_flac(target / 'one.flac', seconds=4)
        make_flac(self.music / 'B' / '2001 - Other' / 'two.flac', seconds=4)
        with mock.patch.object(quality, 'measure_album', wraps=quality.measure_album) as measure:
            quality.library_report('Wanted')
        self.assertEqual([call.args[0] for call in measure.call_args_list], [target])


class IdentificationSafety(unittest.TestCase):
    def test_double_album_is_not_used_for_one_component(self):
        result = {'title': 'Rammstein - Herzeleid (1995) Sehnsucht (1997)', 'master_id': 1}
        self.assertIsNone(discogs._pick_result([result], 'Sehnsucht'))

    def test_single_format_penalty_does_not_reject_exact_short_title(self):
        result = {'title': 'Artist - EP', 'format': ['Single']}
        self.assertEqual(discogs._pick_result([result], 'EP'), result)
