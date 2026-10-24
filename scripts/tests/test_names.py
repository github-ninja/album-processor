import unittest

from albumproc import names


class CleanFolderName(unittest.TestCase):
    def test_junk_is_removed(self):
        cases = {
            "Atol Scrap (2021 Remaster)": "Atol Scrap",
            "Body Riddle (Remastered) [FLAC]": "Body Riddle",
            "Lemodie (15th Anniversary Edition) (2021)": "Lemodie",
            "II (Hires) [24-96]": "II",
            "2007 – Scars On The Horizon (2017 Remastered)": "Scars On The Horizon",
            "Rest Proof Clockwork [2019, Warp Records] [WARP123]": "Rest Proof Clockwork",
            "Album.Name.2019.FLAC": "Album Name",
            "Kid A - FLAC 24bit": "Kid A",
            "The Fat of the Land (Expanded Edition) {24-bit 96kHz WEB}": "The Fat of the Land",
        }
        for raw, expected in cases.items():
            self.assertEqual(names.clean_folder_name(raw), expected, raw)

    def test_store_suffix_and_numeric_prefix_are_removed(self):
        self.assertEqual(names.clean_folder_name("05-07 - Exodus - Single"), "Exodus")
        self.assertEqual(names.clean_folder_name("Nocturne - EP"), "Nocturne")
        self.assertEqual(names.clean_folder_name("Nocturne EP"), "Nocturne EP")
        self.assertEqual(names.clean_folder_name("2001 - Odyssey"), "Odyssey")
        self.assertEqual(names.clean_folder_name("2013-09-02 - I Am Legion [GB - DVSNCD003]"), "I Am Legion")

    def test_meaningful_brackets_and_short_names_are_kept(self):
        for title in ("Artifakts (BC)", "Blade (Music From And Inspired By The Motion Picture)",
                      "v2.0", "01.1", "Skal_Ghost", "1999"):
            self.assertEqual(names.clean_folder_name(title), title)

    def test_artist_prefix_with_any_dash(self):
        for title in ("Omni Trio - Even Angels", "Omni Trio – Even Angels", "omni trio — Even Angels"):
            self.assertEqual(names.strip_artist_prefix(title, "Omni Trio"), "Even Angels")
        self.assertEqual(names.strip_artist_prefix("Omni Trio", "Omni Trio"), "Omni Trio")


