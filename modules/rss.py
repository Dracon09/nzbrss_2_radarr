# modules/rss.py

import os
import re
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET
from modules.util import filter_title, redact_url_query

# Import the SQLite wrapper
from modules.sqlite_store import SQLiteStore

# Module-level HTTP session with retries/backoff to make feed fetching more resilient
_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"]
)
_adapter = HTTPAdapter(max_retries=_retry_strategy)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)


def expand_url(url):
    return os.path.expandvars(url)


def compile_patterns(config):
    inc = re.compile("|".join(config.match_patterns), re.IGNORECASE) if config.match_patterns else None
    exc = re.compile("|".join(config.not_match_patterns), re.IGNORECASE) if config.not_match_patterns else None
    return inc, exc


def _extract_entry_fields(itm: ET.Element):
    """
    Defensive extractor for an <item> element from different NZB/newznab feeds.
    Returns a tuple: (title, nzb_url, pubDate, guid, imdb_digits_or_None, size)
    - imdb is returned without the 'tt' prefix (e.g. '17154734') if found.
    """
    # Title (try common namespaced variants)
    title = itm.findtext("title") or itm.findtext("{http://purl.org/dc/elements/1.1/}title")

    # NZB URL: prefer enclosure url, then link element
    nzb_url = None
    size = None
    enc = itm.find("enclosure")
    if enc is not None:
        nzb_url = enc.get("url") or enc.get("href")
        # enclosure length attribute may be string
        try:
            length = enc.get("length") or enc.get("size")
            if length:
                size = int(length)
        except Exception:
            size = None

    if not nzb_url:
        # Some feeds put the NZB link in <link>
        nzb_url = itm.findtext("link")

    # pubDate
    pubdate = itm.findtext("pubDate") or itm.findtext("published") or itm.findtext("{http://purl.org/dc/elements/1.1/}date")

    # GUID: prefer <guid>, then newznab attr 'guid', then link fallback
    guid = itm.findtext("guid") or itm.findtext("id") or itm.findtext("link")

    # Scan for newznab:attr elements (namespace-agnostic)
    imdb_val = None
    guid_attr_val = None
    # ElementTree represents namespaced tags as '{ns}local'
    for elem in itm.iter():
        tag = elem.tag
        if not isinstance(tag, str):
            continue
        # handle <newznab:attr name="..." value="..."/> and non-namespaced <attr/>
        if tag.endswith("}attr") or tag == "attr":
            name = elem.get("name")
            value = elem.get("value")
            if not name or value is None:
                # also handle <attr name="size">1018733261</attr> style
                try:
                    name = elem.get("name") or (elem.text and elem.tag and elem.tag.lower())
                except Exception:
                    name = None
                value = value or (elem.text.strip() if elem.text else None)
            if not name or value is None:
                continue
            lname = name.lower()
            if lname == "imdb" and value:
                imdb_val = value.strip()
            if lname == "guid" and value:
                guid_attr_val = value.strip()
            if lname == "size" and value:
                try:
                    size = int(value)
                except Exception:
                    pass

    # If guid missing, try guid_attr
    if (not guid or guid.strip() == "") and guid_attr_val:
        guid = guid_attr_val

    # If imdb still missing, try to extract from description/summary with regex
    if not imdb_val:
        desc = itm.findtext("description") or itm.findtext("summary") or ""
        if desc:
            # look for imdb.com/title/tt1234567 (robust to http/https and optional www)
            m = re.search(r'imdb\.com/title/(?:tt)?(\d{6,9})', desc, re.IGNORECASE)
            if m:
                imdb_val = m.group(1)
            else:
                # look for newznab attr embedded in HTML
                m2 = re.search(r'<newznab:attr\s+name=["\']imdb["\']\s+value=["\'](\d{6,9})["\']', desc, re.IGNORECASE)
                if m2:
                    imdb_val = m2.group(1)
                else:
                    # sometimes description contains "IMDB Link: ...tt1234567" or "IMDB: 1234567"
                    m3 = re.search(r'(?:IMDB Link[:\s]*|IMDB[:\s]*|imdb[:\s]*)?(?:https?://(?:www\.)?imdb\.com/title/)?(?:tt)?(\d{6,9})', desc, re.IGNORECASE)
                    if m3:
                        imdb_val = m3.group(1)

    # Normalize imdb_val: strip leading zeros while length > 7 to fix padded values
    if imdb_val and imdb_val.isdigit():
        while len(imdb_val) > 7 and imdb_val.startswith("0"):
            imdb_val = imdb_val[1:]

    # Normalize guid to a short token for dedupe
    clean_guid = None
    if guid:
        g = guid.strip()
        if "/" in g:
            qmatch = re.search(r'[?&](?:id|guid)=([^&]+)', g)
            if qmatch:
                clean_guid = qmatch.group(1)
            else:
                clean_guid = g.rstrip("/").split("/")[-1]
        else:
            clean_guid = g
    else:
        clean_guid = "unknown"

    return title, nzb_url, pubdate, clean_guid, imdb_val, size


