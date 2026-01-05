# modules/util.py

import re
from typing import Optional, List
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse


def filter_title(title: str, inc: Optional[re.Pattern], exc: Optional[re.Pattern]) -> bool:
    """
    Return True if title matches include patterns (if any)
    and does NOT match exclude patterns.
    """
    if not title:
        return False
    # If patterns exist, they must match
    if inc and not inc.search(title):
        return False
    # If exclude patterns exist, they must NOT match
    if exc and exc.search(title):
        return False
    return True


def redact_url_query(url: str, sensitive_keys: List[str] = ["apikey", "api_token", "r"]) -> str:
    """
    Return URL with sensitive query parameters redacted.
    Useful for logging RSS URLs without leaking keys.
    """
    try:
        parsed = urlparse(url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        # Redact keys found in the sensitive list
        redacted = [(k, "****") if k.lower() in sensitive_keys else (k, v) for k, v in query]

        redacted_query = urlencode(redacted)
        return urlunparse(parsed._replace(query=redacted_query))
    except Exception:
        return "redacted_url_error"

ICON_WIDTH = 3  # visual column width for emoji alignment


def log_icon(logger, icon: str, message: str, level: str = "info"):
    """
    Log a message with a fixed-width emoji/icon column so icons align
    across all modules and loggers.
    """
    padded_icon = f"{icon:<{ICON_WIDTH}}"
    log_func = getattr(logger, level, logger.info)
    log_func(f"{padded_icon} {message}")
