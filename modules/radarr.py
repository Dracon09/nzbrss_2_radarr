# modules/radarr.py

import logging
import time
import datetime
from typing import List, Tuple, cast
from arrapi import RadarrAPI
from modules.config import ConfigModel  # ✅ Explicit import


def setup_radarr(url: str, api_key: str) -> RadarrAPI:
    """
    Connects to Radarr using the provided URL and API key.
    """
    try:
        radarr = RadarrAPI(url, api_key)
        logging.info("✅ Connected to Radarr successfully!")
        return radarr
    except Exception as e:
        logging.error(f"❌ Radarr connection failed: {e}")
        raise


def add_movie(
        imdb_id: str,
        title: str,
        config: ConfigModel,
        radarr: RadarrAPI,
        max_attempts: int = 3,
        initial_delay: int = 5
) -> None:
    """
    Adds a movie to Radarr using the given IMDb ID and logs the outcome.
    Retries on failure and logs invalid movies to a file.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            added, exists, invalid, excluded = cast(
                Tuple[List, List, List, List],
                radarr.add_multiple_movies(
                    [imdb_id],
                    config.movie_folder,
                    config.quality_profile
                )
            )
            logging.info(f"✅ {imdb_id} → Added:{added} Exists:{exists} Invalid:{invalid} Excl:{excluded}")

            if invalid:
                timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(config.invalid_movie_log_file, "a", encoding="utf-8") as f:
                    f.write(f"{timestamp} - {title} ({imdb_id})\n")

            return  # exit after success
        except Exception as e:
            logging.error(f"Error adding {imdb_id} (try {attempt}): {e}")
            if attempt < max_attempts:
                time.sleep(initial_delay * (2 ** (attempt - 1)))
            else:
                logging.error(f"❌ Giving up on {imdb_id}")
