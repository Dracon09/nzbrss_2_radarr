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

    # Remove default handlers to prevent duplication
    root = logging.getLogger()
    if root.handlers:
        for handler in root.handlers:
            root.removeHandler(handler)

    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    # File Handler
    file_handler = TimedRotatingFileHandler(
        log_path, when="midnight", interval=1,
        backupCount=config.log_retention_days, encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)

    # Console Handler
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)

    logging.basicConfig(
        level=logging.DEBUG if config.debug_mode else logging.INFO,
        handlers=[file_handler, stream_handler],
        force=True
    )


def main():
    # 1. Load Config
    try:
        config = load_config()
        setup_logging(config)
    except Exception as e:
        print(f"Startup Error: {e}")
        return

    logging.info("***********************************************")
    logging.info("🚀 NZBFinder 2 Radarr (Sync Mode) Started")
    logging.info("***********************************************")

    # 2. Init Engine
    processor = RadarrProcessor(
        url=config.radarr_url,
        api_key=config.radarr_api_key,
        threshold=config.radarr.quality_threshold
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