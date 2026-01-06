# main.py

import time
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
import os
from typing import Any

from modules.config import load_config
from modules.radarr_pyarr import RadarrProcessor
from modules.rss import run_rss_sync


def _cfg_get(cfg: Any, *path, default=None):
    """
    Safe accessor for config values that works whether `cfg` is a dict or an object
    with attributes. Example: _cfg_get(config, "radarr", "url", default="...").
    """
    cur = cfg
    for p in path:
        if cur is None:
            return default
        # dict-like
        try:
            if isinstance(cur, dict) and p in cur:
                cur = cur[p]
                continue
        except Exception:
            pass
        # attribute-like
        try:
            if hasattr(cur, p):
                cur = getattr(cur, p)
                continue
        except Exception:
            pass
        # fallback: try key access if possible
        try:
            cur = cur[p]
        except Exception:
            return default
    return cur if cur is not None else default


def setup_logging(config):
    log_path = os.path.join(_cfg_get(config, "config_folder", default="config"), "script.log")

    # Remove default handlers to prevent duplication on re-run
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    base_level = logging.DEBUG if _cfg_get(config, "debug_logging", default=False) else logging.INFO
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1,
        backupCount=_cfg_get(config, "log_retention_days", default=7), encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(base_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    stream_handler.setLevel(base_level)

    logging.basicConfig(level=base_level, handlers=[file_handler, stream_handler], force=True)

    if _cfg_get(config, "debug_logging", default=False):
        # Make your modules and helpful libraries verbose
        logging.getLogger("modules").setLevel(logging.DEBUG)
        logging.getLogger("modules.rss").setLevel(logging.DEBUG)
        logging.getLogger("modules.radarr_pyarr").setLevel(logging.DEBUG)
        logging.getLogger("pyarr").setLevel(logging.DEBUG)
        logging.getLogger("requests").setLevel(logging.DEBUG)
        logging.getLogger("urllib3").setLevel(logging.DEBUG)

        # Optional: enable low-level HTTP debug output
        try:
            import http.client as http_client
            http_client.HTTPConnection.debuglevel = 1
        except Exception:
            pass

    logging.getLogger(__name__).info(
        "Debug mode: %s, Debug logging: %s",
        _cfg_get(config, "debug_mode", default=False),
        _cfg_get(config, "debug_logging", default=False)
    )


def main():
    # 1. Load Config
    try:
        config = load_config()
        setup_logging(config)
    except Exception as e:
        print(f"Startup Error: {e}")
        return

    # Banner and initial log messages
    logging.info("*******************************************************************************************")
    logging.info("  _   _ __________  ______             _           ")
    logging.info(" | \\ | |___  /  _ \\|  ___|(_)         | |          ")
    logging.info(" |  \\| |  / /| |_) | |__   _ _ __   __| | ___ _ __ ")
    logging.info(" | . ` | / / |  _ <|  __| | | '_ \\ / _` |/ _ \\ '__|")
    logging.info(" | |\\  |/ /__| |_) | |    | | | | | (_| |  __/ |   ")
    logging.info(" |_| \\_/_____|____/|_|    |_|_| |_|\\__,_|\\___|_|   ")
    logging.info("*******************************************************************************************")
    logging.info("🚀 Loaded configuration from config.yaml")

    # 2. Init Engine (robustly read radarr settings from config)
    radarr_url = _cfg_get(config, "radarr", "url", default=_cfg_get(config, "radarr_url"))
    radarr_api_key = _cfg_get(config, "radarr", "api_key", default=_cfg_get(config, "radarr_api_key"))
    quality_profile = _cfg_get(config, "radarr", "quality_profile", default=_cfg_get(config, "quality_profile", default=1))
    quality_threshold = _cfg_get(config, "radarr", "quality_threshold", default=_cfg_get(config, "quality_threshold", default=0))
    root_folder = _cfg_get(config, "radarr", "root_folder", default=_cfg_get(config, "root_folder", default=""))

    try:
        processor = RadarrProcessor(
            url=radarr_url,
            api_key=radarr_api_key,
            quality_profile=int(quality_profile),
            threshold=int(quality_threshold),
            root_folder=root_folder,
            tmdb_api_key=os.getenv("TMDB_API_KEY")  # pass TMDb key from environment
        )
        # optional: log whether the key was provided
        logging.info("RadarrProcessor tmdb_api_key present: %s", bool(processor.tmdb_api_key))

    except Exception as e:
        logging.exception("Failed to initialize RadarrProcessor: %s", e)
        return

    # 3. Main Loop
    # Determine execution interval (minutes) with sensible fallbacks
    execution_interval = _cfg_get(config, "execution_interval",
                                  default=_cfg_get(config, "sleep_minutes",
                                                   default=_cfg_get(config, "poll_interval_minutes", default=30)))
    try:
        execution_interval = float(execution_interval)
    except Exception:
        execution_interval = 30.0

    while True:
        try:
            start_time = time.time()
            added, exists, excluded = run_rss_sync(config, processor)

            duration = round(time.time() - start_time, 2)
            logging.info("📊 Report: Pushed=%s | Exists/Skipped=%s | Time=%ss", added, exists, duration)

        except KeyboardInterrupt:
            logging.info("🛑 Stop signal received. Exiting.")
            break
        except Exception as e:
            logging.error("❌ Critical Loop Error: %s", e, exc_info=True)

        # Sleep
        logging.info("💤 Sleeping for %s minutes...", execution_interval)
        try:
            time.sleep(execution_interval * 60)
        except KeyboardInterrupt:
            logging.info("🛑 Stop signal received. Exiting.")
            break


if __name__ == "__main__":
    main()
