"""Tags, covers and spectral analysis on real (synthetic) files."""

import json
import os
from unittest import mock

import numpy as np
import soundfile as sf
from mutagen.flac import FLAC

from albumproc import audioformat, covers, quality, replaygain, tags, upconvert
from albumproc.tags import TrackMetadata
from mutagen.mp4 import MP4Cover

from tests.helpers import TempLibraryCase, make_flac, make_image, make_m4a


class Tags(TempLibraryCase):
    def test_multi_value_round_trip_and_totals_convention(self):
        f = make_flac(self.tmp / "a.flac", totaltracks="9")
        tags.write_tags(f, TrackMetadata(
            artist="Dissident; Cyberworm", title="Zerkalo", album="Antrakt", date="2017",
            genre="Drum n Bass; Jungle", album_artist="Dissident; Cyberworm", track_number=1, total_tracks=10,
        ))
        raw = FLAC(f)
        self.assertEqual(raw["artist"], ["Dissident", "Cyberworm"])
        self.assertEqual(raw["genre"], ["Drum n Bass", "Jungle"])
        self.assertEqual(raw["tracktotal"], ["10"])
        self.assertNotIn("totaltracks", raw)
        read = tags.read_tags(f)
        self.assertEqual(read["artist"], "Dissident; Cyberworm")
        self.assertEqual(read["track_number"], "1")

    def test_embedded_picture_has_dimensions(self):
        f = make_flac(self.tmp / "a.flac")
        jpeg = make_image((1000, 1000))
        tags.embed_picture(f, jpeg)
        pic = FLAC(f).pictures[0]
        self.assertEqual((pic.type, pic.width, pic.height), (3, 1000, 1000))
        self.assertEqual(tags.get_embedded_picture(f), jpeg)


class Covers(TempLibraryCase):
    def test_large_square_is_downscaled(self):
        out = covers.normalize_cover(make_image((1500, 1500)))
        self.assertEqual(covers.image_size(out), (1000, 1000))
        self.assertTrue(covers.is_standard(out))

    def test_small_is_not_upscaled_and_standard_is_untouched(self):
        small = make_image((600, 600))
        self.assertEqual(covers.normalize_cover(small), small)
        exact = make_image((1000, 1000))
        self.assertEqual(covers.normalize_cover(exact), exact)

    def test_png_and_grayscale_become_rgb_jpeg(self):
        for data in (make_image((800, 800), fmt="PNG"), make_image((800, 800), mode="L")):
            out = covers.normalize_cover(data)
            self.assertTrue(out.startswith(b"\xff\xd8"))
            self.assertEqual(covers.image_size(out), (800, 800))

    def test_not_square_is_kept(self):
        self.assertEqual(covers.image_size(covers.normalize_cover(make_image((1200, 1085)))), (1200, 1085))

    def test_similarity(self):
        a = make_image((1200, 1200), seed=1)
        self.assertGreater(covers.image_similarity(a, make_image((600, 600), seed=1)), covers.SAME_IMAGE_MIN_SIMILARITY)
        self.assertLess(covers.image_similarity(a, make_image((1200, 1200), seed=2)), covers.SAME_IMAGE_MIN_SIMILARITY)

    def test_cover_look_alikes(self):
        album = self.tmp / "album"
        (album / "CD1").mkdir(parents=True)
        for name in ("Сover.jpg", "cover.png", "Cover.jpg", "CD1/Cover.jpg"):  # first is Cyrillic С
            (album / name).write_bytes(make_image((10, 10)))
        found = [str(f.relative_to(album)) for f in covers.cover_like_files(album)]
        self.assertEqual(found[0], "Cover.jpg")
        self.assertEqual(sorted(found), sorted(["Cover.jpg", "Сover.jpg", "cover.png", "CD1/Cover.jpg"]))


class Upconvert(TempLibraryCase):
    def test_full_band_is_ok_and_lowpassed_is_lossy(self):
        clean = upconvert.analyse_track_spectrum(make_flac(self.tmp / "clean.flac", seconds=12))
        fake = upconvert.analyse_track_spectrum(make_flac(self.tmp / "fake.flac", seconds=12, lowpass_hz=16_000))
        self.assertEqual(clean.verdict, "ok")
        self.assertEqual(fake.verdict, "lossy")
        self.assertAlmostEqual(fake.cutoff_hz, 16_000, delta=300)


