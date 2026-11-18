"""Decision benchmarks with expected results based on catalogue or signal evidence."""

from datetime import date
from unittest import mock

import av
import numpy as np
import soundfile as sf

from albumproc import audioformat, config, covers, deezer_dl, discogs, library, opinion, pipeline, quality, releases, replaygain, tags, cue
from tests.helpers import TempLibraryCase, make_flac, make_image, make_m4a
from tests.test_releases import release


class DiscogsDecisions(TempLibraryCase):
    def test_discogs_continues_after_wrong_first_hit(self):
        # Even Spring is on Plaid's Spokes. A same-titled entry with unrelated
        # tracks must not block the useful hit in the same search result.
        source = self.downloads / "Plaid" / "Spokes"
        make_flac(source / "one.flac", title="Even Spring", artist="Plaid")
        rows = [{"title": "Plaid - Spokes", "resource_url": key} for key in ("wrong", "right")]
        details = {key: {"id": number, "title": "Spokes", "artists": [{"name": "Plaid"}],
                         "tracklist": [{"position": "1", "title": title}]}
                   for key, number, title in (("wrong", 1, "Unrelated"), ("right", 2, "Even Spring"))}
        with mock.patch.object(discogs, "search", side_effect=lambda **kw: rows if kw["type"] == "release" else []), \
                mock.patch.object(discogs, "api_get", side_effect=lambda url, **kw: details[url]):
            info = pipeline.resolve_album_info(pipeline.candidate_from_path(source))
        self.assertEqual((info.lookup_status, info.release_id), ("matched", 2))

    def test_one_catalogue_track_cannot_match_three_local_tracks(self):
        local = ["Intro"] * 3
        remote = [{"title": "Intro"}]
        self.assertEqual(discogs.tracklist_agreement(local, remote)[0], 0.5)

    def test_complete_but_unrelated_tracklist_cannot_rename_library_album(self):
        album = self.music / "Plaid" / "2003 - Spokes"
        track = make_flac(album / "one.flac", title="Even Spring", album="Spokes", date="2003", tracknumber="1")
        wrong = {"title": "Spokes (Single)", "artist": "Plaid", "year": "2004",
                 "tracklist": [{"position": "1", "title": "Totally Unrelated"}]}
        with mock.patch.object(discogs, "fetch_album", return_value=wrong):
            plan = library._plan_title(album, [track], check_all=True)
        self.assertEqual((plan["new_album"], plan["new_year"]), ("Spokes", "2003"))

    def test_same_album_title_with_different_programme_is_not_a_duplicate(self):
        folders = [self.music / "Example Artist" / f"{year} - Self Titled" for year in (2000, 2010)]
        for i, (folder, title) in enumerate(zip(folders, ("A Song", "Different Track"))):
            make_flac(folder / f"{i}.flac", title=title, tracknumber="1")
        self.assertEqual(library._plan_duplicates(folders), [])

    def test_numeric_positions_do_not_borrow_titles_from_another_disc(self):
        rows = [{"position": "1-1", "title": "Disc one"}]
        self.assertIsNone(discogs.resolve_track_title(rows, 1, 0, disc=2))

    def test_vinyl_side_numbering_starts_again_on_each_side(self):
        rows = [{"position": "B1", "title": "Side B"}, {"position": "A1", "title": "Side A"}]
        self.assertEqual(discogs.resolve_track_title(rows, 1, 0), "Side A")
        self.assertEqual(discogs.resolve_track_title(rows, 2, 1), "Side B")

    def test_explicit_disc_folders_take_priority_over_copied_tags(self):
        album = self.downloads / "Artist" / "Album"
        make_flac(album / "Disc 1" / "one.flac", title="One", tracknumber="1", discnumber="1")
        make_flac(album / "Disc 2" / "two.flac", title="Two", tracknumber="1", discnumber="1")
        tracks = pipeline.plan_tracks(album, pipeline.AlbumInfo("Artist", "2000", "Album"))
        self.assertEqual([(target.parent.name, meta.disc_number) for _, target, meta in tracks],
                         [("CD1", 1), ("CD2", 2)])

    def test_maintenance_preserves_another_edition_title(self):
        album = self.music / "Example Artist" / "2005 - Album"
        local = ["Opening", "It’s not over now", "Finale", "Warmth"]
        remote = ["Opening", "It's Over Now", "Finale", "Warmth"]
        for number, title in enumerate(local, 1):
            make_flac(album / f"{number}.flac", title=title, tracknumber=str(number),
                      artist="Example Artist", albumartist="Example Artist", album="Album", date="2005")
        match = {"artist": "Example Artist", "title": "Album", "year": "2005",
                 "tracklist": [{"position": str(number), "title": title}
                               for number, title in enumerate(remote, 1)]}
        match["tracklist"][0]["artists"] = "Different Performer"
        with mock.patch.object(discogs, "fetch_album", return_value=match) as fetch, \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            plan = library.plan_library_fixes(check_all=True)
        self.assertTrue(plan)
        self.assertFalse(any(field in {"title", "artist"} for entry in plan for _, field, _, _ in entry["tag_fixes"]))
        fetch.assert_called_once()

    def test_no_catalogue_match_preserves_symbolic_punctuation_in_album_tag(self):
        title = "l--ll-ll ll-ll- l--l-"
        album = self.music / "Artist" / f"2010 - {title}"
        track = make_flac(album / "one.flac", title="One", album=title, date="2010", tracknumber="1")
        with mock.patch.object(discogs, "fetch_album", return_value=None):
            plan = library._plan_title(album, [track], check_all=True)
        self.assertEqual(plan["new_album"], title)
        self.assertEqual(plan["new_folder"], album.name)

    def test_library_plan_distinguishes_an_outage_from_no_catalogue_match(self):
        album = self.music / "Artist" / "2000 - Album"
        track = make_flac(album / "one.flac", title="One", album="Album", date="2000", tracknumber="1")
        with mock.patch.object(discogs, "lookup_album", return_value=(None, "unavailable")):
            plan = library._plan_title(album, [track], check_all=True)
        self.assertIn("не удалось завершить проверку", plan["note"])
        with mock.patch.object(discogs, "lookup_album", return_value=(None, "unmatched")):
            plan = library._plan_title(album, [track], check_all=True)
        self.assertIn("нет совпадения", plan["note"])
        self.assertEqual((plan["new_album"], plan["new_year"]), ("Album", "2000"))

    def test_human_title_spelling_ignores_formatting_but_preserves_version_words(self):
        for local, remote, compatible in [
            ("Delusions / Ruminations / Interlude", "Delusions/Rumination (Interlude)", True),
            ("23Kid", "23kid", True),
            ("Sing to the Moon", "Sing To The Moon (Laura Mvula Rework)", False),
            ("It's Not Over Now", "It's Over Now", False),
            ("Song (Radio Edit)", "Song (Extended Mix)", False),
            ("Theme 1994", "Theme 1995", False),
        ]:
            with self.subTest(local=local, remote=remote):
                self.assertEqual(library._comparable_title(local, remote), compatible)

    def test_spelling_change_needs_duration_evidence(self):
        album = self.music / "Artist" / "Album"
        track = make_flac(album / "one.flac", title="Tether", tracknumber="1", seconds=3)
        candidate = [{"position": "1", "title": "Together"}]
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, candidate), {})
        candidate = [{"position": "1", "title": "Tehter"}]
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, candidate), {})
        candidate[0]["duration"] = "0:09"
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, candidate), {})
        candidate[0]["duration"] = "0:03"
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, []), {})
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, [
            {"position": "1", "title": "Tether"},
        ]), {})
        self.assertEqual(library._confirmed_tracks(album, [track], candidate, candidate), {track: candidate[0]})

    def test_edition_wide_transcription_is_not_evidence_of_individual_typos(self):
        album = self.music / "Pudra" / "Album"
        local = ["Davis Miles (Interlude)", "Skratchitura #1", "Skratchitura #2", "Tibet"]
        remote = ["David Miles (Interlude)", "Scratchitura #1", "Scratchitura #2", "Tibet"]
        paths = [make_flac(album / f"{index}.flac", title=title, tracknumber=str(index), seconds=3)
                 for index, title in enumerate(local, 1)]
        rows = [{"position": str(index), "title": title, "duration": "0:03"}
                for index, title in enumerate(remote, 1)]
        self.assertEqual(library._confirmed_tracks(album, paths, rows, rows), {paths[3]: rows[3]})

    def test_discogs_standard_capitals_do_not_replace_artist_styling(self):
        for local, remote, expected in (
            ("ilanders", "Ilanders", "ilanders"),
            ("23Kid", "23kid", "23kid"),
            ("Sugar & Cream", "Sugar And Cream", "Sugar & Cream"),
            ("Requiem for CS70 and Strings", "Requiem for CS70 And Strings", "Requiem for CS70 and Strings"),
            ("Don't Stay", "Don´t Stay", "Don't Stay"),
            ("I´ll Be Yours", "I'll Be Yours", "I'll Be Yours"),
            ("Youth’s Fountain", "Youth's Fountain", "Youth’s Fountain"),
            ("Why Does My Heart Feel so Bad?", "Why Does My Heart Feel So Bad ?", "Why Does My Heart Feel so Bad?"),
        ):
            with self.subTest(local=local, remote=remote):
                self.assertEqual(library._catalogue_title(local, remote), expected)

    def test_discogs_standard_capitals_do_not_rename_the_album(self):
        album = self.music / "Opius" / "2025 - Sound of Hue"
        track = make_flac(album / "one.flac", title="Example Track", album="Sound of Hue",
                          date="2025", tracknumber="1")
        candidate = {"artist": "Opius; Dacamera", "title": "Sound Of Hue", "year": "2025",
                     "tracklist": [{"position": "1", "title": "Example Track"}]}
        with mock.patch.object(discogs, "fetch_album", return_value=candidate):
            plan = library._plan_title(album, [track], check_all=True)
        self.assertEqual((plan["new_album"], plan["new_folder"]), ("Sound of Hue", album.name))

    def test_canonical_title_keeps_an_already_documented_guest(self):
        album = self.music / "Main" / "Album"
        track = make_flac(album / "one.flac", title="Song (feat. Guest)", tracknumber="1",
                          artist="Main", albumartist="Main")
        rows = [{"position": "1", "title": "Song", "artists": "Main"}]
        changes = library._plan_tag_fixes(album, [track], {}, {}, None, canonical_tracklist=rows)
        self.assertIn(["one.flac", "title", ["Song (feat. Guest)"], ["Song"]], changes)
        self.assertIn(["one.flac", "artist", ["Main"], ["Main", "Guest"]], changes)

    def test_malformed_discogs_search_is_an_outage(self):
        reply = mock.Mock(status_code=200, headers={}, json=mock.Mock(return_value=[]))
        with mock.patch.object(discogs._SESSION, "get", return_value=reply):
            self.assertEqual(discogs.lookup_album("Plaid", "Spokes"), (None, "unavailable"))


