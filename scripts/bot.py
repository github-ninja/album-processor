#!/usr/bin/env python3
"""
Telegram bot — front-end for album_processor.py.

Environment variables (required):
  BOT_TOKEN   — Telegram bot token from @BotFather
  ALLOWED_ID  — Telegram user ID allowed to send commands

Environment variables (optional):
  UPCONVERT_ACTION — passed to the processor; defaults to "ask", which makes
                     the bot ask what to do with every suspected upconvert

Commands:
  /newalbum   — scan downloads and process all new albums
  /fixlibrary — check the library (names, tags, covers, format, ReplayGain) and fix deviations
                ("/fixlibrary all" also checks every album title against Discogs)
  /help       — show available commands
"""

import asyncio
import html
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
except ImportError:
    sys.exit("Error: python-telegram-bot is not installed.")

try:
    from spectrogram import render_spectrogram
    _SPECTROGRAM_AVAILABLE = True
except ImportError:
    _SPECTROGRAM_AVAILABLE = False

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BOT_TOKEN  = os.environ.get("BOT_TOKEN", "")
ALLOWED_ID = int(os.environ.get("ALLOWED_ID", "0"))

PROCESSOR_SCRIPT = str(Path(__file__).with_name("album_processor.py"))
DOWNLOADS_PATH = Path(os.environ.get("DOWNLOADS_PATH", "/mnt/data/downloads"))
UPCONVERT_ACTION = os.environ.get("UPCONVERT_ACTION", "ask")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# Only one processor run at a time — runs move files around in downloads
_processor_lock = asyncio.Lock()

# Suspected upconverts awaiting a decision: short id → processor report entry.
# Kept in memory only; after a restart /newalbum simply asks again.
_pending: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def _is_allowed(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ALLOWED_ID


# ---------------------------------------------------------------------------
# Processor helpers
# ---------------------------------------------------------------------------

async def _run_processor(args: list[str], env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_overrides}
    async with _processor_lock:
        return await asyncio.to_thread(
            lambda: subprocess.run(
                [sys.executable, PROCESSOR_SCRIPT, *args],
                capture_output=True,
                text=True,
                env=env,
            )
        )


async def _reply_with_output(message: Message, result: subprocess.CompletedProcess) -> None:
    output = (result.stdout + result.stderr).strip()

    # Telegram message limit is 4096 chars; truncate if needed
    if len(output) > 4000:
        output = "…\n" + output[-4000:]
    output = html.escape(output)

    if result.returncode == 0:
        await message.reply_text(f"✅ Готово!\n\n<pre>{output}</pre>", parse_mode="HTML")
    else:
        await message.reply_text(f"❌ Скрипт завершился с ошибкой:\n\n<pre>{output}</pre>", parse_mode="HTML")


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(DOWNLOADS_PATH))
    except ValueError:
        return str(path)


def _decision_keyboard(key: str) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton("✅ Обработать", callback_data=f"up:proc:{key}"),
        InlineKeyboardButton("⏭ Оставить", callback_data=f"up:keep:{key}"),
        InlineKeyboardButton("🗑 Удалить", callback_data=f"up:del:{key}"),
    ]]
    if _SPECTROGRAM_AVAILABLE and len(_pending[key].get("tracks", [])) > 1:
        rows.append([InlineKeyboardButton("📊 Другой трек", callback_data=f"up:tracks:{key}")])
    return InlineKeyboardMarkup(rows)


def _confirm_delete_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🗑 Да, удалить", callback_data=f"up:delyes:{key}"),
        InlineKeyboardButton("↩️ Отмена", callback_data=f"up:back:{key}"),
    ]])