class AudioFormat(TempLibraryCase):
    def test_hires_becomes_16_44_with_tags_picture_and_level_kept(self):
        f = make_flac(self.tmp / "hr.flac", seconds=3, sr=96000, subtype="PCM_24", title="Track", artist=["A", "B"])
        tags.embed_picture(f, make_image((1000, 1000)))
        os.chmod(f, 0o644)
        before, _ = sf.read(f)
        self.assertTrue(audioformat.needs_conversion(f))

        audioformat.convert_to_library_format(f)

        audio = FLAC(f)
        self.assertEqual((audio.info.bits_per_sample, audio.info.sample_rate), (16, 44100))
        self.assertEqual((audio["title"], audio["artist"]), (["Track"], ["A", "B"]))
        self.assertEqual(len(audio.pictures), 1)
        self.assertEqual(os.stat(f).st_mode & 0o777, 0o644)
        after, _ = sf.read(f)
        self.assertLessEqual(abs(len(after) - len(before) * 44100 / 96000), 1)
        self.assertFalse(audioformat.needs_conversion(f))
        self.assertEqual(list(self.tmp.glob(".*")), [])

    def test_audible_tone_keeps_its_level(self):
        f = self.tmp / "tone.flac"
        t = np.arange(96000 * 2) / 96000
        sf.write(f, np.stack([0.5 * np.sin(2 * np.pi * 1000 * t)] * 2, axis=1), 96000, subtype="PCM_24")
        audioformat.convert_to_library_format(f)
        after, _ = sf.read(f)
        middle = after[4410:-4410]
        self.assertAlmostEqual(np.sqrt(np.mean(middle ** 2)), 0.5 / np.sqrt(2), delta=1e-4)

    def test_24_bit_at_44_is_only_dithered(self):
        f = make_flac(self.tmp / "b.flac", subtype="PCM_24")
        before, _ = sf.read(f)
        audioformat.convert_to_library_format(f)
        after, _ = sf.read(f)
        self.assertEqual(len(after), len(before))
        self.assertLess(np.abs(after - before).max(), 2 / 32768)  # ≤ rounding + 1 LSB dither

    def test_cd_format_and_lossy_files_are_left_alone(self):
        self.assertFalse(audioformat.needs_conversion(make_flac(self.tmp / "cd.flac")))
        self.assertFalse(audioformat.needs_conversion(self.tmp / "x.mp3"))
        self.assertFalse(audioformat.needs_conversion(make_m4a(self.tmp / "aac.m4a", codec="aac")))

    def test_alac_at_cd_resolution_becomes_identical_flac_with_tags_and_cover(self):
        cover = make_image((1000, 1000))
        f = make_m4a(self.tmp / "01 Track.m4a", **{"\xa9nam": "Track", "\xa9ART": "A", "trkn": [(1, 9)],
                                                   "covr": [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]})
        self.assertTrue(audioformat.needs_conversion(f))
        self.assertEqual(audioformat.describe_format(f), "ALAC 16/44.1")

        flac = audioformat.convert_to_library_format(f)

        self.assertEqual(flac, self.tmp / "01 Track.flac")
        self.assertFalse(f.exists())
        audio = FLAC(flac)
        self.assertEqual((audio.info.bits_per_sample, audio.info.sample_rate), (16, 44100))
        self.assertEqual((audio["title"], audio["artist"], audio["tracknumber"]), (["Track"], ["A"], ["1"]))
        self.assertEqual(audio.pictures[0].data, cover)
        # Decoded ALAC and the new FLAC hold the same samples, no dither added
        expected = np.concatenate([b for b in self._decode_alac(make_m4a(self.tmp / "01 Track.m4a"))])
        after, _ = sf.read(flac, dtype="int16")
        self.assertTrue(np.array_equal(after, expected))

    def test_hires_alac_becomes_16_44(self):
        f = make_m4a(self.tmp / "hr.m4a", sr=96000, sample_format="s32p")
        flac = audioformat.convert_to_library_format(f)
        audio = FLAC(flac)
        self.assertEqual((audio.info.bits_per_sample, audio.info.sample_rate), (16, 44100))
        self.assertLessEqual(abs(audio.info.total_samples - 2 * 44100), 1)

    @staticmethod
    def _decode_alac(path):
        import av
        with av.open(str(path)) as container:
            for frame in container.decode(audio=0):
                yield frame.to_ndarray().T


