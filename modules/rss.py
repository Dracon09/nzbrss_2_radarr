# modules/rss.py

import os
import re
import logging
import requests
import xml.etree.ElementTree as ET
from modules.util import filter_title, redact_url_query


def expand_url(url):
    return os.path.expandvars(url)


def compile_patterns(config):
    inc = re.compile("|".join(config.match_patterns), re.IGNORECASE) if config.match_patterns else None
    exc = re.compile("|".join(config.not_match_patterns), re.IGNORECASE) if config.not_match_patterns else None
    return inc, exc


def run_rss_sync(config, radarr_processor):
    added = exists = excluded = 0

    guid_file = os.path.join(config.config_folder, "scanned_guids.txt")
    # Use a list to preserve insertion order; we'll keep only the last N entries
    seen_list = []
    seen_set = set()
    if os.path.exists(guid_file):
        try:
            with open(guid_file, "r", encoding="utf-8") as f:
                for line in f:
                    g = line.strip()
                    if g:
                        seen_list.append(g)
                        seen_set.add(g)
        except Exception:
            logging.warning("⚠️ Could not read scanned_guids.txt, starting fresh.")

    # Log how many GUIDs were loaded and show a small sample
    try:
        logging.info("   Loaded %d seen GUIDs from %s", len(seen_set), guid_file)
        if len(seen_set) > 0:
            sample = list(seen_set)[:10]
            logging.info("   Sample seen GUIDs: %s", ", ".join(sample))
    except Exception:
        # Non-fatal logging issue should not stop processing
        logging.debug("Could not log scanned_guids sample", exc_info=True)

    inc_pattern, exc_pattern = compile_patterns(config)
    new_guids = []

    for feed in config.rss_feeds:
        real_url = expand_url(feed.url)
        clean_log_url = redact_url_query(real_url)
        logging.info("📡 Fetching RSS: %s (%s)", feed.name, clean_log_url)

        try:
            response = requests.get(real_url, timeout=30)
            response.raise_for_status()

            root = ET.fromstring(response.content)
            items = root.findall("./channel/item")
            logging.info("   Found %d items.", len(items))

            feed_new_count_before = len(new_guids)

            for itm in items:
                guid = itm.findtext("guid") or itm.findtext("link")
                clean_guid = guid.split("/")[-1] if guid else "unknown"

                # Already seen
                if clean_guid in seen_set:
                    logging.info("   [Seen] %s (guid=%s)", itm.findtext("title") or "unknown", clean_guid)
                    continue

                title = itm.findtext("title")

                # Title filter
                if not filter_title(title, inc_pattern, exc_pattern):
                    # Log excluded items at INFO for visibility
                    logging.info("   ❌ NOT MATCHED: %s", title or "unknown")
                    excluded += 1
                    new_guids.append(clean_guid)
                    seen_set.add(clean_guid)
                    continue

                # Extract basic fields
                nzb_url = itm.findtext("link")
                rss_date = itm.findtext("pubDate")

                # Extract IMDb id
                imdb_id = None
                for attr in itm.findall(".//{https://nzbfinder.ws/rsshelp/}attr"):
                    if attr.get("name") == "imdb":
                        imdb_id = attr.get("value")
                if not imdb_id:
                    for attr in itm.findall(".//{http://www.newznab.com/DTD/2010/feeds/attributes/}attr"):
                        if attr.get("name") == "imdb":
                            imdb_id = attr.get("value")

                if imdb_id and not imdb_id.startswith("tt"):
                    imdb_id = f"tt{imdb_id}"

                # Log match when we have title + imdb
                if imdb_id and title:
                    logging.info("   ✅ MATCHED (IMDb): %s (%s)", title, imdb_id)
                else:
                    logging.info("   ❌ NOT MATCHED: %s (missing imdb or title)", title or "unknown title")
                    # mark seen so we don't retry forever
                    new_guids.append(clean_guid)
                    seen_set.add(clean_guid)
                    excluded += 1
                    continue

                # Process the movie with Radarr
                result = radarr_processor.process_release(title, imdb_id, nzb_url, rss_date)

                # Log result per item
                if result == "PUSHED":
                    logging.info("   ✅ PUSHED: %s (%s)", title, imdb_id)
                    added += 1
                elif result == "ADDED":
                    logging.info(f" ➕ ADDED: {title} ({imdb_id})")
                    added += 1
                elif result == "QUALITY_MET":
                    logging.info("   ℹ️ QUALITY MET: %s (%s)", title, imdb_id)
                    exists += 1
                elif result == "NOT_IN_LIBRARY":
                    logging.info("   ❌ NOT IN LIBRARY: %s (%s)", title, imdb_id)
                    excluded += 1
                elif result == "API_ERROR":
                    logging.warning("   ⚠️ API ERROR: %s (%s) — will retry later", title, imdb_id)
                    # Do not mark as seen so we retry next time
                    continue
                elif result == "REJECTED":
                    logging.info("   ⛔ PUSH REJECTED: %s (%s)", title, imdb_id)
                elif result == "UNKNOWN_RESPONSE":
                    logging.warning("   ❓ UNKNOWN RESPONSE for %s (%s)", title, imdb_id)
                else:
                    logging.warning("   ❗ Unexpected result '%s' for %s (%s)", result, title, imdb_id)

                # Mark as seen so we don't process again
                new_guids.append(clean_guid)
                seen_set.add(clean_guid)

            # Log how many new GUIDs were discovered for this feed in this run
            feed_new = len(new_guids) - feed_new_count_before
            logging.info("   Feed %s: new GUIDs this run: %d", feed.name, feed_new)

        except Exception as e:
            logging.error("❌ Error fetching feed %s: %s", feed.name, e, exc_info=True)

    # Persist GUIDs preserving order and keeping only the last N
    if new_guids:
        # Append new_guids to the end of seen_list while avoiding duplicates
        for g in new_guids:
            if g not in seen_list:
                seen_list.append(g)

        # Keep only the last N entries
        final_list = seen_list[-config.max_stored_guids:]

        logging.info("   Persisting %d GUIDs to %s (keeping last %d)", len(final_list), guid_file, config.max_stored_guids)
        try:
            with open(guid_file, "w", encoding="utf-8") as f:
                f.write("\n".join(final_list))
        except Exception:
            logging.error("⚠️ Failed to write scanned_guids.txt", exc_info=True)

    return added, exists, excluded