def _tracks_keyboard(key: str) -> InlineKeyboardMarkup:
    rows = []
    for i, track in enumerate(_pending[key]["tracks"]):
        label = Path(track["path"]).stem[:40]
        if track.get("cutoff_hz"):
            label += f" · {track['cutoff_hz'] / 1000:.1f} kHz"
        rows.append([InlineKeyboardButton(label, callback_data=f"up:spec:{key}:{i}")])
    rows.append([InlineKeyboardButton("↩️ Назад", callback_data=f"up:back:{key}")])
    return InlineKeyboardMarkup(rows)


def _representative_track(tracks: list[dict]) -> Optional[dict]:
    """The flagged track whose cutoff is closest to the album median."""
    if not tracks:
        return None
    cutoffs = sorted(t["cutoff_hz"] for t in tracks if t.get("cutoff_hz"))
    if not cutoffs:
        return tracks[0]
    median = cutoffs[len(cutoffs) // 2]
    return min(tracks, key=lambda t: abs((t.get("cutoff_hz") or 0) - median))


async def _render(track: dict) -> Optional[bytes]:
    if not _SPECTROGRAM_AVAILABLE:
        return None
    try:
        return await asyncio.to_thread(render_spectrogram, Path(track["path"]), track.get("cutoff_hz"))
    except Exception:
        log.exception("Failed to render spectrogram for %s", track["path"])
        return None


async def _edit_text(query, text: str) -> None:
    """Replace the text of a question message (photo caption or plain text)."""
    if query.message.photo:
        await query.edit_message_caption(caption=text, parse_mode="HTML", reply_markup=None)
    else:
        await query.edit_message_text(text, parse_mode="HTML")


async def _ask_about_upconverts(message: Message, entries: list[dict]) -> None:
    for entry in entries:
        key = uuid.uuid4().hex[:12]
        _pending[key] = entry
        title = "Вероятный апконверт" if entry["confident"] else "Возможный апконверт"
        text = (
            f"🚩 <b>{title}</b>\n"
            f"📁 {html.escape(_display_path(Path(entry['path'])))}\n"
            f"{html.escape(entry['summary'])} · {entry['flagged']}/{entry['analysed']} трек(ов)\n\n"
            "Что делать с альбомом?"
        )
        track = _representative_track(entry.get("tracks", []))
        png = await _render(track) if track else None
        if png:
            await message.reply_photo(png, caption=text, parse_mode="HTML",
                                      reply_markup=_decision_keyboard(key))
        else:
            await message.reply_text(text, parse_mode="HTML", reply_markup=_decision_keyboard(key))


def _delete_album(path: Path) -> None:
    """Delete an album folder from downloads, plus its artist folder if left empty."""
    downloads = DOWNLOADS_PATH.resolve()
    target = path.resolve()
    if downloads not in target.parents:
        raise ValueError(f"путь вне папки загрузок: {path}")
    shutil.rmtree(target)
    parent = target.parent
    if parent != downloads and not any(parent.iterdir()):
        parent.rmdir()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "Доступные команды:\n"
        "/newalbum — обработать новые альбомы из папки загрузок\n"
        "/fixlibrary — проверить библиотеку (названия, теги, обложки, формат) и исправить\n"
        "/fixlibrary all — то же, плюс сверить с Discogs названия всех альбомов (долго)\n"
        "/help     — это сообщение\n\n"
        "Образ альбома с CUE режется на треки; хайрез, ALAC, APE и WAV приводятся к FLAC 16/44.1. "
        "Теги и обложка — по Discogs и Deezer, громкость — ReplayGain. "
        "Если альбом похож на апконверт из MP3, бот спросит, что с ним делать."
    )