class ReplayGain(TempLibraryCase):
    def tone(self, name, dbfs):
        """10 s stereo 1 kHz sine with its peak at *dbfs*: BS.1770 rates it at *dbfs* LUFS."""
        t = np.arange(441000) / 44100
        wave = 10 ** (dbfs / 20) * np.sin(2 * np.pi * 1000 * t)
        sf.write(self.tmp / name, np.stack([wave, wave], axis=1), 44100, subtype="PCM_16")
        return self.tmp / name

    def test_track_and_album_gains_relative_to_minus_18_lufs(self):
        quiet, loud = self.tone("quiet.flac", -23), self.tone("loud.flac", -13)
        replaygain.tag_album([quiet, loud])
        gain = lambda f, field: float(FLAC(f)[field][0].split()[0])  # noqa: E731
        self.assertAlmostEqual(gain(quiet, "replaygain_track_gain"), 5.0, delta=0.05)
        self.assertAlmostEqual(gain(loud, "replaygain_track_gain"), -5.0, delta=0.05)
        # Equal lengths: the album is the energy mean, 10·log10((10^-2.3 + 10^-1.3) / 2) ≈ −15.6 LUFS
        self.assertAlmostEqual(gain(quiet, "replaygain_album_gain"), -2.4, delta=0.05)
        self.assertEqual(FLAC(quiet)["replaygain_album_gain"], FLAC(loud)["replaygain_album_gain"])
        self.assertAlmostEqual(float(FLAC(loud)["replaygain_track_peak"][0]), 10 ** (-13 / 20), delta=0.005)
        self.assertTrue(replaygain.has_replaygain([quiet, loud]))


class MasteringPassport(TempLibraryCase):
    def sine(self, name, gain):
        """10 s stereo 1 kHz sine, *gain* times full scale, hard-clipped at full scale."""
        t = np.arange(441000) / 44100
        wave = np.clip(gain * np.sin(2 * np.pi * 1000 * t), -1, 32767 / 32768)
        sf.write(self.tmp / name, np.stack([wave, wave], axis=1), 44100, subtype="PCM_16")
        return self.tmp / name

    def test_dr_and_flat_tops(self):
        clean = quality.measure_track(self.sine("clean.flac", 0.5))
        clipped = quality.measure_track(self.sine("clipped.flac", 4))
        self.assertAlmostEqual(clean["dr"], 0, delta=0.2)  # a sine's peak is √2 × its RMS: DR 0
        self.assertEqual(clean["clips"], 0)
        self.assertGreater(clipped["clips"], 10000)       # every half-wave is a flat top
        noise = quality.measure_track(make_flac(self.tmp / "noise.flac", seconds=12))
        self.assertGreater(noise["dr"], 8)                # Gaussian noise: crest factor ~4.5σ
        self.assertEqual(noise["seconds"], 12.0)

    def test_grades(self):
        passport = lambda dr, clips: {"tracks": [{"dr": dr, "clips": clips, "seconds": 60}]}  # noqa: E731
        self.assertEqual(quality.grade(passport(11, 0)), "🟢")
        self.assertEqual(quality.grade(passport(7, 0)), "🟡")
        self.assertEqual(quality.grade(passport(11, 20)), "🟡")
        self.assertEqual(quality.grade(passport(4, 0)), "🔴")
        self.assertEqual(quality.grade(passport(8, 500)), "🔴")
        self.assertEqual(quality.grade(passport(11, 500)), "🟡")   # open master, shaved peaks

    def test_passport_follows_the_audio_not_the_name(self):
        album = self.music / "Plaid" / "2003 - Spokes"
        make_flac(album / "01. Plaid - Even Spring.flac", seconds=4)
        first = quality.passport(album)
        renamed = album.rename(self.music / "Plaid" / "2003 - Spokes (renamed)")
        with mock.patch.object(quality, "measure_album") as measure:
            self.assertEqual(quality.passport(renamed), first)
            measure.assert_not_called()
        self.assertEqual(list(json.loads(quality.store_path().read_text())), ["Plaid/2003 - Spokes (renamed)"])
