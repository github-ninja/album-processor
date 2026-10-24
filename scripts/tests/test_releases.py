"""New albums of the library's artists: filtering, namesakes, schedules (Deezer mocked)."""

import datetime
from unittest import mock

from albumproc import releases
from tests.helpers import TempLibraryCase, make_flac

TODAY = "2026-10-25"


def release(id_, title, date, record_type="album", genre=106):
    return {"id": id_, "title": title, "release_date": date, "record_type": record_type, "genre_id": genre}


class NewAlbums(TempLibraryCase):
    def test_only_new_unowned_albums_are_reported(self):
        found = releases.new_albums([
            release(1, "Gutterpunk", "2026-10-20"),
            release(2, "Gutterpump", "2026-10-20", "single"),
            release(3, "The Resonance IX", "2026-10-21", "ep"),
            release(4, "Outer Edges (Remixes)", "2026-10-21"),
            release(5, "Split The Atom (Deluxe Edition)", "2026-10-21"),
            release(6, "Split The Atom", "2026-10-22"),          # owned
            release(7, "Old Album", "2026-01-01"),               # before the last check
            release(8, "Next Year", "2027-01-01"),               # announced, not out
            release(9, "Gutterpunk", "2026-10-20"),              # explicit / clean twin
            release(10, "Reported", "2026-10-23"),
            release(11, "II Reworked", "2026-10-23"),
            release(12, "Rulfo/Voces/Ecos", "2026-10-23", genre=98),   # a namesake's classical album
            release(14, "Split The Atom (15th Anniversary Edition)", "2026-10-23"),
        ], owned=["split the atom"], since="2026-10-18", today=TODAY, notified={10}, genres=[106])
        self.assertEqual([(a["title"], a["doubt"]) for a in found],
                         [("Gutterpunk", ""), ("Rulfo/Voces/Ecos", "другой жанр: Classical")])
        self.assertEqual(found[0]["link"], "https://www.deezer.com/album/1")

    def test_album_missing_from_the_artist_page_is_found_by_search(self):
        # Deezer lists 159 releases on Akira Kosemura's page, "Polaroid Piano" not among them
        search = {"data": [{"id": 915649171, "title": "Polaroid Piano (15th Anniversary Edition)", "genre_id": 98,
                            "artist": {"id": 469604}}]}
        with mock.patch.object(releases, "_get", return_value=search):
            owned = releases.owned_on_deezer(469604, "Akira Kosemura", ["polaroid piano"], page=[])
        self.assertEqual([r["id"] for r in owned], [915649171])

    def test_report_parts(self):
        news = [{"artist": "Clark", "title": "Steep Stims II", "date": "2026-10-23", "doubt": "", "link": "L1"},
                {"artist": "DKay", "title": "CHAOS", "date": "2026-09-18", "doubt": "другой жанр: Rap/Hip Hop",
                 "link": "L2"}]
        text = releases.report(news, {"artists": {}})
        self.assertLess(text.index("🆕"), text.index("Clark"))
        self.assertLess(text.index("❓"), text.index("DKay — CHAOS (18.09.2026) — другой жанр: Rap/Hip Hop"))
        self.assertLess(text.index("Clark"), text.index("❓"))

    def test_namesake_is_told_apart_by_the_library(self):
        search = {"data": [{"id": 1, "name": "Pudra", "nb_fan": 5000}, {"id": 2, "name": "Pudra", "nb_fan": 10}]}
        discographies_genres = {2: 106}
        discographies = {1: [release(11, "Someone Else's Hit", "2020-01-01")],
                         2: [release(21, "Мечты", "2014-01-01")]}
        with mock.patch.object(releases, "_get", return_value=search), \
                mock.patch.object(releases, "albums_of", side_effect=lambda artist_id: discographies[artist_id]):
            self.assertEqual(releases.find_artist("Pudra", ["мечты"]), {"id": 2, "genres": [discographies_genres[2]]})
            # No page with an album of the library: the most followed, unverified
            self.assertEqual(releases.find_artist("Pudra", ["something else"]), {"id": 1, "genres": []})


class Schedule(TempLibraryCase):
    def state(self, schedule, last_check):
        return {"schedule": schedule, "last_check": last_check, "artists": {}, "notified": []}

    def test_due(self):
        sunday_noon = datetime.datetime(2026, 10, 25, 12)
        monday_9 = datetime.datetime(2026, 10, 26, 9)
        monday_11 = datetime.datetime(2026, 10, 26, 11)
        self.assertFalse(releases.is_due(self.state(None, None), sunday_noon))
        self.assertTrue(releases.is_due(self.state("daily", "2026-10-24"), sunday_noon))
        self.assertFalse(releases.is_due(self.state("daily", "2026-10-25"), sunday_noon))
        self.assertFalse(releases.is_due(self.state("weekly", "2026-10-20"), sunday_noon))
        self.assertFalse(releases.is_due(self.state("weekly", "2026-10-20"), monday_9))   # before 10:00
        self.assertTrue(releases.is_due(self.state("weekly", "2026-10-20"), monday_11))
        self.assertTrue(releases.is_due(self.state("monthly", "2026-09-30"), datetime.datetime(2026, 10, 3, 15)))
        self.assertEqual(releases.next_check(self.state("weekly", "2026-10-20"), sunday_noon),
                         datetime.datetime(2026, 10, 26, 10))
        self.assertEqual(releases.next_check(self.state("monthly", "2026-10-01"), sunday_noon),
                         datetime.datetime(2026, 11, 1, 10))

    def test_schedule_is_kept(self):
        releases.set_schedule("weekly")
        self.assertEqual(releases.load_state()["schedule"], "weekly")
        releases.set_schedule(None)
        self.assertIsNone(releases.load_state()["schedule"])


class Check(TempLibraryCase):
    def test_first_check_looks_back_90_days_and_news_is_told_once(self):
        make_flac(self.music / "Noisia" / "2010 - Split The Atom" / "01.flac")
        make_flac(self.music / "Various Artists" / "2002 - Blade II" / "01.flac")
        discography = [release(1, "Gutterpunk", "2026-09-01"), release(2, "Ancient", "2026-01-01"),
                       release(3, "Split The Atom", "2010-04-01")]
        with mock.patch.object(releases, "find_artist", return_value={"id": 6455, "genres": [106]}) as find, \
                mock.patch.object(releases, "albums_of", return_value=discography):
            news, state = releases.check(datetime.date(2026, 10, 25))
            self.assertEqual([(a["artist"], a["title"]) for a in news], [("Noisia", "Gutterpunk")])
            self.assertEqual(state["artists"], {"Noisia": {"id": 6455, "genres": [106]}})   # not Various Artists
            again, _ = releases.check(datetime.date(2026, 11, 2))
            self.assertEqual(again, [])
            find.assert_called_once()                               # the artist id is remembered
        self.assertIn("Noisia — Gutterpunk (01.09.2026)", releases.report(news, state))
        self.assertEqual(releases.report([], state, quiet=True), "")