def run_rss_sync(config, radarr_processor):
    added = exists = excluded = 0

    # Initialize SQLite store (DB path can be overridden via SCANNED_DB_PATH env or config)
    db_path = getattr(config, "scanned_db_path", None) or os.path.join(config.config_folder, "scanned.db")
    store = SQLiteStore(db_path)

    # Migrate any existing scanned_*.txt files (including scanned_guids.txt) into the DB
    try:
        migrated = store.migrate_from_feed_files(config.config_folder, file_pattern="scanned_*.txt")
        if migrated:
            logging.info("   Migrated %d GUIDs from scanned_*.txt files into SQLite DB", migrated)
    except Exception:
        logging.debug("No migration performed or migration failed", exc_info=True)

    # Log a sample of recent GUIDs for visibility
    try:
        recent = list(store.list_recent(limit=10))
        logging.info("   Loaded %d recent GUIDs from DB (sample): %s", len(recent), ", ".join(r["guid"] for r in recent))
    except Exception:
        logging.debug("Could not read recent GUIDs from DB", exc_info=True)

    inc_pattern, exc_pattern = compile_patterns(config)

    try:
        for feed in config.rss_feeds:
            real_url = expand_url(feed.url)
            clean_log_url = redact_url_query(real_url)
            logging.info("📡 Fetching RSS: %s (%s)", feed.name, clean_log_url)

            try:
                # Use the resilient session with retries
                try:
                    response = _session.get(real_url, timeout=30)
                    response.raise_for_status()
                except requests.exceptions.ConnectionError as ce:
                    cause = getattr(ce, "__cause__", None)
                    if cause and "NameResolutionError" in type(cause).__name__:
                        logging.error("❌ DNS resolution failed for feed %s (%s). Check DNS, VPN, proxy, or hosts file.", feed.name, clean_log_url)
                    else:
                        logging.error("❌ Connection error fetching feed %s: %s", feed.name, ce)
                    continue
                except requests.exceptions.RequestException as rexc:
                    logging.error("❌ Error fetching feed %s: %s", feed.name, rexc)
                    continue

                # Parse XML and find items in a tolerant way
                root = ET.fromstring(response.content)
                items = root.findall(".//item")
                logging.info("   Found %d items.", len(items))

                feed_new = 0

                for itm in items:
                    title, nzb_url, rss_date, clean_guid, imdb_digits, size = _extract_entry_fields(itm)

                    # Skip if we've already seen this GUID for this feed
                    if store.has_seen(feed.name, clean_guid):
                        logging.debug("   [Seen] %s (guid=%s)", title or "unknown", clean_guid)
                        continue

                    # If title doesn't match include/exclude patterns, mark seen and skip
                    if not filter_title(title, inc_pattern, exc_pattern):
                        logging.info("   ❌ NOT MATCHED: %s", title or "unknown")
                        excluded += 1
                        store.mark_seen(feed.name, clean_guid, title=title, imdb_id=None, size=size)
                        feed_new += 1
                        continue

                    if not nzb_url:
                        logging.debug("   Skipping entry without NZB URL: %s (guid=%s)", title or "unknown", clean_guid)
                        excluded += 1
                        store.mark_seen(feed.name, clean_guid, title=title, imdb_id=None, size=size)
                        feed_new += 1
                        continue

                    imdb_id = None
                    if imdb_digits:
                        imdb_digits = imdb_digits.strip()
                        imdb_digits = imdb_digits[2:] if imdb_digits.startswith("tt") else imdb_digits
                        if imdb_digits.isdigit():
                            imdb_id = f"tt{imdb_digits}"

                    if not imdb_id and title:
                        m = re.search(r'(tt\d{6,9}|\d{6,9})', title)
                        if m:
                            candidate = m.group(1)
                            candidate = candidate[2:] if candidate.startswith("tt") else candidate
                            if candidate.isdigit():
                                imdb_id = f"tt{candidate}"

                    if imdb_id and title:
                        logging.info("   ✅ MATCHED (IMDb): %s (%s)", title, imdb_id)
                    else:
                        logging.info("   ❌ NOT MATCHED: %s (missing imdb or title)", title or "unknown title")
                        excluded += 1
                        store.mark_seen(feed.name, clean_guid, title=title, imdb_id=None, size=size)
                        feed_new += 1
                        continue

                    # Call the processor with size when available
                    result = radarr_processor.process_release(title, imdb_id, nzb_url, rss_date, size=size)

                    if result == "PUSHED":
                        logging.info("   ✅ PUSHED: %s (%s)", title, imdb_id)
                        added += 1
                    elif result == "ADDED":
                        logging.info("   ➕ ADDED: %s (%s)", title, imdb_id)
                        added += 1
                    elif result == "QUALITY_MET":
                        logging.info("   ℹ️ QUALITY MET: %s (%s)", title, imdb_id)
                        exists += 1
                    elif result == "NOT_IN_LIBRARY":
                        logging.info("   ❌ NOT IN LIBRARY: %s (%s)", title, imdb_id)
                        excluded += 1
                    elif result == "API_ERROR":
                        logging.warning("   ⚠️ API ERROR: %s (%s) — will retry later", title, imdb_id)
                        # Do not mark as seen so it can be retried later
                        continue
                    elif result == "REJECTED":
                        logging.info("   ⛔ PUSH REJECTED: %s (%s)", title, imdb_id)
                    elif result == "UNKNOWN_RESPONSE":
                        logging.warning("   ❓ UNKNOWN RESPONSE for %s (%s)", title, imdb_id)
                    else:
                        # Treat ALREADY_HAVE_BETTER as informational (expected) rather than unexpected
                        if result == "ALREADY_HAVE_BETTER":
                            logging.info("   ℹ️ SKIPPED (already have equal or better): %s (%s)", title, imdb_id)
                        else:
                            logging.warning("   ❗ Unexpected result '%s' for %s (%s)", result, title, imdb_id)

                    # Mark GUID as seen for this feed (successful or rejected pushes are recorded)
                    store.mark_seen(feed.name, clean_guid, title=title, imdb_id=(imdb_id[2:] if imdb_id and imdb_id.startswith("tt") else imdb_id), size=size)
                    feed_new += 1

                logging.info("   Feed %s: new GUIDs this run: %d", feed.name, feed_new)

            except Exception as e:
                logging.error("❌ Error processing feed %s: %s", feed.name, e, exc_info=True)

        # Prune old rows if configured (default 90 days)
        prune_days = getattr(config, "prune_days", 90)
        try:
            deleted = store.prune_older_than(days=prune_days)
            logging.info("   Pruned %d old GUID rows older than %d days", deleted, prune_days)
        except Exception:
            logging.debug("Prune failed", exc_info=True)

    finally:
        try:
            store.close()
        except Exception:
            logging.debug("Failed to close SQLite store cleanly", exc_info=True)

    return added, exists, excluded
