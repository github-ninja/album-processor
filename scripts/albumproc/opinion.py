"""
Anton's opinion on an album: a short verdict on its mastering and editions,
written by a Gemini model from a dossier of facts — never from the model's
own memory of the album, which is where invented remasters come from.

Dossier: tags, the mastering passport (quality.py), and what Discogs knows
about the album's editions (labels, mastering credits, notes, every version
under the master).  The listener's taste comes from an optional profile file,
music/.album_processor/listener.md, so the verdict is about *this* listener.
"""

import json
import logging
import math
from pathlib import Path
from typing import Optional

import requests

from . import config, discogs, quality
from .files import album_audio_files, library_album_dirs
from .tags import read_tags

log = logging.getLogger(__name__)

_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_TIMEOUT = 60
_OVERLOADED = {500, 502, 503, 504}

INSTRUCTIONS = """\
Ты — Антон, музыкальный бот-библиотекарь с отличным слухом и знанием мастеринга.
Тебе дают досье альбома из медиатеки слушателя. Напиши по-русски короткое мнение,
4–7 предложений, обычным текстом без Markdown:

- каков мастер: что значат цифры паспорта на слух (DR, плоские вершины = жёсткий
  клиппинг, true peak выше 0 dB), живой он, плотный или пережатый, жанровая ли это норма;
- стоит ли искать другое издание и какое именно — только из списка изданий в досье
  (год, формат, лейбл), с объяснением; если лучше оставить как есть — так и скажи прямо;
- если уместно, какие треки послушать внимательнее.

Как читать паспорт (мерило — эти шкалы, а не общие слова):
- DR альбома: 12+ открытый, 9–11 нормальный, 7–8 плотный, 5–6 сжатый, ≤4 пережатый.
  Для электроники, хип-хопа, рока 6–8 — обычная норма жанра, не повод для тревоги.
- Плоские вершины (flat_tops) — следы жёсткого клиппинга; смотри на частоту в минуту:
  меньше 5/мин — ничтожно и не слышно; 5–100/мин — срезанные удары, слышно на внимательном
  прослушивании; 100–1000/мин — заметный клиппинг; тысячи — грубый, слышен всегда.
  Десятки полок на весь альбом — это ничего.
- True peak до +1 dB — обычное дело и безвредно, если у слушателя лимитер или ReplayGain;
  +3 dB и выше — признак перегруженного мастера.

Как выбирать издание:
- Другой каталожный номер, страна или формат того же релиза — это обычно тот же мастер
  и тот же звук; это не повод для замены.
- Просмотри все editions целиком, до последнего года — переиздания обычно в конце списка.
- Советуй издание, только если в досье есть признак другого мастеринга: пометка
  Remastered или Remaster, заметно более поздний год переиздания, другой инженер
  мастеринга в credits, указание в notes. Назови этот признак.
- Если такого признака нет — прямо скажи, что по досье лучшего издания не видно,
  и посоветуй оставить текущее.

Правила:
- Факты — только из досье. Не придумывай ремастеры, инженеров, даты и издания,
  которых там нет. Если данных не хватает для вывода — скажи об этом.
- Учитывай профиль слушателя, если он есть.
- Говори прямо и по делу, с вердиктом в конце, без приветствий, вступлений и пересказа всех цифр.
"""

DEFAULT_LISTENER = "Слушатель предпочитает оригинальные мастеры без громкостной войны и одно издание альбома в медиатеке."


def listener_path() -> Path:
    return config.state_dir() / "listener.md"


def find_album(query: str) -> list[Path]:
    """Library albums whose "Artist/YYYY - Album" contains *query* (case aside), or the folder itself."""
    path = Path(query)
    if path.is_dir():
        return [path]
    needle = query.casefold()
    return [d for d in library_album_dirs()
            if needle in str(d.relative_to(config.MUSIC_PATH)).casefold()]