class DeezerDecisions(TempLibraryCase):
    def test_deezer_catalogue_album_shape_and_count(self):
        # Public Deezer /album/302127 response observed during the review.
        album = {"id": 302127, "title": "Discovery", "nb_tracks": 14,
                 "release_date": "2001-03-07", "record_type": "album"}
        reply = mock.Mock(json=mock.Mock(return_value=album))
        with mock.patch.object(deezer_dl.requests, "get", return_value=reply):
            self.assertEqual(deezer_dl._track_count(302127), 14)
            reply.json.return_value = {"nb_tracks": True}
            self.assertIsNone(deezer_dl._track_count(302127))

    def test_title_words_and_series_are_not_edition_markers(self):
        rows = [release(1, "Live and Let Die", "2026-10-18"), release(2, "Antrakt II", "2026-10-19"),
                release(3, "Live at Wembley", "2026-10-20"), release(4, "Antrakt (Remastered)", "2026-10-20")]
        actual = releases.new_albums(rows, ["Antrakt"], "2026-10-15", "2026-10-25", set(), [106])
        self.assertEqual([item["id"] for item in actual], [1, 2])

    def test_late_catalogue_addition_is_reported_after_the_previous_run(self):
        make_flac(self.music / "Example" / "2001 - Earlier" / "track.flac")
        state = {"schedule": "weekly", "last_check": "2026-10-20",
                 "artists": {"Example": {"id": 1, "genres": [106]}}, "notified": [4]}
        rows = [release(1, "Late Addition", "2026-10-10"), release(4, "Issued", "2026-10-15"),
                release(5, "Issued", "2026-10-16")]
        with mock.patch.object(releases, "load_state", return_value=state), \
                mock.patch.object(releases, "albums_of", return_value=rows):
            news, _ = releases.check(date(2026, 10, 25))
        self.assertEqual([item["id"] for item in news], [1])

    def test_reused_download_folder_requires_fresh_identity(self):
        album = config.DEEZER_DOWNLOADER_DIR / "albums" / "A - Same Title"
        track = make_flac(album / "one.flac")
        with mock.patch.object(deezer_dl, "_track_count", return_value=1):
            deezer_dl.ensure_complete(album, 123)
            make_flac(track, seconds=3)
            with mock.patch.object(deezer_dl, "_queue", return_value=[]), \
                    self.assertRaisesRegex(deezer_dl.DeezerError, "album ID"):
                deezer_dl.ensure_complete(album)
        self.assertTrue(track.is_file())

    def test_corrupt_queue_is_not_idle(self):
        with mock.patch.object(deezer_dl, "_call", return_value={"data": []}):
            self.assertTrue(deezer_dl.busy())

    def test_small_cover_is_only_upgraded_with_the_same_art(self):
        album = self.tmp / "album"
        album.mkdir()
        local = make_image((250, 250), seed=1)
        (album / "Cover.jpg").write_bytes(local)
        with mock.patch.object(covers, "fetch_deezer_cover", return_value=make_image((1000, 1000), seed=2)):
            result, origin, _ = covers.choose_cover(album, [], "Artist", "Album")
        self.assertEqual((result, origin), (local, "local"))
        with mock.patch.object(covers, "fetch_deezer_cover", return_value=make_image((1000, 1000), seed=1)):
            result, origin, _ = covers.choose_cover(album, [], "Artist", "Album")
        self.assertEqual((covers.image_size(result), origin), ((1000, 1000), "deezer"))