async def cmd_newalbum(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    await update.message.reply_text("⏳ Запускаю обработку альбомов…")
    log.info("Processing triggered by user %d", ALLOWED_ID)

    with tempfile.TemporaryDirectory() as tmp:
        report_file = Path(tmp) / "upconverts.json"
        try:
            result = await _run_processor([], {
                "UPCONVERT_ACTION": UPCONVERT_ACTION,
                "UPCONVERT_REPORT_FILE": str(report_file),
            })
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось запустить скрипт:\n{exc}")
            log.exception("Failed to run processor")
            return

        try:
            deferred = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            deferred = []

    await _reply_with_output(update.message, result)
    await _ask_about_upconverts(update.message, deferred)

    log.info("Processor finished with return code %d", result.returncode)


async def on_upconvert_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not _is_allowed(update):
        await query.answer()
        return

    _, action, key, *rest = query.data.split(":")
    entry = _pending.get(key)
    if entry is None:
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⌛ Запрос устарел — запустите /newalbum ещё раз.")
        return

    path = Path(entry["path"])
    name = html.escape(_display_path(path))

    if action == "del":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_confirm_delete_keyboard(key))
        return
    if action == "back":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_decision_keyboard(key))
        return
    if action == "tracks":
        await query.answer()
        await query.edit_message_reply_markup(reply_markup=_tracks_keyboard(key))
        return
    if action == "spec":
        tracks = entry.get("tracks", [])
        idx = int(rest[0]) if rest and rest[0].isdigit() else -1
        if not 0 <= idx < len(tracks):
            await query.answer()
            return
        await query.answer("Строю спектрограмму…")
        await query.edit_message_reply_markup(reply_markup=_decision_keyboard(key))
        track = tracks[idx]
        png = await _render(track)
        caption = html.escape(Path(track["path"]).name)
        if track.get("cutoff_hz"):
            caption += f"\nсрез {track['cutoff_hz'] / 1000:.1f} kHz"
        if png:
            await query.message.reply_photo(png, caption=caption, parse_mode="HTML")
        else:
            await query.message.reply_text(f"⚠️ Не удалось построить спектрограмму: {caption}",
                                           parse_mode="HTML")
        return

    _pending.pop(key, None)
    await query.answer()

    if not path.is_dir():
        await _edit_text(query, f"⚠️ Папка уже не существует: {name}")
        return

    if action == "keep":
        await _edit_text(query, f"⏭ Оставлен в загрузках: {name}")

    elif action == "delyes":
        try:
            await asyncio.to_thread(_delete_album, path)
        except Exception as exc:
            log.exception("Failed to delete %s", path)
            await _edit_text(query, f"❌ Не удалось удалить {name}:\n{html.escape(str(exc))}")
            return
        log.info("Deleted suspected upconvert: %s", path)
        await _edit_text(query, f"🗑 Удалён: {name}")

    elif action == "proc":
        await _edit_text(query, f"⏳ Обрабатываю: {name}")
        log.info("Processing suspected upconvert on user request: %s", path)
        try:
            result = await _run_processor([str(path)], {"UPCONVERT_ACTION": "warn"})
        except Exception as exc:
            await query.message.reply_text(f"❌ Не удалось запустить скрипт:\n{exc}")
            log.exception("Failed to run processor")
            return
        await _reply_with_output(query.message, result)


# ---------------------------------------------------------------------------
# Library fixes
# ---------------------------------------------------------------------------

# Pending /fixlibrary plans: short id → {"plan": [...], "idx": int, "accepted": [...]}
_fix_sessions: dict[str, dict] = {}


def _fix_line(i: int, entry: dict) -> str:
    old_artist = entry.get("old_artist_folder", entry["artist"])
    new_artist = entry.get("new_artist_folder", entry["artist"])
    old = f"{old_artist}/{entry['old_folder']}"
    new = f"{new_artist}/{entry['new_folder']}"
    line = f"{i}. {old}" + (f" → {new}" if new != old else "")
    extras = []
    if (entry["old_album"], entry["old_year"]) != (entry["new_album"], entry["new_year"]):
        extras.append("теги")
    if entry.get("file_renames"):
        extras.append(f"файлов: {len(entry['file_renames'])}")
    if entry.get("tag_fixes"):
        extras.append(f"полей тегов: {len(entry['tag_fixes'])}")
    if entry.get("cover"):
        extras.append("обложка")
    if entry.get("convert"):
        extras.append(f"→ 16/44.1: {len(entry['convert'])}")
    if entry.get("replaygain"):
        extras.append("ReplayGain")
    if entry.get("duplicate_of"):
        extras.append("дубль — убрать")
    if extras:
        line += f" ({', '.join(extras)})"
    if entry["source"] == "local" and entry["note"]:
        line += " 🔸"
    return html.escape(line)


