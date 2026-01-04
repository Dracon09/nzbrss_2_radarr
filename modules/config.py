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
    quality_threshold: int = 18


class ConfigModel(BaseModel):
    config_folder: str = "config"
    execution_interval: int = 15
    max_stored_guids: int = 1000
    log_retention_days: int = 7
    debug_mode: bool = False
    debug_logging: bool = False
    use_keyboard: bool = True
    match_patterns: Optional[List[str]] = None
    not_match_patterns: Optional[List[str]] = None
    rss_feeds: List[FeedConfig]
    radarr: RadarrConfig

    # Internal fields (injected from environment)
    radarr_url: Optional[str] = None
    radarr_api_key: Optional[str] = None

def load_config() -> ConfigModel:
    # Resolve project base and config folder absolute path
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_folder = os.path.join(base_dir, "config")

    # Load env vars from config/.env
    env_path = os.path.join(config_folder, ".env")
    load_dotenv(env_path)

    radarr_url = os.getenv("RADARR_URL")
    radarr_api_key = os.getenv("RADARR_API_KEY")

    # If env vars are missing, fail with a clear message
    if not radarr_url or not radarr_api_key:
        print("❌ CRITICAL: RADARR_URL or RADARR_API_KEY missing from config/.env", file=sys.stderr)
        sys.exit(1)

    try:
        cfg_path = os.path.join(config_folder, "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        # Ensure radarr section exists
        raw.setdefault("radarr", {})
        radarr_section = raw["radarr"]

        # If YAML only has quality_profile, keep it and ensure threshold default exists
        if "quality_profile" in radarr_section and "quality_threshold" not in radarr_section:
            # keep the profile and set a sensible default threshold if missing
            radarr_section["quality_threshold"] = int(radarr_section.get("quality_threshold", 18))
        else:
            radarr_section["quality_threshold"] = int(radarr_section.get("quality_threshold", 18))

        # No change to quality_profile; ensure it's present
        if "quality_profile" not in radarr_section:
            radarr_section["quality_profile"] = int(radarr_section.get("quality_profile", 8))

        # Inject environment values and absolute config folder path
        raw["radarr_url"] = radarr_url
        raw["radarr_api_key"] = radarr_api_key
        raw["config_folder"] = config_folder

        # Ensure optional lists are present as None or lists
        if "match_patterns" not in raw:
            raw["match_patterns"] = None
        if "not_match_patterns" not in raw:
            raw["not_match_patterns"] = None

        return ConfigModel(**raw)
    except Exception as e:
        print(f"❌ Failed to load config.yaml: {e}", file=sys.stderr)
        sys.exit(1)