class AudioDecisions(TempLibraryCase):
    def test_low_rate_lossless_source_does_not_become_fake_lossless_after_resampling(self):
        make_flac(self.downloads / "A" / "Album" / "one.flac", sr=32000,
                  title="One", album="Album", artist="A")
        with mock.patch.object(config, "UPCONVERT_ACTION", "ask"), \
                mock.patch.object(discogs, "fetch_album", return_value=None), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            self.assertTrue(pipeline.process_album(pipeline.find_new_albums()[0], pipeline.RunState()))
        self.assertTrue(list(self.music.glob("A/Album/*.flac")))

    def test_truncated_flac_has_no_publishable_conversion(self):
        path = make_flac(self.tmp / "track.flac", sr=48000, subtype="PCM_24")
        data = path.read_bytes()
        broken = data[:len(data) // 2]
        path.write_bytes(broken)
        with self.assertRaises((ValueError, OSError)):
            audioformat.convert_to_library_format(path)
        self.assertEqual(path.read_bytes(), broken)
        self.assertEqual(list(self.tmp.glob("*.flac")), [path])

    def test_true_peak_exceeds_the_discrete_sample_peak(self):
        rate = 44100
        time = np.arange(rate * 2) / rate
        envelope = np.minimum(np.minimum(time, time[-1] - time) * 10, 1)
        signal = 0.8 * envelope * np.sin(np.pi / 2 * np.arange(len(time)) + np.pi / 4)
        path = self.tmp / "quarter-rate.flac"
        sf.write(path, signal, rate, subtype="PCM_24")
        peak = replaygain.measure(path).peak
        self.assertAlmostEqual(np.max(np.abs(signal)), 0.8 / np.sqrt(2), places=5)
        self.assertAlmostEqual(peak, 0.8, delta=0.015)

    def test_one_flat_top_straddling_two_blocks_is_counted_once(self):
        rate = 44100
        boundary = 3 * rate
        samples = np.tile([1000, -1000], boundary)
        samples[boundary - 2:boundary + 2] = 32000
        path = self.tmp / "flat-top.flac"
        sf.write(path, samples.astype(np.int16), rate, subtype="PCM_16")
        self.assertEqual(quality.measure_track(path)["clips"], 1)

    def test_cue_without_bom_and_embedded_art_survive_split(self):
        album = self.tmp / "cue"
        image = make_flac(album / "album.flac", seconds=3)
        art = make_image((50, 50))
        tags.embed_picture(image, art)
        (album / "album.cue").write_bytes(
            ('FILE "album.flac" WAVE\nTRACK 01 AUDIO\nTITLE "Начало"\nINDEX 01 00:00:00\n'
             'TRACK 02 AUDIO\nTITLE "Финал"\nINDEX 01 00:01:00\n').encode("utf-16-le"))
        self.assertEqual(cue.split_album(album), 1)
        self.assertEqual(tags.read_tags(album / "01.flac")["title"], "Начало")
        self.assertEqual(tags.get_embedded_picture(album / "02.flac"), art)

    def test_aac_payload_is_unchanged_through_import(self):
        source = make_m4a(self.downloads / "A" / "Album" / "one.m4a", codec="aac",
                          **{"\xa9nam": "One", "\xa9ART": "A", "\xa9alb": "Album"})

        def packets(path):
            with av.open(path) as container:
                return [bytes(packet) for packet in container.demux(audio=0) if packet.size]

        original = packets(source)
        with mock.patch.object(discogs, "fetch_album", return_value=None), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            self.assertTrue(pipeline.process_album(pipeline.find_new_albums()[0], pipeline.RunState()))
        dest, = (self.music / "A" / "Album").glob("*.m4a")
        self.assertEqual(packets(dest), original)

    def test_silent_track_has_measured_peaks_and_no_gain(self):
        from mutagen.flac import FLAC

        silence = self.tmp / "silent.flac"
        sf.write(silence, np.zeros(44100), 44100, subtype="PCM_16")
        self.assertFalse(replaygain.has_replaygain([silence]))
        replaygain.tag_album([silence])
        self.assertTrue(replaygain.has_replaygain([silence]))
        self.assertEqual(FLAC(silence)["replaygain_track_peak"], ["0.000000"])
        self.assertNotIn("replaygain_track_gain", FLAC(silence))

    def test_corrupt_stored_loudness_does_not_become_a_measurement(self):
        track = make_flac(self.tmp / "track.flac", replaygain_track_gain="n/a", replaygain_track_peak="nan")
        passport = quality.measure_track(track)
        self.assertIsNone(passport["lufs"])
        self.assertIsNone(passport["true_peak"])


class OpinionDecisions(TempLibraryCase):
    def test_malformed_gemini_reply_uses_fallback(self):
        invalid = mock.Mock(status_code=200, json=mock.Mock(return_value=[]))
        valid = mock.Mock(status_code=200, json=mock.Mock(return_value={
            "candidates": [{"content": {"parts": [{"text": "Оставить оригинал."}]}}],
        }))
        with mock.patch.object(config, "GEMINI_API_KEY", "test"), \
                mock.patch.object(opinion.requests, "post", side_effect=[invalid, valid]):
            self.assertEqual(opinion.ask_gemini({}), "Оставить оригинал.")