def _fix_card(key: str) -> str:
    session = _fix_sessions[key]
    entry = session["plan"][session["idx"]]
    old_artist = entry.get("old_artist_folder", entry["artist"])
    new_artist = entry.get("new_artist_folder", entry["artist"])
    lines = [f"<b>{session['idx'] + 1}/{len(session['plan'])}</b> · {html.escape(old_artist)}"]
    if new_artist != old_artist:
        lines.append(f"📂 артист: {html.escape(old_artist)} → {html.escape(new_artist)}")
    if entry["old_folder"] != entry["new_folder"]:
        lines.append(f"📁 {html.escape(entry['old_folder'])}\n→ {html.escape(entry['new_folder'])}")
    if entry["old_album"] != entry["new_album"]:
        lines.append(f"🏷 альбом: {html.escape(repr(entry['old_album']))} → {html.escape(repr(entry['new_album']))}")
    if entry["old_year"] != entry["new_year"]:
        lines.append(f"📅 год: {entry['old_year'] or '—'} → {entry['new_year'] or '—'}")
    renames = entry.get("file_renames", [])
    if renames:
        lines.append(f"📝 файлов к переименованию: {len(renames)}, например:")
        for old, new in renames[:3]:
            lines.append(f"  {html.escape(old)}\n  → {html.escape(new)}")
    tag_fixes = entry.get("tag_fixes", [])
    if tag_fixes:
        fields = sorted({field for _, field, _, _ in tag_fixes})
        lines.append(f"🏷 полей тегов к исправлению: {len(tag_fixes)} ({html.escape(', '.join(fields))})")
    cover = entry.get("cover")
    if cover:
        sizes = "×".join(map(str, cover["from_size"] or [])) or "нет"
        to = "×".join(map(str, cover["to_size"] or []))
        source = "из Deezer" if cover["origin"] == "deezer" else "своя"
        lines.append(f"🖼 обложка ({source}): {sizes} → {to}, встроить в {cover['embed']} файл(ов)")
    convert = entry.get("convert", [])
    if convert:
        formats = ", ".join(sorted({fmt for _, fmt in convert}))
        lines.append(f"🎚 конвертация {formats} → 16/44.1: {len(convert)} файл(ов)")
    if entry.get("replaygain"):
        lines.append("🔊 пересчитать ReplayGain")
    if entry["note"]:
        lines.append(f"ℹ️ {html.escape(entry['note'])}")
    return "\n".join(lines)


def _fix_card_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Да", callback_data=f"fx:yes:{key}"),
        InlineKeyboardButton("⏭ Нет", callback_data=f"fx:no:{key}"),
        InlineKeyboardButton("⏹ Хватит", callback_data=f"fx:stop:{key}"),
    ]])


async def _apply_fixes(message: Message, plan: list[dict]) -> None:
    if not plan:
        await message.reply_text("Ничего не применено.")
        return
    await message.reply_text(f"⏳ Применяю исправления: {len(plan)}…")
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / "plan.json"
        plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        try:
            result = await _run_processor(["--fix-library", "--apply", str(plan_file)], {})
        except Exception as exc:
            await message.reply_text(f"❌ Не удалось запустить скрипт:\n{exc}")
            log.exception("Failed to run processor")
            return
    await _reply_with_output(message, result)