def dossier(album_dir: Path) -> dict:
    """Everything Anton may say anything about."""
    files = album_audio_files(album_dir)
    tags = read_tags(files[0]) if files else {}
    passport = quality.passport(album_dir)
    facts: dict = {
        "album": {
            "artist": tags.get("album_artist") or tags.get("artist"),
            "title": tags.get("album"),
            "year": tags.get("date"),
            "genre": tags.get("genre"),
            "tracks": len(files),
        },
        "passport": {
            "summary": quality.summary(passport),
            "grade_scale": "🟢 открытый мастер, 🟡 плотный или следы клиппинга, 🔴 пережатый",
            "tracks": [
                {"track": Path(t["file"]).stem, "dr": t["dr"], "flat_tops": t["clips"],
                 "flat_tops_per_minute": round(t["clips"] / max(t["seconds"] / 60, 1 / 60), 1),
                 "minutes": round(t["seconds"] / 60, 1),
                 "true_peak_db": round(20 * math.log10(t["true_peak"]), 1) if t["true_peak"] else None}
                for t in passport["tracks"]
            ],
        },
    }
    found = discogs.fetch_album(facts["album"]["artist"], facts["album"]["title"] or album_dir.name)
    if found and found.get("release_id"):
        history = discogs.edition_history(found["release_id"], found.get("master_id"))
        history["note"] = ("release — издание, найденное поиском Discogs: не обязательно то, "
                           "что лежит в медиатеке; editions — сводка всех изданий этого альбома "
                           "(год · носитель · лейбл · пометки · ×число тиражей)")
        facts["discogs"] = history
    else:
        facts["discogs"] = "альбом не найден в Discogs"
    try:
        facts["listener"] = listener_path().read_text(encoding="utf-8").strip()
    except OSError:
        facts["listener"] = DEFAULT_LISTENER
    return facts


class OpinionError(RuntimeError):
    """A problem worth telling the user about in plain words."""


class _Overloaded(OpinionError):
    """The model is busy or silent: worth asking another one."""


def ask_gemini(facts: dict) -> str:
    """The opinion from GEMINI_MODEL, or from GEMINI_FALLBACK_MODEL when the first is overloaded."""
    if not config.GEMINI_API_KEY:
        raise OpinionError("не задан GEMINI_API_KEY")
    models = list(dict.fromkeys(m for m in (config.GEMINI_MODEL, config.GEMINI_FALLBACK_MODEL) if m))
    for model in models:
        try:
            return _generate(model, facts)
        except _Overloaded as exc:
            log.warning("%s: %s", model, exc)
            if model == models[-1]:
                raise OpinionError(f"модели Gemini перегружены — попробуйте позже ({exc})") from exc
    raise AssertionError("unreachable")


def _generate(model: str, facts: dict) -> str:
    body = {
        "system_instruction": {"parts": [{"text": INSTRUCTIONS}]},
        "contents": [{"role": "user", "parts": [{"text": json.dumps(facts, ensure_ascii=False, indent=1)}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }
    proxies = {"https": config.GEMINI_PROXY} if config.GEMINI_PROXY else None
    try:
        resp = requests.post(_API_URL.format(model=model), json=body, timeout=_TIMEOUT,
                             headers={"x-goog-api-key": config.GEMINI_API_KEY}, proxies=proxies)
    except (requests.Timeout, requests.ConnectionError) as exc:
        # Silence or a dropped connection: an overloaded model as often as a network problem
        raise _Overloaded(f"нет ответа ({type(exc).__name__})") from exc
    except requests.RequestException as exc:
        raise OpinionError(f"Gemini не отвечает: {exc}") from exc
    if resp.status_code != 200:
        message = ""
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            pass
        if "location" in message.lower():
            raise OpinionError("Gemini API недоступен из страны сервера — задайте GEMINI_PROXY")
        if resp.status_code == 429:
            raise OpinionError("исчерпан лимит бесплатного тарифа Gemini — попробуйте позже")
        if resp.status_code in _OVERLOADED:
            raise _Overloaded(f"HTTP {resp.status_code}")
        raise OpinionError(f"Gemini: HTTP {resp.status_code} {message}".strip())
    parts = ((resp.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise OpinionError("Gemini вернул пустой ответ")
    return text


def opinion(album_dir: Path) -> str:
    facts = dossier(album_dir)
    title = f"{facts['album']['artist']} — {facts['album']['title']}"
    return f"💬 Антон о «{title}»\n{quality.summary(quality.passport(album_dir))}\n\n{ask_gemini(facts)}"


def run(query: str, show_dossier: bool = False) -> tuple[int, str]:
    """(exit code, text) for the command line and the bot."""
    albums = find_album(query)
    if not albums:
        return 1, f"Не нашёл альбома по запросу «{query}»."
    if len(albums) > 1:
        names = "\n".join(f"• {d.relative_to(config.MUSIC_PATH)}" for d in albums[:15])
        return 1, f"Подходит несколько альбомов — уточните:\n{names}"
    if show_dossier:
        return 0, json.dumps(dossier(albums[0]), ensure_ascii=False, indent=1)
    try:
        return 0, opinion(albums[0])
    except OpinionError as exc:
        return 1, f"🤷 Антон не смог высказаться: {exc}"
