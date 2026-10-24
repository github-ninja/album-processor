"""Splitting a whole-album image by its CUE sheet."""

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from albumproc import cue
from tests.helpers import TempLibraryCase, make_flac

# Tracks start at 0, 1 s and 2.4 s (00:02:30 = 180 CD frames); the image lasts 3 s
SHEET = """﻿REM GENRE "Электроника"
REM DATE 1999
PERFORMER "Ночные Снайперы"
TITLE "Альбом"
FILE "{name}" WAVE
  TRACK 01 AUDIO
    TITLE "Первая"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Вторая"
    PERFORMER "Гость"
    INDEX 00 00:00:70
    INDEX 01 00:01:00
  TRACK 03 AUDIO
    TITLE "Третья"
    INDEX 01 00:02:30
"""


def write_cue(path, image_name, encoding="utf-8"):
    path.write_bytes(SHEET.format(name=image_name).encode(encoding, errors="ignore"))
    return path


class SplitImage(TempLibraryCase):
    def test_tracks_are_cut_at_index_01_sample_exact_and_tagged(self):
        image = make_flac(self.tmp / "album.flac", seconds=3)
        before, _ = sf.read(image, dtype="int16")
        write_cue(self.tmp / "album.cue", "album.wav", encoding="cp1251")  # stale extension, legacy code page

        self.assertEqual(cue.split_album(self.tmp), 1)

        tracks = [self.tmp / f"0{n}.flac" for n in (1, 2, 3)]
        parts = [sf.read(t, dtype="int16")[0] for t in tracks]
        self.assertEqual([len(p) for p in parts], [44100, 61740, 26460])
        self.assertTrue(np.array_equal(np.concatenate(parts), before))
        self.assertFalse(image.exists())
        second = FLAC(tracks[1])
        self.assertEqual((second["title"], second["artist"], second["albumartist"], second["album"]),
                         (["Вторая"], ["Гость"], ["Ночные Снайперы"], ["Альбом"]))
        self.assertEqual((second["tracknumber"], second["date"], second["genre"]), (["2"], ["1999"], ["Электроника"]))

    def test_24_bit_image_is_copied_exactly(self):
        image = make_flac(self.tmp / "album.flac", seconds=3, subtype="PCM_24")
        before, _ = sf.read(image, dtype="int32")
        write_cue(self.tmp / "album.cue", "album.flac")
        cue.split_album(self.tmp)
        parts = [sf.read(self.tmp / f"0{n}.flac", dtype="int32")[0] for n in (1, 2, 3)]
        self.assertEqual(sf.info(str(self.tmp / "01.flac")).subtype, "PCM_24")
        self.assertTrue(np.array_equal(np.concatenate(parts), before))

    def test_two_images_in_one_folder_become_two_discs(self):
        for n in (1, 2):
            make_flac(self.tmp / f"CD{n}.flac", seconds=3)
            write_cue(self.tmp / f"CD{n}.cue", f"CD{n}.flac")
        self.assertEqual(cue.split_album(self.tmp), 2)
        self.assertEqual(sorted(p.name for p in self.tmp.glob("*.flac")),
                         ["1-01.flac", "1-02.flac", "1-03.flac", "2-01.flac", "2-02.flac", "2-03.flac"])
        self.assertEqual(FLAC(self.tmp / "2-01.flac")["discnumber"], ["2"])

    def test_already_split_tracks_are_left_alone(self):
        make_flac(self.tmp / "01.flac")
        make_flac(self.tmp / "02.flac")
        write_cue(self.tmp / "album.cue", "album.flac")
        self.assertEqual(cue.split_album(self.tmp), 0)

    def test_sheet_longer_than_image_changes_nothing(self):
        image = make_flac(self.tmp / "album.flac", seconds=2)  # shorter than track 3's start
        write_cue(self.tmp / "album.cue", "album.flac")
        with self.assertRaises(RuntimeError):
            cue.split_album(self.tmp)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["album.cue", "album.flac", "downloads", "music"])
        self.assertTrue(image.exists())
