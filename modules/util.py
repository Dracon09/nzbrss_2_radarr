import re
import time
import logging
import signal
import sys
import datetime
import threading
from typing import Optional
from typing import List

import keyboard
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

def setup_signals():
    stop_event = threading.Event()
    manual_event = threading.Event()

    def handle_stop(_sig, _frame):
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)
    return stop_event, manual_event

def listen_for_manual_run(manual_event, stop_event):
    while not stop_event.is_set():
        keyboard.wait("ctrl+r")
        manual_event.set()


def countdown(seconds: int, manual_event, stop_event) -> bool:
    """
    Display a live countdown in the console, returning True if manual_event is set,
    or False once the timer expires. stop_event will abort early.
    """
    # Log next scheduled time
    next_time = datetime.datetime.now() + datetime.timedelta(seconds=seconds)
    logging.info(f"⏳ Next run at {next_time.strftime('%Y-%m-%d %H:%M:%S')}")

    start = time.time()
    while not stop_event.is_set():
        # manual trigger?
        if manual_event.is_set():
            return True

        remaining = seconds - int(time.time() - start)
        if remaining <= 0:
            break

        mins, secs = divmod(remaining, 60)
        # overwrite the same line with carriage return
        sys.stdout.write(f"\r⏳ {mins}m{secs}s until next run (Ctrl+R) ")
        sys.stdout.flush()
        time.sleep(1)

    # clear the line before exit
    sys.stdout.write("\r" + " " * 60 + "\r")
    sys.stdout.flush()
    return False

def filter_title(title: str, inc: Optional[re.Pattern], exc: Optional[re.Pattern]) -> bool:
    if not title: return False
    if inc and not inc.search(title): return False
    if exc and exc.search(title): return False
    return True

def add_movie(imdb_id: str, title: str, config, radarr):
    for i in range(3):
        try:
            added, exists, invalid, excluded = radarr.add_multiple_movies(
                [imdb_id], config.movie_folder, config.quality_profile)
            logging.info(f"✅ {imdb_id} → Added:{added} Exists:{exists} Invalid:{invalid} Excl:{excluded}")
            if invalid:
                with open("config/invalid_movie.log", "a", encoding="utf-8") as f:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    f.write(f"{ts} - {title} ({imdb_id})\n")
            return
        except Exception as e:
            logging.error(f"Error adding {imdb_id} (try {i+1}): {e}")
            time.sleep(5 * (2 ** i))


def redact_url_query(url: str, sensitive_keys: List[str] = ["apikey", "api_token", "r"]) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(k, "****") if k.lower() in sensitive_keys else (k, v) for k, v in query]
    redacted_query = urlencode(redacted)
    return urlunparse(parsed._replace(query=redacted_query))
