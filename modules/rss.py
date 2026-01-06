# modules/rss.py

import os
import re
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import xml.etree.ElementTree as ET
from modules.util import filter_title, redact_url_query


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
    Returns a tuple: (title, nzb_url, pubDate, guid, imdb_digits_or_None)
    - imdb is returned without the 'tt' prefix (e.g. '17154734') if found.
    """
    # Title
    title = itm.findtext("title") or itm.findtext("{http://purl.org/dc/elements/1.1/}title")

    # NZB URL: prefer enclosure url, then link element
    nzb_url = None
    enc = itm.find("enclosure")
    if enc is not None:
        nzb_url = enc.get("url") or enc.get("href")
    if not nzb_url:
        nzb_url = itm.findtext("link")

    # pubDate
    pubdate = itm.findtext("pubDate") or itm.findtext("published") or itm.findtext("{http://purl.org/dc/elements/1.1/}date")

    # GUID: prefer <guid>, then newznab attr 'guid', then link fallback
    guid = itm.findtext("guid") or itm.findtext("id") or itm.findtext("link")

    # Scan for newznab:attr elements (namespace-agnostic)
    imdb_val = None
    guid_attr_val = None
    for elem in itm.iter():
        tag = elem.tag
        # ElementTree represents namespaced tags as '{ns}local'
        if isinstance(tag, str) and (tag.endswith("}attr") or tag == "attr"):
            name = elem.get("name")
            value = elem.get("value")
            if not name or value is None:
                continue
            lname = name.lower()
            if lname == "imdb" and value:
                imdb_val = value.strip()
            if lname == "guid" and value:
                guid_attr_val = value.strip()

    # If guid missing, try guid_attr
    if (not guid or guid.strip() == "") and guid_attr_val:
        guid = guid_attr_val

    # If imdb still missing, try to extract from description/summary with regex
    if not imdb_val:
        desc = itm.findtext("description") or itm.findtext("summary") or ""
        if desc:
            m = re.search(r'imdb\.com/title/(?:tt)?(\d{6,9})', desc, re.IGNORECASE)
            if m:
                imdb_val = m.group(1)
            else:
                m2 = re.search(r'<newznab:attr\s+name=["\']imdb["\']\s+value=["\'](\d{6,9})["\']', desc, re.IGNORECASE)
                if m2:
                    imdb_val = m2.group(1)

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

    return title, nzb_url, pubdate, clean_guid, imdb_val


def run_rss_sync(config, radarr_processor):
    added = exists = excluded = 0

    guid_file = os.path.join(config.config_folder, "scanned_guids.txt")
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

    try:
        logging.info("   Loaded %d seen GUIDs from %s", len(seen_set), guid_file)
        if len(seen_set) > 0:
            sample = list(seen_set)[:10]
            logging.info("   Sample seen GUIDs: %s", ", ".join(sample))
    except Exception:
        logging.debug("Could not log scanned_guids sample", exc_info=True)

    inc_pattern, exc_pattern = compile_patterns(config)
    new_guids = []

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

            root = ET.fromstring(response.content)
            items = root.findall("./channel/item")
            logging.info("   Found %d items.", len(items))

            feed_new_count_before = len(new_guids)

            for itm in items:
                title, nzb_url, rss_date, clean_guid, imdb_digits = _extract_entry_fields(itm)

                if clean_guid in seen_set:
                    logging.debug("   [Seen] %s (guid=%s)", title or "unknown", clean_guid)
                    continue

                if not filter_title(title, inc_pattern, exc_pattern):
                    logging.info("   ❌ NOT MATCHED: %s", title or "unknown")
                    excluded += 1
                    new_guids.append(clean_guid)
                    seen_set.add(clean_guid)
                    continue

                if not nzb_url:
                    logging.debug("   Skipping entry without NZB URL: %s (guid=%s)", title or "unknown", clean_guid)
                    new_guids.append(clean_guid)
                    seen_set.add(clean_guid)
                    excluded += 1
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
                    new_guids.append(clean_guid)
                    seen_set.add(clean_guid)
                    excluded += 1
                    continue

                result = radarr_processor.process_release(title, imdb_id, nzb_url, rss_date)

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
                    continue
                elif result == "REJECTED":
                    logging.info("   ⛔ PUSH REJECTED: %s (%s)", title, imdb_id)
                elif result == "UNKNOWN_RESPONSE":
                    logging.warning("   ❓ UNKNOWN RESPONSE for %s (%s)", title, imdb_id)
                else:
                    logging.warning("   ❗ Unexpected result '%s' for %s (%s)", result, title, imdb_id)

                new_guids.append(clean_guid)
                seen_set.add(clean_guid)

            feed_new = len(new_guids) - feed_new_count_before
            logging.info("   Feed %s: new GUIDs this run: %d", feed.name, feed_new)

        except Exception as e:
            logging.error("❌ Error processing feed %s: %s", feed.name, e, exc_info=True)

    if new_guids:
        for g in new_guids:
            if g not in seen_list:
                seen_list.append(g)

        final_list = seen_list[-config.max_stored_guids:]

        logging.info("   Persisting %d GUIDs to %s (keeping last %d)", len(final_list), guid_file, config.max_stored_guids)
        try:
            with open(guid_file, "w", encoding="utf-8") as f:
                f.write("\n".join(final_list))
        except Exception:
            logging.error("⚠️ Failed to write scanned_guids.txt", exc_info=True)

    return added, exists, excluded
