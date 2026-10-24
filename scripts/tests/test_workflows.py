"""End-to-end: processing a download and fixing a library, with the network mocked."""

import os
from unittest import mock

from mutagen.flac import FLAC

from albumproc import config, covers, discogs, library, pipeline, replaygain
from tests.helpers import TempLibraryCase, make_flac, make_image, make_m4a

DISCOGS_ALBUM = {
    "title": "Antrakt", "artist": "Dissident; Cyberworm", "year": "2017",
    "genre": "Drum n Bass; Jungle", "tracklist": [{"position": "1", "title": "Zerkalo"}],
}


class ProcessDownload(TempLibraryCase):
    def setUp(self):
        super().setUp()
        self.album = self.downloads / "Dissident & Cyberworm" / "Antrakt (2017) [FLAC]"
        make_flac(self.album / "01.flac", tracknumber="1", title="Track 1", artist=["Dissident", "Cyberworm"])
        make_flac(self.album / "02.flac", sr=48000, subtype="PCM_24",
                  tracknumber="2", title="Spiritual Existential Funk", artist="Dissident")
        (self.album / "info.txt").write_text("junk")
        (self.album / "cover.png").write_bytes(make_image((1400, 1400), fmt="PNG"))
        for patcher in (mock.patch.object(discogs, "fetch_album", return_value=DISCOGS_ALBUM),
                        mock.patch.object(covers, "fetch_deezer_cover", return_value=None),
                        mock.patch.object(config, "UPCONVERT_ACTION", "warn")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_pipeline(self):
        state = pipeline.RunState()
        candidates = pipeline.find_new_albums()
        self.assertEqual(len(candidates), 1)
        return pipeline.process_album(candidates[0], state)

    def test_album_lands_in_library_named_and_tagged_from_tags_and_discogs(self):
        self.assertTrue(self.run_pipeline())
        dest = self.music / "Dissident" / "2017 - Antrakt"
        self.assertEqual(sorted(p.name for p in dest.iterdir()), [
            "01. Dissident & Cyberworm - Zerkalo.flac",
            "02. Dissident - Spiritual Existential Funk.flac",
            "Cover.jpg",
        ])
        first = FLAC(dest / "01. Dissident & Cyberworm - Zerkalo.flac")
        self.assertEqual(first["albumartist"], ["Dissident", "Cyberworm"])
        self.assertEqual(first["genre"], ["Drum n Bass", "Jungle"])
        self.assertEqual((first["album"], first["date"], first["tracktotal"]), (["Antrakt"], ["2017"], ["2"]))
        second = FLAC(dest / "02. Dissident - Spiritual Existential Funk.flac")
        self.assertEqual((second.info.bits_per_sample, second.info.sample_rate), (16, 44100))
        self.assertEqual(second["title"], ["Spiritual Existential Funk"])
        self.assertEqual(covers.image_size((dest / "Cover.jpg").read_bytes()), (1000, 1000))
        self.assertEqual(first.pictures[0].data, (dest / "Cover.jpg").read_bytes())
        self.assertEqual(os.stat(dest).st_mode & 0o777, config.DIR_MODE)
        self.assertFalse((self.downloads / "Dissident & Cyberworm").exists())

    def test_track_artist_takes_the_album_artist_spelling(self):
        make_flac(self.album / "01.flac", tracknumber="1", title="Track 1", artist=["DISSIDENT", "cyberworm"])
        self.assertTrue(self.run_pipeline())
        self.assertTrue((self.music / "Dissident" / "2017 - Antrakt" / "01. Dissident & Cyberworm - Zerkalo.flac").exists())

    def test_display_credit_next_to_its_guests_is_the_album_artist(self):
        make_flac(self.album / "01.flac", tracknumber="1", title="Nova",
                  artist=["Dissident & Cyberworm & Camo & Krooked", "Camo & Krooked"])
        make_flac(self.album / "02.flac", tracknumber="2", title="Horizon",
                  artist=["Skrillex, Dissident, Thys & Sleepnet", "Skrillex"])
        self.assertTrue(self.run_pipeline())
        dest = self.music / "Dissident" / "2017 - Antrakt"
        self.assertEqual(FLAC(dest / "01. Dissident, Cyberworm & Camo & Krooked - Nova.flac")["artist"],
                         ["Dissident", "Cyberworm", "Camo & Krooked"])
        self.assertEqual(FLAC(dest / "02. Dissident & Skrillex - Horizon.flac")["artist"], ["Dissident", "Skrillex"])

    def test_alac_track_becomes_flac(self):
        (self.album / "02.flac").unlink()
        make_m4a(self.album / "02.m4a", sr=48000, sample_format="s32p",
                 **{"trkn": [(2, 2)], "\xa9nam": "Spiritual Existential Funk", "\xa9ART": "Dissident"})
        self.assertTrue(self.run_pipeline())
        second = FLAC(self.music / "Dissident" / "2017 - Antrakt" / "02. Dissident - Spiritual Existential Funk.flac")
        self.assertEqual((second.info.bits_per_sample, second.info.sample_rate), (16, 44100))

    def test_existing_album_is_moved_aside_not_deleted(self):
        old = self.music / "Dissident" / "2017 - Antrakt"
        old.mkdir(parents=True)
        (old / "old.flac").write_bytes(b"old")
        self.assertTrue(self.run_pipeline())
        replaced = list((config.state_dir() / "replaced").rglob("old.flac"))
        self.assertEqual(len(replaced), 1)

    def test_same_album_under_another_year_or_case_is_replaced(self):
        old = self.music / "Dissident" / "2020 - antrakt"
        old.mkdir(parents=True)
        (old / "old.flac").write_bytes(b"old")
        other = self.music / "Dissident" / "2018 - Antrakt II"
        other.mkdir()
        self.assertTrue(self.run_pipeline())
        self.assertEqual(sorted(p.name for p in (self.music / "Dissident").iterdir()),
                         ["2017 - Antrakt", "2018 - Antrakt II"])
        self.assertEqual(len(list((config.state_dir() / "replaced").rglob("old.flac"))), 1)

    def test_lossy_album_is_deferred_in_ask_mode(self):
        lossy = self.downloads / "Plaid" / "Fake"
        make_flac(lossy / "01.flac", seconds=12, lowpass_hz=16_000, tracknumber="1", title="A", artist="Plaid")
        state = pipeline.RunState()
        with mock.patch.object(config, "UPCONVERT_ACTION", "ask"):
            result = pipeline.process_album(pipeline.AlbumCandidate(lossy, "Plaid"), state)
        self.assertIsNone(result)
        self.assertEqual(state.deferred[0]["verdict"], "lossy")
        self.assertTrue(lossy.exists())


class DownloadCutsCollaborators(TempLibraryCase):
    """The download credits only the first artist of a joint album; Discogs lists both."""

    def test_album_and_its_tracks_get_the_full_discogs_credit(self):
        album = self.downloads / "Catching Flies" / "Beats & Pieces"
        make_flac(album / "01.flac", tracknumber="1", title="Sundial Avenue", artist="Catching Flies")
        make_flac(album / "02.flac", tracknumber="2", title="Iajo (feat. Fejká)", artist=["Catching Flies", "Fejka"])
        make_flac(album / "03.flac", tracknumber="3", title="Rain (Thorjn Remix)", artist=["Catching Flies", "Thorjn"])
        found = {"title": "Beats & Pieces", "artist": "Catching Flies; The West Green Quartet",
                 "year": "2025", "genre": "Trip Hop", "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None), \
                mock.patch.object(config, "UPCONVERT_ACTION", "warn"):
            self.assertTrue(pipeline.process_album(pipeline.AlbumCandidate(album, "Catching Flies"),
                                                   pipeline.RunState()))
        dest = self.music / "Catching Flies" / "2025 - Beats & Pieces"
        self.assertEqual(sorted(p.name for p in dest.glob("*.flac")), [
            "01. Catching Flies & The West Green Quartet - Sundial Avenue.flac",
            "02. Catching Flies & Fejka - Iajo.flac",
            "03. Catching Flies & The West Green Quartet - Rain (Thorjn Remix).flac",
        ])
        first = FLAC(next(dest.glob("01.*")))
        self.assertEqual(first["artist"], ["Catching Flies", "The West Green Quartet"])
        self.assertEqual(first["albumartist"], ["Catching Flies", "The West Green Quartet"])

    def test_joint_artist_written_as_one_value_is_split(self):
        album = self.downloads / "Noisia & Mayhem" / "05-07 - Exodus - Single"
        make_flac(album / "01.flac", tracknumber="1", title="Exodus", artist=["Noisia & Mayhem", "KRS-One"])
        found = {"title": "Exodus", "artist": "Noisia; Mayhem", "year": "2007", "genre": "Drum n Bass", "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found) as fetch, \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None), \
                mock.patch.object(config, "UPCONVERT_ACTION", "warn"):
            self.assertTrue(pipeline.process_album(pipeline.AlbumCandidate(album, "Noisia & Mayhem"),
                                                   pipeline.RunState()))
        track = self.music / "Noisia" / "2007 - Exodus" / "01. Noisia, Mayhem & KRS-One - Exodus.flac"
        self.assertEqual(FLAC(track)["artist"], ["Noisia", "Mayhem", "KRS-One"])
        self.assertEqual(FLAC(track)["albumartist"], ["Noisia", "Mayhem"])

    def test_alias_on_discogs_and_in_track_tags_is_adopted(self):
        album = self.downloads / "Carl Finlow - Bioroid"
        for n, title in enumerate(("Thrusters", "Nucleon", "Paradigm"), 1):
            make_flac(album / f"0{n}.flac", tracknumber=str(n), title=title, artist="Silicon Scally",
                      albumartist="Carl Finlow")
        found = {"title": "Bioroid", "artist": "Silicon Scally", "year": "2007", "genre": "Electro", "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found):
            info = pipeline.resolve_album_info(pipeline.AlbumCandidate(album, None))
        self.assertEqual(info.artist, "Silicon Scally")

    def test_joint_credit_written_as_one_value_takes_the_discogs_credit(self):
        album = self.downloads / "Noisia, Calyx, TeeBee & Evol Intent" / "Hyenas, The Liquid"
        make_flac(album / "01.flac", tracknumber="1", title="Hyenas", artist="Noisia, Calyx & TeeBee")
        found = {"title": "Hyenas / The Liquid", "artist": "Noisia; Calyx & Teebee; Evol Intent", "year": "2013",
                 "genre": "Drum n Bass", "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found):
            info = pipeline.resolve_album_info(pipeline.AlbumCandidate(album, "Noisia, Calyx, TeeBee & Evol Intent"))
        self.assertEqual(info.artist, "Noisia; Calyx & Teebee; Evol Intent")
        with mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            pipeline.process_tracks(album, info)
            pipeline.move_to_library(album, info)
        track = self.music / "Noisia" / "2013 - Hyenas, The Liquid" / "01. Noisia & Calyx & Teebee - Hyenas.flac"
        self.assertEqual(FLAC(track)["artist"], ["Noisia", "Calyx & Teebee"])

    def test_different_discogs_artist_is_not_adopted(self):
        album = self.downloads / "Plaid" / "Spokes"
        make_flac(album / "01.flac", tracknumber="1", title="Even Spring", artist="Plaid")
        found = {"title": "Spokes", "artist": "Someone Else", "year": "2003", "genre": "", "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found):
            info = pipeline.resolve_album_info(pipeline.AlbumCandidate(album, "Plaid"))
        self.assertEqual((info.artist, info.download_artist), ("Plaid", ""))


class AlbumFolderNamedAfterArtist(TempLibraryCase):
    """In Utero downloaded into a folder called just "Nirvana"."""

    def setUp(self):
        super().setUp()
        self.album = self.downloads / "Nirvana"
        for n, title in enumerate(["Serve the Servants", "Scentless Apprentice", "Heart-Shaped Box"], 1):
            make_flac(self.album / f"{n:02}.flac", tracknumber=str(n), title=title,
                      artist="Nirvana", album="In Utero (24kt Gold Plated)")

    def resolve(self, found):
        with mock.patch.object(discogs, "fetch_album", return_value=found) as fetch:
            info = pipeline.resolve_album_info(pipeline.AlbumCandidate(self.album, None))
        return info, fetch.call_args.args

    def test_album_tag_is_searched_instead_of_the_folder_name(self):
        found = {"title": "In Utero", "artist": "Nirvana", "year": "1993", "genre": "Grunge", "tracklist": [
            {"position": "1", "title": "Serve The Servants"}, {"position": "2", "title": "Scentless Apprentice"},
            {"position": "3", "title": "Heart-Shaped Box"}, {"position": "4", "title": "Rape Me"}]}
        info, args = self.resolve(found)
        self.assertEqual(args, ("Nirvana", "In Utero (24kt Gold Plated)"))
        self.assertEqual((info.album, info.year), ("In Utero", "1993"))

    def test_release_with_other_tracks_is_ignored(self):
        # The 60s band's "Nirvana" album
        found = {"title": "Nirvana", "artist": "Nirvana", "year": "1990", "genre": "Rock", "tracklist": [
            {"position": "1", "title": "Pentecost Hotel"}, {"position": "2", "title": "Rainbow Chaser"},
            {"position": "3", "title": "Tiny Goddess"}]}
        info, _ = self.resolve(found)
        self.assertEqual((info.album, info.year, info.genre), ("In Utero (24kt Gold Plated)", "", ""))


class DiscogsTitleTypo(TempLibraryCase):
    def test_artist_only_search_finds_a_misspelt_title(self):
        releases = [
            {"title": "Catching Flies (2) & The West Green Quartet - Beats & Pieces", "master_id": 1,
             "resource_url": "u1", "year": "2025"},
            {"title": "Catching Flies (2) - Beats & Pieces II", "master_id": 2,
             "resource_url": "u2", "year": "2026"},
        ]
        searches = []

        def search(**params):
            searches.append(params)
            return releases if "release_title" not in params and "q" not in params else []

        details = {"u2": {"title": "Beats & Pieces II", "year": 2026, "artists": [{"name": "Catching Flies (2)"}]}}
        with mock.patch.object(discogs, "search", side_effect=search), \
                mock.patch.object(discogs, "api_get", side_effect=lambda url, **kw: details.get(url, {})):
            found = discogs.fetch_album("Catching Flies", "Beats & Peaces II")
        self.assertEqual((found["title"], found["artist"], found["year"]),
                         ("Beats & Pieces II", "Catching Flies", "2026"))
        self.assertEqual(len(searches), 3)


class DiscogsVersionOfAnAlbum(TempLibraryCase):
    def test_instrumentals_keep_their_title_and_editions_take_the_masters(self):
        details = {
            "u1": {"title": "I Am Legion (Instrumentals)", "year": 2013, "master_id": 7, "artists": [{"name": "I Am Legion"}]},
            "u2": {"title": "I Am Legion (Deluxe Edition)", "year": 2013, "master_id": 7, "artists": [{"name": "I Am Legion"}]},
            f"{discogs._API}/masters/7": {"title": "I Am Legion", "year": 2013},
        }
        for url, expected in (("u1", "I Am Legion (Instrumentals)"), ("u2", "I Am Legion")):
            results = [{"title": f"I Am Legion - {details[url]['title']}", "master_id": 7, "resource_url": url}]
            with mock.patch.object(discogs, "search", return_value=results), \
                    mock.patch.object(discogs, "api_get", side_effect=lambda u, **kw: details.get(u, {})):
                self.assertEqual(discogs.fetch_album("I Am Legion", details[url]["title"])["title"], expected)


class FixLibrary(TempLibraryCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(discogs, "fetch_album", return_value=None),
                        mock.patch.object(discogs, "canonical_artist_name", return_value=None),
                        mock.patch.object(covers, "fetch_deezer_cover", return_value=None)):
            patcher.start()
            self.addCleanup(patcher.stop)

        album = self.music / "The Offspring" / "1998 - Americana"
        common = dict(album="Americana", date="1998", genre="Punk Rock, Pop Rock", totaltracks="2")
        make_flac(album / "1. The Offspring - Welcome.flac", sr=96000, subtype="PCM_24", tracknumber="1",
                  title="Welcome ", artist="The Offspring", **common)
        make_flac(album / "02. The Offspring - Have You Ever.flac", tracknumber="2", title="Have You Ever",
                  artist="The Offspring", **common)
        (album / "Сover.jpg").write_bytes(make_image((1200, 1200)))  # Cyrillic С

        zw_album = self.music / "The Flashbulb" / "2022 - Kirlian Tapes v1\u200b.\u200b0"
        make_flac(zw_album / "01. The Flashbulb - Treaty.flac", tracknumber="1", title="Treaty",
                  artist="The Flashbulb", albumartist="The Flashbulb", album="Kirlian Tapes v1\u200b.\u200b0",
                  date="2022", genre="IDM", tracktotal="1", discnumber="1", disctotal="1")
        (zw_album / "Cover.jpg").write_bytes(make_image((1000, 1000)))

    def test_plan_apply_converges(self):
        plan = library.plan_library_fixes()
        self.assertEqual(len(plan), 2)
        zw_entry, entry = plan
        self.assertEqual(zw_entry["new_folder"], "2022 - Kirlian Tapes v1.0")
        fields = {(rel, field): new for rel, field, _, new in entry["tag_fixes"]}
        self.assertEqual(fields[("1. The Offspring - Welcome.flac", "title")], ["Welcome"])
        self.assertEqual(fields[("1. The Offspring - Welcome.flac", "albumartist")], ["The Offspring"])
        self.assertEqual(fields[("1. The Offspring - Welcome.flac", "genre")], ["Punk Rock", "Pop Rock"])
        self.assertEqual(fields[("1. The Offspring - Welcome.flac", "totaltracks")], [])
        self.assertEqual(entry["cover"]["remove"], ["Сover.jpg"])
        self.assertEqual(entry["convert"], [["1. The Offspring - Welcome.flac", "24/96"]])

        self.assertEqual(library.apply_library_fixes(plan), 0)
        album = self.music / "The Offspring" / "1998 - Americana"
        self.assertEqual(sorted(p.name for p in album.iterdir()), [
            "01. The Offspring - Welcome.flac", "02. The Offspring - Have You Ever.flac", "Cover.jpg",
        ])
        self.assertEqual(covers.image_size((album / "Cover.jpg").read_bytes()), (1000, 1000))
        welcome = FLAC(album / "01. The Offspring - Welcome.flac")
        self.assertEqual((welcome.info.sample_rate, welcome["title"], welcome["albumartist"]),
                         (44100, ["Welcome"], ["The Offspring"]))
        self.assertTrue((self.music / "The Flashbulb" / "2022 - Kirlian Tapes v1.0").is_dir())
        self.assertEqual(library.plan_library_fixes(), [])  # nothing left: fixes are a fixpoint


class ReleaseTypeAndRemixers(TempLibraryCase):
    def test_download_gets_release_type_and_remixers(self):
        album = self.downloads / "Noisia" / "Outer Edges (Remixes)"
        make_flac(album / "01.flac", tracknumber="1", title="Mantra (Mat Zo Remix)", artist="Noisia")
        make_flac(album / "02.flac", tracknumber="2", title="Voodoo (Noisia's 'Outer Edges' Remix)", artist="Noisia")
        found = {"title": "Outer Edges (Remixes)", "artist": "Noisia", "year": "2017", "genre": "Drum n Bass",
                 "release_types": ["album", "remix"], "tracklist": []}
        with mock.patch.object(discogs, "fetch_album", return_value=found), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            self.assertTrue(pipeline.process_album(pipeline.find_new_albums()[0], pipeline.RunState()))
        dest = self.music / "Noisia" / "2017 - Outer Edges (Remixes)"
        first = FLAC(dest / "01. Noisia - Mantra (Mat Zo Remix).flac")
        self.assertEqual((first["releasetype"], first["remixer"]), (["album", "remix"], ["Mat Zo"]))
        self.assertNotIn("remixer", FLAC(dest / "02. Noisia - Voodoo (Noisia's 'Outer Edges' Remix).flac"))
        self.assertNotIn("compilation", first)

    def test_release_types_from_discogs_formats(self):
        release = lambda descriptions, **extra: {"formats": [{"descriptions": descriptions}], **extra}  # noqa: E731
        self.assertEqual(discogs.release_types(release(["12\"", "EP"])), ["ep"])
        self.assertEqual(discogs.release_types(release(["Album", "Compilation"], styles=["Soundtrack"])),
                         ["album", "compilation", "soundtrack"])
        self.assertEqual(discogs.release_types(release(["12\""], tracklist=[{"title": "A"}, {"title": "B"}])),
                         ["single"])
        self.assertEqual(discogs.album_release_types([], 12, "Various Artists"), ["album", "compilation"])


class LibraryScan(TempLibraryCase):
    def test_windows_recycle_bin_is_not_an_artist(self):
        make_flac(self.music / "$RECYCLE.BIN" / "$R1UTRLC" / "01.flac")
        make_flac(self.music / "Plaid" / "2003 - Spokes" / "01.flac")
        self.assertEqual([d.name for d in library.library_album_dirs()], ["2003 - Spokes"])


class FixLibraryDuplicates(TempLibraryCase):
    def test_older_copy_of_an_album_is_moved_aside(self):
        with mock.patch.object(discogs, "fetch_album", return_value=None), \
                mock.patch.object(discogs, "canonical_artist_name", return_value=None), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            folders = {}
            for name, mtime in (("2020 - Hybrid Theory", 1_000), ("2000 - Hybrid Theory", 2_000)):
                album = self.music / "Linkin Park" / name
                make_flac(album / "01. Linkin Park - Papercut.flac", tracknumber="1", tracktotal="1",
                          discnumber="1", disctotal="1", title="Papercut", artist="Linkin Park",
                          albumartist="Linkin Park", album="Hybrid Theory", date=name[:4], genre="Nu Metal",
                          releasetype="album")
                (album / "Cover.jpg").write_bytes(make_image((1000, 1000)))
                library.embed_picture(album / "01. Linkin Park - Papercut.flac", (album / "Cover.jpg").read_bytes())
                replaygain.tag_album([album / "01. Linkin Park - Papercut.flac"])
                os.utime(album, (mtime, mtime))
                folders[name] = album

            plan = library.plan_library_fixes()
            self.assertEqual([(e["old_folder"], e.get("duplicate_of")) for e in plan],
                             [("2020 - Hybrid Theory", str(folders["2000 - Hybrid Theory"]))])
            self.assertEqual(library.apply_library_fixes(plan), 0)
            self.assertEqual([p.name for p in (self.music / "Linkin Park").iterdir()], ["2000 - Hybrid Theory"])
            self.assertTrue(list((config.state_dir() / "replaced").rglob("2020 - Hybrid Theory")))
            self.assertEqual(library.plan_library_fixes(), [])


class DiscogsPicksAlbumOverSingle(TempLibraryCase):
    def test_album_wins_over_same_named_single(self):
        results = [
            {"title": "Squarepusher - Do You Know Squarepusher?", "master_id": 1, "format": ["Vinyl", "Single"]},
            {"title": "Squarepusher - Do You Know Squarepusher", "master_id": 2, "format": ["CD", "Album"]},
        ]
        self.assertEqual(discogs._pick_result(results, "Do You Know Squarepusher")["master_id"], 2)


class FixLibraryJointFolder(TempLibraryCase):
    def test_joint_release_moves_to_the_first_artists_folder(self):
        with mock.patch.object(discogs, "fetch_album", return_value=None), \
                mock.patch.object(discogs, "canonical_artist_name", return_value=None), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            album = self.music / "Noisia & Mayhem" / "2007 - Exodus"
            make_flac(album / "01. Noisia & Mayhem - Exodus.flac", tracknumber="1", tracktotal="1",
                      discnumber="1", disctotal="1", title="Exodus", artist=["Noisia", "Mayhem"],
                      albumartist=["Noisia", "Mayhem"], album="Exodus", date="2007", genre="Drum n Bass")
            (album / "Cover.jpg").write_bytes(make_image((1000, 1000)))
            library.embed_picture(album / "01. Noisia & Mayhem - Exodus.flac", (album / "Cover.jpg").read_bytes())
            (self.music / "Noisia" / "2016 - Outer Edges").mkdir(parents=True)

            plan = library.plan_library_fixes()
            self.assertEqual([(e["old_artist_folder"], e["new_artist_folder"]) for e in plan], [("Noisia & Mayhem", "Noisia")])
            self.assertEqual(library.apply_library_fixes(plan), 0)
            self.assertTrue((self.music / "Noisia" / "2007 - Exodus" / "01. Noisia & Mayhem - Exodus.flac").exists())
            self.assertFalse((self.music / "Noisia & Mayhem").exists())
            self.assertEqual(library.plan_library_fixes(), [])


class MultiDiscDownloads(TempLibraryCase):
    def setUp(self):
        super().setUp()
        found = {"title": "Experience", "artist": "The Prodigy", "year": "1992", "genre": "Breakbeat",
                 "tracklist": [{"position": "2-1", "title": "Your Love", "featuring": [], "remixers": []}]}
        for patcher in (mock.patch.object(discogs, "fetch_album", return_value=found),
                        mock.patch.object(covers, "fetch_deezer_cover", return_value=None),
                        mock.patch.object(config, "UPCONVERT_ACTION", "warn")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check_library_album(self):
        album = self.music / "The Prodigy" / "1992 - Experience"
        self.assertEqual(sorted(str(p.relative_to(album)) for p in album.rglob("*.flac")), [
            "CD1/01. The Prodigy - Jericho.flac", "CD1/02. The Prodigy - Music Reach.flac",
            "CD2/01. The Prodigy - Your Love.flac",
        ])
        second = FLAC(album / "CD2" / "01. The Prodigy - Your Love.flac")
        self.assertEqual((second["discnumber"], second["disctotal"], second["tracktotal"]), (["2"], ["2"], ["1"]))
        first = FLAC(album / "CD1" / "01. The Prodigy - Jericho.flac")
        self.assertEqual((first["discnumber"], first["tracktotal"]), (["1"], ["2"]))

    def test_flat_download_with_disc_tags_is_split_into_cd_folders(self):
        album = self.downloads / "The Prodigy" / "Experience Expanded"
        make_flac(album / "a.flac", tracknumber="1", discnumber="1", title="Jericho", artist="The Prodigy")
        make_flac(album / "b.flac", tracknumber="2", discnumber="1/2", title="Music Reach", artist="The Prodigy")
        make_flac(album / "c.flac", tracknumber="1", discnumber="2/2", title="Track 1", artist="The Prodigy")
        self.assertTrue(pipeline.process_album(pipeline.find_new_albums()[0], pipeline.RunState()))
        self.check_library_album()

    def test_download_with_disc_folders_is_one_album(self):
        album = self.downloads / "The Prodigy - Experience Expanded"
        make_flac(album / "Disc 1" / "01.flac", tracknumber="1", title="Jericho", artist="The Prodigy")
        make_flac(album / "Disc 1" / "02.flac", tracknumber="2", title="Music Reach", artist="The Prodigy")
        make_flac(album / "Disc 2" / "01.flac", tracknumber="1", title="Your Love", artist="The Prodigy")
        (album / "Scans").mkdir()
        (album / "Scans" / "back.jpg").write_bytes(b"x")
        (album / "Disc 2" / "rip.log").write_text("log")
        candidates = pipeline.find_new_albums()
        self.assertEqual([c.path for c in candidates], [album])
        self.assertTrue(pipeline.process_album(candidates[0], pipeline.RunState()))
        self.check_library_album()
        self.assertEqual(sorted(p.name for p in (self.music / "The Prodigy" / "1992 - Experience").iterdir()),
                         ["CD1", "CD2"])


class FixLibraryFlatMultiDisc(TempLibraryCase):
    def test_flat_album_with_disc_tags_is_split(self):
        with mock.patch.object(discogs, "fetch_album", return_value=None), \
                mock.patch.object(discogs, "canonical_artist_name", return_value=None), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            album = self.music / "The Prodigy" / "1992 - Experience"
            common = dict(artist="The Prodigy", albumartist="The Prodigy", album="Experience", date="1992",
                          genre="Breakbeat", disctotal="2")
            make_flac(album / "01. The Prodigy - Jericho.flac", tracknumber="1", tracktotal="1", discnumber="1",
                      title="Jericho", **common)
            make_flac(album / "01. The Prodigy - Your Love.flac", tracknumber="1", tracktotal="1", discnumber="2",
                      title="Your Love", **common)
            (album / "Cover.jpg").write_bytes(make_image((1000, 1000)))
            for f in album.glob("*.flac"):
                library.embed_picture(f, (album / "Cover.jpg").read_bytes())

            plan = library.plan_library_fixes()
            self.assertEqual(sorted(plan[0]["file_renames"]), [
                ["01. The Prodigy - Jericho.flac", "CD1/01. The Prodigy - Jericho.flac"],
                ["01. The Prodigy - Your Love.flac", "CD2/01. The Prodigy - Your Love.flac"],
            ])
            self.assertEqual(library.apply_library_fixes(plan), 0)
            self.assertEqual(FLAC(album / "CD2" / "01. The Prodigy - Your Love.flac")["discnumber"], ["2"])
            self.assertEqual(library.plan_library_fixes(), [])


class ImageWithCueDownload(TempLibraryCase):
    def test_image_is_split_into_named_tracks(self):
        from tests.test_cue import write_cue

        album = self.downloads / "Ночные Снайперы" / "Альбом"
        make_flac(album / "Альбом.flac", seconds=3, sr=48000, subtype="PCM_24")
        write_cue(album / "Альбом.cue", "Альбом.wav")
        found = {"title": "Альбом", "artist": "Ночные Снайперы", "year": "1999", "genre": "Rock",
                 "tracklist": [{"position": str(n), "title": t} for n, t in enumerate(("Первая", "Вторая", "Третья"), 1)]}
        with mock.patch.object(discogs, "fetch_album", return_value=found), \
                mock.patch.object(covers, "fetch_deezer_cover", return_value=None):
            self.assertTrue(pipeline.process_album(pipeline.find_new_albums()[0], pipeline.RunState()))
        dest = self.music / "Ночные Снайперы" / "1999 - Альбом"
        self.assertEqual(sorted(p.name for p in dest.iterdir()), [
            "01. Ночные Снайперы - Первая.flac", "02. Гость - Вторая.flac", "03. Ночные Снайперы - Третья.flac"])
        audio = FLAC(dest / "02. Гость - Вторая.flac")
        self.assertEqual((audio.info.bits_per_sample, audio.info.sample_rate), (16, 44100))
