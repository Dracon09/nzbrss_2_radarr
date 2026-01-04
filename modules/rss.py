# modules/rss.py

import os
import re
import logging
import requests
import xml.etree.ElementTree as ET
from modules.util import filter_title, redact_url_query


# Fix: Ensure we can expand ${VARS} in the URL
def expand_url(url):
    return os.path.expandvars(url)


def compile_patterns(config):
    """Compiles regex patterns from config lists"""
    # We join patterns with OR (|) so any match is good
    inc = re.compile("|".join(config.match_patterns), re.IGNORECASE) if config.match_patterns else None
    exc = re.compile("|".join(config.not_match_patterns), re.IGNORECASE) if config.not_match_patterns else None
    return inc, exc


def run_rss_sync(config, radarr_processor):
    added = exists = excluded = 0

    # Load previously seen GUIDs
    guid_file = os.path.join(config.config_folder, "scanned_guids.txt")
    seen = set()
    if os.path.exists(guid_file):
        try:
            with open(guid_file, "r", encoding="utf-8") as f:
                seen = set(f.read().splitlines())
        except Exception:
            logging.warning("⚠️ Could not read scanned_guids.txt, starting fresh.")

    # Compile Regex
    inc_pattern, exc_pattern = compile_patterns(config)

    new_guids = []

    for feed in config.rss_feeds:
        # CRITICAL FIX: Expand ${NZBFINDER_API_KEY} to the real key
        real_url = expand_url(feed.url)

        # Log the URL but hide the key for safety
        clean_log_url = redact_url_query(real_url)
        logging.info(f"📡 Fetching RSS: {feed.name} ({clean_log_url})")

        try:
            # 30s timeout is safer for large RSS feeds
            response = requests.get(real_url, timeout=30)
            response.raise_for_status()

            # Parse XML
            root = ET.fromstring(response.content)
            items = root.findall("./channel/item")
            logging.info(f"   Found {len(items)} items.")

            for itm in items:
                # 1. GUID Check
                guid = itm.findtext("guid")
                if not guid: guid = itm.findtext("link")

                # Clean GUID (often is a URL, we just want the last part)
                clean_guid = guid.split("/")[-1] if guid else "unknown"

                if clean_guid in seen:
                    continue

                # 2. Title Filter
                title = itm.findtext("title")
                if not filter_title(title, inc_pattern, exc_pattern):
                    # Only log excluded items if debug mode is on to keep logs clean
                    if config.debug_mode:
                        logging.info(f"   [Excluded] {title}")
                    excluded += 1
                    new_guids.append(clean_guid)
                    continue

                # 3. Extract Info
                nzb_url = itm.findtext("link")
                rss_date = itm.findtext("pubDate")

                # Extract IMDb (Handling the specific namespaces of your indexers)
                imdb_id = None
                # Try standard attribute (NZBFinder usually uses this)
                for attr in itm.findall(".//{https://nzbfinder.ws/rsshelp/}attr"):
                    if attr.get("name") == "imdb":
                        imdb_id = attr.get("value")

                # Fallback for other indexers (newznab standard)
                if not imdb_id:
                    for attr in itm.findall(".//{http://www.newznab.com/DTD/2010/feeds/attributes/}attr"):
                        if attr.get("name") == "imdb":
                            imdb_id = attr.get("value")

                # Clean IMDb ID (ensure tt prefix)
                if imdb_id and not imdb_id.startswith("tt"):
                    imdb_id = f"tt{imdb_id}"

                if not imdb_id or not title:
                    new_guids.append(clean_guid)
                    continue

                # 4. PROCESS THE MOVIE
                result = radarr_processor.process_release(title, imdb_id, nzb_url, rss_date)

                if result == "PUSHED":
                    added += 1
                elif result == "QUALITY_MET":
                    exists += 1
                elif result == "NOT_IN_LIBRARY":
                    excluded += 1
                elif result == "API_ERROR":
                    # If API fails, don't mark as seen so we retry next time
                    continue

                    # Mark as seen so we don't process again
                new_guids.append(clean_guid)

        except Exception as e:
            logging.error(f"❌ Error fetching feed {feed.name}: {e}")

    # Save new GUIDs
    if new_guids:
        # Update set
        seen.update(new_guids)
        # Keep only last N items to prevent file from growing forever
        final_list = list(seen)[-config.max_stored_guids:]

        with open(guid_file, "w", encoding="utf-8") as f:
            f.write("\n".join(final_list))

    return added, exists, excluded