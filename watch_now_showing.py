"""Poll carnivalcinemas.sg and Telegram-alert as a target movie's booking window opens up.

The site (an AngularJS SPA) fetches its "Now Showing" grid and per-movie
showtimes from plain JSON endpoints at service.carnivalcinemas.sg. Those
endpoints are guarded by a `Token` header computed client-side from a secret
string embedded in the site's public JS bundle (AllJavaScripts,
`cinemaManager.generate`). This script reproduces that HMAC-SHA256 scheme in
pure stdlib Python so it can call the API directly, with no browser/scraping
involved.

Progress is persisted in a state file so restarts (a fresh GitHub Actions run
every few hours) don't lose track or re-alert:

1. "movie"    — waiting for the target movie to appear in Now Showing.
2. "watching" — movie is showing; on every poll, alerts once for each new
                showtime that appears inside the target time window on the
                target date (whatever its bookable status), and again for
                any of those that later flip from listed to bookable.

Every matching showtime gets its own round of alerts — the site sometimes
releases showtimes for a date in batches, so an earlier slot inside the
window shouldn't stop the watch from also catching a later one. The watch
only stops once the target date itself has fully passed (with a safety
buffer), not once a single match is found.

No external dependencies — stdlib only (hmac, hashlib, base64, urllib, json).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ── Carnival Cinemas API auth ────────────────────────────────────────────────

_MOVIECODE = "rz8LuOtFBXphj9WQfvFh"
_TOKEN_URL = "https://carnivalcinemas.sg/#/"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_API_BASE = "https://service.carnivalcinemas.sg/api/QuickSearch"


def _hmac_sha256_b64(*, message: str, key: str) -> str:
    digest = hmac.new(key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _dotnet_ticks_now() -> int:
    unix_ms = int(time.time() * 1000)
    return unix_ms * 10_000 + 621_355_968_000_000_000


def _generate_token() -> str:
    key = _hmac_sha256_b64(message=f"{_TOKEN_URL}|{_MOVIECODE}", key=_MOVIECODE)
    ticks = _dotnet_ticks_now()
    signed = _hmac_sha256_b64(message=f"{_USER_AGENT}|{ticks}", key=key)
    payload = f"{signed}|{_TOKEN_URL}|{ticks}|{_MOVIECODE}"
    return base64.b64encode(payload.encode("utf-8")).decode("utf-8")


def _api_get(endpoint: str, **params: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    request = urllib.request.Request(
        f"{_API_BASE}/{endpoint}?{query}",
        headers={"Token": _generate_token(), "User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    if body.get("responseError", {}).get("ErrorCode") != "200":
        raise ValueError(f"Carnival API returned an error response: {body}")
    return body


# ── Carnival Cinemas API client ──────────────────────────────────────────────


def fetch_now_playing(*, location: str = "Singapore") -> list[dict[str, Any]]:
    return _api_get("GetNowPlayingMovies", location=location).get("responseMovies", [])


def fetch_showtimes(
    *,
    movie_code: str,
    date_value: str,
    location: str = "Singapore",
) -> list[dict[str, Any]]:
    """Return one entry per (cinema, session): {cinema, time_label, bookable, time_obj, session_id}."""
    body = _api_get(
        "GetCinemaAndShowTimeByMovie",
        location=location,
        movieCode=movie_code,
        date=date_value,
    )
    sessions: list[dict[str, Any]] = []
    for cinema in body.get("responseCinemaWithShowTime", []):
        time_tokens = [t.strip() for t in cinema.get("showTime", "").split(",") if t.strip()]
        id_tokens = [t.strip() for t in cinema.get("longSessionID", "").split(",") if t.strip()]
        for index, token in enumerate(time_tokens):
            bookable = token[-1] == "T"
            time_label = token[:-1].strip()
            time_obj = datetime.datetime.strptime(time_label, "%I:%M %p").time()
            session_id = id_tokens[index] if index < len(id_tokens) else f"{cinema.get('cinemaName')}|{time_label}"
            sessions.append(
                {
                    "cinema": cinema.get("cinemaName"),
                    "time_label": time_label,
                    "bookable": bookable,
                    "time_obj": time_obj,
                    "session_id": session_id,
                }
            )
    return sessions


def _normalize_title(text: str) -> str:
    return "".join(text.lower().split())


def find_target_movie(
    movies: list[dict[str, Any]],
    *,
    target_name: str,
) -> dict[str, Any] | None:
    needle = _normalize_title(target_name)
    for movie in movies:
        if needle in _normalize_title(movie.get("name", "")):
            return movie
    return None


def find_sessions_in_window(
    sessions: list[dict[str, Any]],
    *,
    window_start: datetime.time,
    window_end: datetime.time,
) -> list[dict[str, Any]]:
    return [s for s in sessions if window_start <= s["time_obj"] <= window_end]


# ── Telegram alerting ────────────────────────────────────────────────────────


def send_telegram_message(text: str, *, bot_token: str, chat_id: str) -> None:
    query = urllib.parse.urlencode({"chat_id": chat_id, "text": text})
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage?{query}"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            response.read()
    except urllib.error.URLError as exc:
        logger.error("Failed to send Telegram message: %s", exc)
        raise


# ── Self-disabling the GitHub Actions schedule once fully done ──────────────


def disable_current_workflow(*, github_token: str, repo: str, workflow_file: str) -> None:
    url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/disable"
    request = urllib.request.Request(
        url,
        method="PUT",
        headers={
            "Authorization": f"Bearer {github_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "carnival-watch-script",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response.read()
        logger.info("Disabled the scheduled workflow — job done, no more polling.")
    except urllib.error.URLError as exc:
        logger.error("Failed to disable workflow (non-fatal): %s", exc)


# ── State persistence ────────────────────────────────────────────────────────

_DEFAULT_STATE: dict[str, Any] = {
    "stage": "movie",
    "movie_code": None,
    "movie_name": None,
    "alerted_session_ids": [],
    "bookable_session_ids": [],
}

_STOP_BUFFER = datetime.timedelta(days=2)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return dict(_DEFAULT_STATE)
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _format_sessions(sessions: list[dict[str, Any]]) -> str:
    return ", ".join(
        f"{s['time_label']} ({'bookable' if s['bookable'] else 'listed, not yet bookable'})"
        for s in sessions
    )


def build_booking_url(*, movie_name: str, movie_code: str) -> str:
    name_segment = urllib.parse.quote(movie_name)
    code_segment = urllib.parse.quote(movie_code)
    return f"https://carnivalcinemas.sg/#/{name_segment}/{code_segment}"


# ── Main polling loop ────────────────────────────────────────────────────────


def run_watch_loop(
    *,
    target_movie: str,
    target_date_value: str,
    window_start: datetime.time,
    window_end: datetime.time,
    location: str,
    poll_interval_seconds: int,
    max_runtime_seconds: int,
    bot_token: str,
    chat_id: str,
    github_token: str | None,
    repo: str | None,
    workflow_file: str | None,
    state_path: Path,
) -> None:
    state = load_state(state_path)
    start = time.monotonic()
    consecutive_failures = 0
    failure_alert_sent = False
    stop_after = (
        datetime.datetime.fromisoformat(target_date_value).replace(tzinfo=datetime.timezone.utc)
        + _STOP_BUFFER
    )

    while time.monotonic() - start < max_runtime_seconds:
        if datetime.datetime.now(datetime.timezone.utc) >= stop_after:
            logger.info("Target date has passed — stopping the watch for good.")
            if github_token and repo and workflow_file:
                disable_current_workflow(
                    github_token=github_token,
                    repo=repo,
                    workflow_file=workflow_file,
                )
            return

        try:
            if state["stage"] == "movie":
                movies = fetch_now_playing(location=location)
                movie = find_target_movie(movies, target_name=target_movie)
                logger.info(
                    "Checked Now Showing (%d titles) — %s: %s",
                    len(movies),
                    target_movie,
                    "found!" if movie else "not listed yet",
                )
                if movie is not None:
                    send_telegram_message(
                        f"🎬 {movie.get('name')} is now in the Now Showing list on "
                        "carnivalcinemas.sg. Watching for showtimes between "
                        f"{window_start.strftime('%I:%M %p')} and {window_end.strftime('%I:%M %p')} "
                        "on the target date now — you'll get an alert for every "
                        "matching showtime that appears, not just the first.",
                        bot_token=bot_token,
                        chat_id=chat_id,
                    )
                    state["stage"] = "watching"
                    state["movie_code"] = movie.get("code")
                    state["movie_name"] = movie.get("name")
                    save_state(state_path, state)

            elif state["stage"] == "watching":
                sessions = fetch_showtimes(
                    movie_code=state["movie_code"],
                    date_value=target_date_value,
                    location=location,
                )
                matches = find_sessions_in_window(sessions, window_start=window_start, window_end=window_end)
                logger.info(
                    "Checked showtimes for target date — %d session(s), %d in window %s-%s",
                    len(sessions),
                    len(matches),
                    window_start.strftime("%I:%M %p"),
                    window_end.strftime("%I:%M %p"),
                )

                alerted_ids = set(state.get("alerted_session_ids", []))
                bookable_ids = set(state.get("bookable_session_ids", []))
                booking_url = build_booking_url(
                    movie_name=state.get("movie_name") or state["movie_code"],
                    movie_code=state["movie_code"],
                )

                new_sessions = [m for m in matches if m["session_id"] not in alerted_ids]
                if new_sessions:
                    any_new_bookable = any(s["bookable"] for s in new_sessions)
                    send_telegram_message(
                        f"🕖 New showtime(s) between {window_start.strftime('%I:%M %p')} and "
                        f"{window_end.strftime('%I:%M %p')} on the target date: "
                        f"{_format_sessions(new_sessions)}."
                        + (f" Go book now! {booking_url}" if any_new_bookable else " Not bookable yet — still watching."),
                        bot_token=bot_token,
                        chat_id=chat_id,
                    )
                    for session in new_sessions:
                        alerted_ids.add(session["session_id"])
                        if session["bookable"]:
                            bookable_ids.add(session["session_id"])

                newly_bookable = [
                    m for m in matches if m["session_id"] in alerted_ids and m["session_id"] not in bookable_ids
                ]
                if newly_bookable:
                    send_telegram_message(
                        f"✅ Now bookable: {_format_sessions(newly_bookable)}. Go book now! {booking_url}",
                        bot_token=bot_token,
                        chat_id=chat_id,
                    )
                    for session in newly_bookable:
                        bookable_ids.add(session["session_id"])

                if new_sessions or newly_bookable:
                    state["alerted_session_ids"] = sorted(alerted_ids)
                    state["bookable_session_ids"] = sorted(bookable_ids)
                    save_state(state_path, state)

            consecutive_failures = 0

        except (urllib.error.URLError, ValueError) as exc:
            consecutive_failures += 1
            logger.warning("Poll failed (%d in a row): %s", consecutive_failures, exc)
            if consecutive_failures == 20 and not failure_alert_sent:
                send_telegram_message(
                    "Carnival watch script has failed 20 polls in a row — "
                    "it may need attention (site or auth scheme may have changed).",
                    bot_token=bot_token,
                    chat_id=chat_id,
                )
                failure_alert_sent = True

        time.sleep(poll_interval_seconds)

    logger.info("Runtime window elapsed at stage=%s; next scheduled run will resume.", state["stage"])


def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    target_movie = os.environ.get("TARGET_MOVIE", "Jananayagan")
    target_date_value = os.environ.get("TARGET_DATE_VALUE", "2026-07-23T00:00:00")
    window_start = datetime.datetime.strptime(
        os.environ.get("WINDOW_START", "18:30"), "%H:%M"
    ).time()
    window_end = datetime.datetime.strptime(
        os.environ.get("WINDOW_END", "20:00"), "%H:%M"
    ).time()
    location = os.environ.get("LOCATION", "Singapore")
    poll_interval_seconds = int(os.environ.get("POLL_INTERVAL_SECONDS", "90"))
    max_runtime_seconds = int(os.environ.get("MAX_RUNTIME_SECONDS", "17400"))
    state_path = Path(os.environ.get("STATE_PATH", "state.json"))

    run_watch_loop(
        target_movie=target_movie,
        target_date_value=target_date_value,
        window_start=window_start,
        window_end=window_end,
        location=location,
        poll_interval_seconds=poll_interval_seconds,
        max_runtime_seconds=max_runtime_seconds,
        bot_token=bot_token,
        chat_id=chat_id,
        github_token=os.environ.get("GH_TOKEN"),
        repo=os.environ.get("GITHUB_REPOSITORY"),
        workflow_file=os.environ.get("WORKFLOW_FILE"),
        state_path=state_path,
    )


if __name__ == "__main__":
    _main()
