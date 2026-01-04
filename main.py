# main.py

import time
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
import os

from modules.config import load_config
from modules.radarr_pyarr import RadarrProcessor
from modules.rss import run_rss_sync


def setup_logging(config):
    log_path = os.path.join(config.config_folder, "script.log")

    # Remove default handlers to prevent duplication on re-run
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)

    base_level = logging.DEBUG if getattr(config, "debug_logging", False) else logging.INFO
    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s")

    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1,
        backupCount=getattr(config, "log_retention_days", 7), encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)
    file_handler.setLevel(base_level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)
    stream_handler.setLevel(base_level)

    logging.basicConfig(level=base_level, handlers=[file_handler, stream_handler], force=True)

    if getattr(config, "debug_logging", False):
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
        getattr(config, "debug_mode", False),
        getattr(config, "debug_logging", False)
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

    # Log key config values (hide secrets)
    logging.info("🎬 RadarrPyarr initialized")
    logging.info("    Quality Profile: %s", getattr(config.radarr, "quality_profile", "N/A"))
    logging.info("    Quality Threshold: %s", getattr(config.radarr, "quality_threshold", "N/A"))
    logging.info("    Root Folder: %s", getattr(config.radarr, "root_folder", "N/A"))
    logging.info("    Radarr URL: %s", config.radarr_url or "N/A")
    logging.info("    Radarr API Key: %s", "***hidden***")

    # 2. Init Engine
    processor = RadarrProcessor(
        url=config.radarr_url,
        api_key=config.radarr_api_key,
        quality_profile=config.radarr.quality_profile,
        threshold=config.radarr.quality_threshold,
        root_folder=config.radarr.root_folder
    )

    # 3. Main Loop
    while True:
        try:
            start_time = time.time()
            added, exists, excluded = run_rss_sync(config, processor)

            duration = round(time.time() - start_time, 2)
            logging.info(f"📊 Report: Pushed={added} | Exists/Skipped={exists} | Time={duration}s")

        except KeyboardInterrupt:
            logging.info("🛑 Stop signal received. Exiting.")
            break
        except Exception as e:
            logging.error(f"❌ Critical Loop Error: {e}")

        # Sleep
        logging.info(f"💤 Sleeping for {config.execution_interval} minutes...")
        try:
            time.sleep(config.execution_interval * 60)
        except KeyboardInterrupt:
            logging.info("🛑 Stop signal received. Exiting.")
            break


if __name__ == "__main__":
    main()