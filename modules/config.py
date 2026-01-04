# modules/config.py

import os
import sys
import yaml
from pydantic import BaseModel
from typing import List, Optional
from dotenv import load_dotenv


# Matches your YAML 'rss_feeds' list
class FeedConfig(BaseModel):
    name: str
    url: str


# Matches your YAML 'radarr' section
class RadarrConfig(BaseModel):
    quality_profile: int
    root_folder: str
    # If you add 'quality_threshold: 21' to yaml later, it overrides this 18
    quality_threshold: int = 18


class ConfigModel(BaseModel):
    config_folder: str = "config"
    execution_interval: int = 15
    max_stored_guids: int = 1000
    log_retention_days: int = 7
    debug_mode: bool = False
    debug_logging: bool = False
    use_keyboard: bool = True
    match_patterns: Optional[List[str]] = []
    not_match_patterns: Optional[List[str]] = []
    rss_feeds: List[FeedConfig]
    radarr: RadarrConfig

    # Internal fields (hidden from YAML)
    radarr_url: Optional[str] = None
    radarr_api_key: Optional[str] = None


def load_config() -> ConfigModel:
    config_folder = "config"
    # Load env vars (where your API keys live)
    load_dotenv(os.path.join(config_folder, ".env"))

    radarr_url = os.getenv("RADARR_URL")
    radarr_api_key = os.getenv("RADARR_API_KEY")

    if not radarr_url or not radarr_api_key:
        print("❌ CRITICAL: RADARR_URL or RADARR_API_KEY missing from .env", file=sys.stderr)
        sys.exit(1)

    try:
        with open(os.path.join(config_folder, "config.yaml"), "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)

        # Inject the env vars into the config model
        raw['radarr_url'] = radarr_url
        raw['radarr_api_key'] = radarr_api_key
        raw['config_folder'] = config_folder

        return ConfigModel(**raw)
    except Exception as e:
        print(f"❌ Failed to load config.yaml: {e}", file=sys.stderr)
        sys.exit(1)