async def cmd_fixlibrary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        return

    check_all = bool(context.args) and context.args[0].lower() == "all"
    await update.message.reply_text(
        "⏳ Проверяю библиотеку" + (" и сверяю все альбомы с Discogs" if check_all else "")
        + "… Это может занять несколько минут."
    )
    with tempfile.TemporaryDirectory() as tmp:
        plan_file = Path(tmp) / "plan.json"
        args = ["--fix-library", "--plan-out", str(plan_file)] + (["--all"] if check_all else [])
        try:
            result = await _run_processor(args, {})
            plan = json.loads(plan_file.read_text(encoding="utf-8"))
        except Exception as exc:
            await update.message.reply_text(f"❌ Не удалось составить план:\n{exc}")
            log.exception("Failed to build library fix plan")
            return

    if result.returncode != 0:
        await _reply_with_output(update.message, result)
        return
    if not plan:
        await update.message.reply_text("✅ Библиотека в порядке — исправлять нечего.")
        return

    key = uuid.uuid4().hex[:12]
    _fix_sessions[key] = {"plan": plan, "idx": 0, "accepted": []}

    # Split the list into messages under Telegram's 4096-char limit
    chunks, current = [], f"<b>Предлагаемые исправления ({len(plan)}):</b>"
    for i, entry in enumerate(plan, start=1):
        line = _fix_line(i, entry)
        if len(current) + len(line) > 3800:
            chunks.append(current)
            current = ""
        current += "\n" + line
    if any(e["source"] == "local" and e["note"] for e in plan):
        current += "\n\n🔸 — нет в Discogs, только локальная чистка"
    chunks.append(current)
    for chunk in chunks[:-1]:
        await update.message.reply_text(chunk, parse_mode="HTML")
    await update.message.reply_text(chunks[-1], parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Применить всё ({len(plan)})", callback_data=f"fx:all:{key}")],
        [InlineKeyboardButton("👆 По одному", callback_data=f"fx:one:{key}"),
         InlineKeyboardButton("❌ Отмена", callback_data=f"fx:cancel:{key}")],
    ]))


async def on_fix_decision(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_allowed(update):
        return

    _, action, key = query.data.split(":", 2)
    session = _fix_sessions.get(key)
    if session is None:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⌛ План устарел — запустите /fixlibrary ещё раз.")
        return

    if action == "cancel":
        _fix_sessions.pop(key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("❌ Отменено, ничего не изменено.")
    elif action == "all":
        _fix_sessions.pop(key, None)
        await query.edit_message_reply_markup(reply_markup=None)
        await _apply_fixes(query.message, session["plan"])
    elif action == "one":
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(_fix_card(key), parse_mode="HTML",
                                       reply_markup=_fix_card_keyboard(key))
    elif action in ("yes", "no", "stop"):
        number = session["idx"] + 1
        entry = session["plan"][session["idx"]]
        if action == "yes":
            session["accepted"].append(entry)
        if action != "stop":
            session["idx"] += 1
        mark = {"yes": "✅", "no": "⏭", "stop": "⏹"}[action]
        await query.edit_message_text(f"{mark} {_fix_line(number, entry)}", parse_mode="HTML")
        if action != "stop" and session["idx"] < len(session["plan"]):
            await query.message.reply_text(_fix_card(key), parse_mode="HTML",
                                           reply_markup=_fix_card_keyboard(key))
            return
        _fix_sessions.pop(key, None)
        await _apply_fixes(query.message, session["accepted"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    if not BOT_TOKEN:
        sys.exit("Error: BOT_TOKEN environment variable is not set.")
    if not ALLOWED_ID:
        sys.exit("Error: ALLOWED_ID environment variable is not set.")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("newalbum",  cmd_newalbum))
    app.add_handler(CommandHandler("fixlibrary", cmd_fixlibrary))
    app.add_handler(CallbackQueryHandler(on_fix_decision, pattern=r"^fx:"))
    app.add_handler(CallbackQueryHandler(on_upconvert_decision, pattern=r"^up:"))

    log.info("Bot started. Waiting for commands from user ID %d…", ALLOWED_ID)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