class FileNames(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(names.sanitize_path_component("Nova / Moth"), "Nova, Moth")
        self.assertEqual(names.sanitize_path_component("AC/DC"), "AC-DC")
        self.assertEqual(names.sanitize_path_component("Divinity: Original Sin 2"), "Divinity Original Sin 2")
        self.assertEqual(names.sanitize_path_component(".44", strip_dots=False), ".44")

    def test_artist_joining(self):
        self.assertEqual(names.credit_name("Plaid"), "Plaid")
        self.assertEqual(names.credit_name("Dissident; Cyberworm"), "Dissident & Cyberworm")
        self.assertEqual(names.credit_name("Kacper Krupa; Magiera; Skalpel"), "Kacper Krupa, Magiera & Skalpel")
        self.assertEqual(names.credit_name("Blocks & Escher"), "Blocks & Escher")
        # Folders: the first credited artist; a duo entity is one value
        self.assertEqual(names.artist_folder_name("Noisia; Mayhem"), "Noisia")
        self.assertEqual(names.artist_folder_name("Fred V & Grafix"), "Fred V & Grafix")

    def test_track_and_album_names(self):
        self.assertEqual(names.track_file_name(3, "A; B", "Song / Reprise", ".flac"), "03. A & B - Song, Reprise.flac")
        self.assertEqual(names.album_folder_name("1999", "Planescape: Torment"), "1999 - Planescape Torment")
        self.assertEqual(names.album_folder_name("", "Untitled"), "Untitled")


class Matching(unittest.TestCase):
    def test_subtitle_counts_as_match(self):
        self.assertGreaterEqual(names.title_similarity("Illinoize", "Illinoize: The Remix Tape"), 0.8)

    def test_different_titles(self):
        self.assertLess(names.title_similarity("Future Perfect", "1000% Drum'N'Bass Vol. 3"), 0.6)

    def test_genres_split_on_comma_but_not_slash(self):
        self.assertEqual(names.split_genres("Breaks, Big Beat; IDM"), ["Breaks", "Big Beat", "IDM"])
        self.assertEqual(names.split_genres("Funk / Soul"), ["Funk / Soul"])

    def test_clean_text(self):
        self.assertEqual(names.clean_text("Kirlian Tapes v1​.​0 "), "Kirlian Tapes v1.0")
        self.assertEqual(names.clean_text("Ganja Man  (ft. X)"), "Ganja Man (ft. X)")


if __name__ == "__main__":
    unittest.main()


class TrackCredits(unittest.TestCase):
    def credit(self, title, artists, album_artists=None, tracklist=None):
        return names.credit_track(title, artists, album_artists or artists[:1], tracklist)

    def test_featured_guest_moves_from_title_to_artists(self):
        self.assertEqual(self.credit("The Universe (feat. Jennifer Neal)", ["A Guy Called Gerald"]),
                         ("The Universe", ["A Guy Called Gerald", "Jennifer Neal"]))
        self.assertEqual(self.credit("Song [ft. B & C]", ["A"]), ("Song", ["A", "B", "C"]))

    def test_guest_already_credited_is_not_doubled(self):
        self.assertEqual(self.credit("Iajo (feat. Fejká)", ["Catching Flies", "Fejka"]),
                         ("Iajo", ["Catching Flies", "Fejka"]))

    def test_album_artist_without_featuring(self):
        # "Love Is The Devil" came with the first track's credit as album artist
        self.assertEqual(names.strip_featuring("Blu Mar Ten feat. Airwalker"), "Blu Mar Ten")
        self.assertEqual(names.strip_featuring("A (ft. B)"), "A")
        self.assertEqual(names.strip_featuring("Blocks & Escher"), "Blocks & Escher")

    def test_feat_inside_an_artist_value(self):
        self.assertEqual(self.credit("Skin to Bone", ["Linkin Park feat. Cody B. Ware & Ryu"], ["Linkin Park"]),
                         ("Skin to Bone", ["Linkin Park", "Cody B. Ware", "Ryu"]))

    def test_remixer_is_not_an_artist_unless_they_are_the_album_artist(self):
        self.assertEqual(self.credit("Embrace (Thorjn Remix)", ["Hudson Lee", "Thorjn"]),
                         ("Embrace (Thorjn Remix)", ["Hudson Lee"]))
        self.assertEqual(self.credit("Break Out (Skalpel remix)", ["Skalpel"]),
                         ("Break Out (Skalpel remix)", ["Skalpel"]))
        self.assertEqual(self.credit("Watch the Smoke Rise (John B Remix)", ["John B", "HydrA"]),
                         ("Watch the Smoke Rise (John B Remix)", ["John B", "HydrA"]))
        # A remix album credited to the remixer alone keeps its artist
        self.assertEqual(self.credit("Track (X Remix)", ["X"], ["Someone"]), ("Track (X Remix)", ["X"]))

    def test_discogs_roles_win(self):
        tracklist = [{"title": "Soul Arcs (Samuel Marquis Remix)", "featuring": [], "remixers": ["Samuel Marquis"]},
                     {"title": "Fearless", "featuring": ["Laura Brehm"], "remixers": []}]
        self.assertEqual(self.credit("Soul Arcs (Samuel Marquis Remix)", ["False Noise", "Hudson Lee", "Samuel Marquis"],
                                     ["Hudson Lee"], tracklist),
                         ("Soul Arcs (Samuel Marquis Remix)", ["False Noise", "Hudson Lee"]))
        self.assertEqual(self.credit("Fearless", ["Hudson Lee"], tracklist=tracklist), ("Fearless", ["Hudson Lee", "Laura Brehm"]))

    def test_discogs_placeholder_and_punctuation_variants(self):
        tracklist = [{"title": "Alien Report", "featuring": ["Unknown Artist"], "remixers": []},
                     {"title": "Plasticworld", "featuring": ["MC Fats", "T.C."], "remixers": []}]
        self.assertEqual(self.credit("Alien Report (feat. Public Domain)", ["Gerald"], tracklist=tracklist),
                         ("Alien Report", ["Gerald", "Public Domain"]))
        self.assertEqual(self.credit("Plasticworld (feat. MC Fats & TC)", ["Pendulum", "MC Fats", "TC"], ["Pendulum"],
                                     tracklist), ("Plasticworld", ["Pendulum", "MC Fats", "TC"]))

    def test_guest_spelt_otherwise_is_not_doubled(self):
        tracklist = [{"title": "Old Friend", "featuring": ["JP Cooper"], "remixers": []}]
        self.assertEqual(self.credit("Old Friend", ["Frameworks", "J P Cooper"], ["Frameworks"], tracklist),
                         ("Old Friend", ["Frameworks", "J P Cooper"]))
        tracklist = [{"title": "Woman", "featuring": ["Shapla"], "remixers": []}]
        self.assertEqual(self.credit("Woman", ["Nitin Sawhney", "Shapla Salique"], ["Nitin Sawhney"], tracklist),
                         ("Woman", ["Nitin Sawhney", "Shapla Salique"]))

    def test_remixers_from_discogs_or_the_title(self):
        self.assertEqual(names.track_remixers("Mantra (Mat Zo Remix)"), ["Mat Zo"])
        self.assertEqual(names.track_remixers("Make It Tonight (High Contrast VIP Remix)"), ["High Contrast"])
        for title in ("Voodoo (Noisia's 'Outer Edges' Remix)", "Track (2010 Remix)", "Track (VIP Remix)", "Track (Radio Edit)",
                      "Façade (VIP)", "Track (Slow Mix)", "Track (Funky Remix)", "What I've Done [distorted remix]",
                      "Track (Plaid Mix)"):
            self.assertEqual(names.track_remixers(title), [], title)
        tracklist = [{"title": "Embrace (Thorjn Remix)", "remixers": ["Thorjn"]}]
        self.assertEqual(names.track_remixers("Embrace (Thorjn Remix)", tracklist), ["Thorjn"])

    def test_versions_and_plain_titles_are_untouched(self):
        for title in ("Embrace", "Numb (Radio Edit)", "Arrakis (2025 Extended Mix)", "Faint [Live]"):
            self.assertEqual(self.credit(title, ["A"]), (title, ["A"]))


class DiscogsPositions(unittest.TestCase):
    def test_positions(self):
        from albumproc import discogs
        self.assertEqual(discogs._position("2-03"), (2, 3))
        self.assertEqual(discogs._position("CD2-3"), (2, 3))
        self.assertEqual(discogs._position("7"), (1, 7))
        self.assertEqual(discogs._position("B2"), (1, 2))
        tracklist = [{"position": "1-1", "title": "Jericho"}, {"position": "2-1", "title": "Your Love"}]
        self.assertEqual(discogs.resolve_track_title(tracklist, 1, 0, disc=2), "Your Love")

    def test_artist_list(self):
        from albumproc import discogs
        # Release 1992 "Experience" lists The Prodigy twice
        artists = [{"name": "The Prodigy"}, {"name": "The Prodigy"}, {"name": "Maxim (3)"}]
        self.assertEqual(discogs.join_artists(artists), "The Prodigy; Maxim")
