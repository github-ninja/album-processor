"""
Command line:

  album_processor.py                         process all new albums in downloads
  album_processor.py <album dir>…            process the given album folders
  album_processor.py --fix-library [--all] [--plan-out FILE]
                                             check the library, print/save a plan
  album_processor.py --fix-library --apply FILE
                                             apply a saved plan
  album_processor.py --quality [QUERY]       mastering passports: the most crushed
                                             albums, or the albums matching QUERY
  album_processor.py --releases [check|due|status|daily|weekly|monthly|stop]
                                             new albums of the library's artists (Deezer):
                                             check now, run a scheduled check if due,
                                             show or set the schedule
  album_processor.py --deezer search QUERY   albums on Deezer, as JSON (via deezer-downloader)
  album_processor.py --deezer download ID    download a Deezer album, print its folder
  album_processor.py --opinion QUERY [--dossier]
                                             Anton's opinion on an album (Gemini), or
                                             the dossier of facts it is written from
"""

import datetime
import json
import logging
import sys
from pathlib import Path

from . import config, deezer_dl, library, opinion, pipeline, quality, releases

log = logging.getLogger("albumproc")


def _process(paths: list[str]) -> int:
    if paths:
        candidates = [pipeline.candidate_from_path(Path(p)) for p in paths]
    else:
        log.info("🔍 Scanning for new albums in: %s", config.DOWNLOADS_PATH)
        candidates = pipeline.find_new_albums()
    if not candidates:
        log.info("ℹ️  No new albums found.")
        return 0

    log.info("📋 Found %d album(s) to process.\n", len(candidates))
    state = pipeline.RunState()
    results = [pipeline.process_album(candidate, state) for candidate in candidates]
    failed, deferred = results.count(False), results.count(None)

    if config.DEFERRED_REPORT_FILE:
        Path(config.DEFERRED_REPORT_FILE).write_text(json.dumps(state.deferred + state.processed, ensure_ascii=False),
                                                     encoding="utf-8")
    if state.not_found:
        # At the end too: a misspelt artist is easy to miss in the middle of the log
        log.warning("\n⚠️ Не найдены в Discogs — год и жанр не проставлены (%d):", len(state.not_found))
        for line in state.not_found:
            log.warning("  • %s", line)
        log.warning("Проверь написание исполнителя (например, кириллица вместо латиницы) и запусти заново.")
    if state.flagged:
        # Repeated at the end: the Telegram bot only shows the tail of long logs
        log.warning("\n🚩 Подозрение на апконверт (%d):", len(state.flagged))
        for line in state.flagged:
            log.warning("  • %s", line)

    if failed:
        log.error("\n❌ %d album(s) failed to process.", failed)
        return 1
    if deferred:
        log.info("\n🎉 %d album(s) processed, %d awaiting decision.", len(results) - deferred, deferred)
    else:
        log.info("\n🎉 All %d album(s) processed successfully!", len(results))
    return 0


def _fix_library(args: list[str]) -> int:
    if "--apply" in args:
        plan = json.loads(Path(args[args.index("--apply") + 1]).read_text(encoding="utf-8"))
        failed = library.apply_library_fixes(plan)
        log.info("\n✅ Исправлено: %d, ошибок: %d", len(plan) - failed, failed)
        return 1 if failed else 0

    plan = library.plan_library_fixes(check_all="--all" in args)
    if "--plan-out" in args:
        Path(args[args.index("--plan-out") + 1]).write_text(
            json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    log.info("\n📋 Предложено исправлений: %d", len(plan))
    return 0


def _report_releases(news: list[dict]) -> None:
    """The found albums for the bot's download buttons."""
    if config.RELEASES_REPORT_FILE:
        Path(config.RELEASES_REPORT_FILE).write_text(json.dumps(
            [{"id": a["id"], "artist": a["artist"], "title": a["title"], "doubt": a["doubt"]} for a in news],
            ensure_ascii=False), encoding="utf-8")


def _deezer(args: list[str]) -> int:
    action, rest = (args[0], " ".join(args[1:])) if args else ("", "")
    try:
        if action == "search" and rest:
            print(json.dumps(deezer_dl.search(rest), ensure_ascii=False))
        elif action == "download" and rest.isdigit():
            print(deezer_dl.download(int(rest)))
        else:
            print("Использование: --deezer search ЗАПРОС | --deezer download ID")
            return 1
    except deezer_dl.DeezerError as exc:
        print(f"❌ {exc}")
        return 1
    return 0


def _releases(action: str) -> int:
    now = datetime.datetime.now()
    if action in releases.SCHEDULES or action == "stop":
        state = releases.set_schedule(None if action == "stop" else action)
        print(releases.status(state, now))
    elif action == "status":
        print(releases.status(releases.load_state(), now))
    elif action == "due":
        # Scheduled: silent unless a check is due and finds something
        if releases.is_due(releases.load_state(), now):
            news, state = releases.check(now.date())
            _report_releases(news)
            text = releases.report(news, state, quiet=True)
            if text:
                print(text)
    elif action == "check":
        news, state = releases.check(now.date())
        _report_releases(news)
        print(releases.report(news, state))
    else:
        print(f"Неизвестное действие: {action}. Есть: check, status, daily, weekly, monthly, stop.")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # These print a message for the bot on stdout: their log goes to stderr
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stderr if argv[:1] in (["--releases"], ["--opinion"], ["--deezer"]) else sys.stdout)
    if argv and argv[0] == "--fix-library":
        return _fix_library(argv[1:])
    if argv and argv[0] == "--releases":
        return _releases(argv[1] if len(argv) > 1 else "check")
    if argv and argv[0] == "--deezer":
        return _deezer(argv[1:])
    if argv and argv[0] == "--opinion":
        args = [a for a in argv[1:] if a != "--dossier"]
        code, text = opinion.run(" ".join(args), show_dossier="--dossier" in argv)
        print(text)
        return code
    if argv and argv[0] == "--quality":
        print("\n".join(quality.library_report(" ".join(argv[1:]))))
        return 0
    return _process(argv)
