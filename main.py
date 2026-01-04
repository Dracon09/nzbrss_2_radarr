from modules.config import load_config, setup_logging
from modules.radarr import setup_radarr
from modules.rss import run_rss_processing
from modules.util import setup_signals, countdown, listen_for_manual_run
import threading
import sys
import logging
import os


# Global Counters
total_added = total_exists = total_invalid = total_excluded = 0

def is_docker() -> bool:
    """Detect if running inside a Docker container."""
    return os.path.exists("/.dockerenv") or os.path.isfile("/proc/1/cgroup") and "docker" in open("/proc/1/cgroup", "rt").read()

def main():
    config = load_config()
    setup_logging(config)

    logging.info("*******************************************************************************************")
    logging.info("  _   _ __________  ______             _           ")
    logging.info(" | \\ | |___  /  _ \\|  ___|(_)         | |          ")
    logging.info(" |  \\| |  / /| |_) | |__   _ _ __   __| | ___ _ __ ")
    logging.info(" | . ` | / / |  _ <|  __| | | '_ \\ / _` |/ _ \\ '__|")
    logging.info(" | |\\  |/ /__| |_) | |    | | | | | (_| |  __/ |   ")
    logging.info(" |_| \\_/_____|____/|_|    |_|_| |_|\\__,_|\\___|_|   ")
    logging.info("*******************************************************************************************")
    logging.info("🚀 Loaded configuration from config.yaml")

    radarr = setup_radarr(config.radarr_url, config.radarr_api_key)
    stop_event, manual_event = setup_signals()

    if config.use_keyboard and not is_docker():
        threading.Thread(target=listen_for_manual_run, args=(manual_event, stop_event), daemon=True).start()
    else:
        logging.info("⌨️ Keyboard input disabled (not supported in Docker)")

    # Initialize cumulative counters
    total_added = total_exists = total_invalid = total_excluded = 0

    def run_and_log():
        nonlocal total_added, total_exists, total_invalid, total_excluded
        added, exists, invalid, excluded = run_rss_processing(config, radarr, stop_event)
        total_added += added
        total_exists += exists
        total_invalid += invalid
        total_excluded += excluded

        logging.info(
            f"📊 Cumulative Summary: "
            f"Total Added: {total_added}, "
            f"Total Exists: {total_exists}, "
            f"Total Invalid: {total_invalid}, "
            f"Total Excluded: {total_excluded}"
        )

    # First run
    run_and_log()

    while not stop_event.is_set():
        if countdown(config.execution_interval * 60, manual_event, stop_event):
            logging.info("🟢 Manual execution triggered!")
            manual_event.clear()
        run_and_log()

    logging.info("🛑 Exiting.")

if __name__ == "__main__":
    main()