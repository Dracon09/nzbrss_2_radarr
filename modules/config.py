import os
import sys
import yaml
import logging
from logging.handlers import TimedRotatingFileHandler
from pydantic import BaseModel
from typing import List, Optional


class FeedConfig(BaseModel):
    name: str
    url: str


class ConfigModel(BaseModel):
    config_folder: str = "config"  # Default config directory
    execution_interval: int = 15
    max_stored_guids: int = 1000
    log_retention_days: int = 7  # default to 7 days
    debug_mode: bool = False
    debug_logging: bool = False
    use_keyboard: bool = True
    movie_folder: str
    quality_profile: str
    match_patterns: Optional[List[str]] = []
    not_match_patterns: Optional[List[str]] = []
    rss_feeds: List[FeedConfig]
    radarr_url: str
    radarr_api_key: str
    invalid_movie_log_file: str  # Now required and injected


def load_config() -> ConfigModel:
    from dotenv import load_dotenv
    config_folder = "config"
    load_dotenv(os.path.join(config_folder, ".env"))

    try:
        with open(os.path.join(config_folder, "config.yaml"), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Inject .env and path-based values
        raw['radarr_url'] = os.getenv("RADARR_URL")
        raw['radarr_api_key'] = os.getenv("RADARR_API_KEY")
        raw['config_folder'] = config_folder
        raw['invalid_movie_log_file'] = os.path.join(config_folder, "invalid_movie.log")

        return ConfigModel(**raw)
    except Exception as e:
        print(f"❌ Failed to load config: {e}", file=sys.stderr)
        sys.exit(1)


def setup_logging(config: ConfigModel):
    log_path = os.path.join(config.config_folder, "script.log")

    for h in logging.getLogger().handlers:
        logging.getLogger().removeHandler(h)

    log_formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = TimedRotatingFileHandler(
        log_path,
        when="midnight",
        interval=1,
        backupCount=config.log_retention_days,
        encoding="utf-8"
    )
    file_handler.setFormatter(log_formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(log_formatter)

    logging.basicConfig(
        level=logging.DEBUG if config.debug_logging else logging.INFO,
        handlers=[file_handler, stream_handler],
    )
