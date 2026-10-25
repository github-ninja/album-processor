"""Anton's opinions: the dossier and the Gemini call, with the network mocked."""

import json
from unittest import mock

from albumproc import config, discogs, opinion, replaygain
from tests.helpers import TempLibraryCase, make_flac


def gemini_reply(text: str, status: int = 200, error: str = "") -> mock.Mock:
    body = ({"candidates": [{"content": {"parts": [{"text": "thinking…", "thought": True}, {"text": text}]}}]}
            if status == 200 else {"error": {"message": error}})
    return mock.Mock(status_code=status, json=mock.Mock(return_value=body))


class Opinion(TempLibraryCase):
    def setUp(self):
        super().setUp()
        self.album = self.music / "Hidden Orchestra" / "2010 - Night Walks"
        files = [make_flac(self.album / f"0{n}. Hidden Orchestra - Track {n}.flac", seconds=4, tracknumber=str(n),
                           title=f"Track {n}", artist="Hidden Orchestra", albumartist="Hidden Orchestra",
                           album="Night Walks", date="2010") for n in (1, 2)]
        replaygain.tag_album(files)
        found = {"release_id": 1, "master_id": 2, "title": "Night Walks", "artist": "Hidden Orchestra", "year": "2010"}
        history = {"release": {"credits": ["Mastered By: Andrew Baldwin"]},
                   "versions": [{"title": "Night Walks", "year": "2010", "format": "CD", "label": "Tru Thoughts"}]}
        for patcher in (mock.patch.object(discogs, "fetch_album", return_value=found),
                        mock.patch.object(discogs, "edition_history", return_value=history),
                        mock.patch.object(config, "GEMINI_API_KEY", "test-key")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_dossier_has_passport_editions_and_listener(self):
        (config.state_dir()).mkdir(parents=True, exist_ok=True)
        opinion.listener_path().write_text("Слушает на Audeze Maxwell.", encoding="utf-8")
        facts = opinion.dossier(self.album)
        self.assertEqual((facts["album"]["title"], facts["album"]["year"]), ("Night Walks", "2010"))
        self.assertEqual(len(facts["passport"]["tracks"]), 2)
        self.assertIn("Andrew Baldwin", json.dumps(facts["discogs"], ensure_ascii=False))
        self.assertEqual(facts["listener"], "Слушает на Audeze Maxwell.")

    def test_opinion_from_gemini_without_its_thoughts(self):
        with mock.patch.object(opinion.requests, "post", return_value=gemini_reply("Оставь как есть.")) as post:
            code, text = opinion.run("Night Walks")
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("💬 Антон о «Hidden Orchestra — Night Walks»"))
        self.assertTrue(text.endswith("Оставь как есть."))
        self.assertNotIn("thinking", text)
        request = post.call_args
        self.assertIn(config.GEMINI_MODEL, request.args[0])
        self.assertEqual(request.kwargs["headers"], {"x-goog-api-key": "test-key"})
        self.assertIn("Факты — только из досье", request.kwargs["json"]["system_instruction"]["parts"][0]["text"])

    def test_errors_are_explained(self):
        cases = ((400, "User location is not supported for the API use.", "GEMINI_PROXY"),
                 (429, "Resource has been exhausted", "лимит"))
        for status, error, hint in cases:
            with mock.patch.object(opinion.requests, "post", return_value=gemini_reply("", status, error)):
                code, text = opinion.run("Night Walks")
            self.assertEqual(code, 1)
            self.assertIn(hint, text)
        with mock.patch.object(config, "GEMINI_API_KEY", ""):
            self.assertIn("GEMINI_API_KEY", opinion.run("Night Walks")[1])

    def test_ambiguous_or_unknown_album(self):
        make_flac(self.music / "Hidden Orchestra" / "2012 - Archipelago" / "01.flac")
        self.assertIn("уточните", opinion.run("Hidden Orchestra")[1])
        self.assertIn("Не нашёл", opinion.run("Nonexistent")[1])
