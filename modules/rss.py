import os
import re
import time
import logging
import xml.etree.ElementTree as ET
from typing import List, Tuple, Optional, cast
import requests
import datetime
import threading

from modules.config import ConfigModel
from modules.radarr import add_movie
from modules.util import redact_url_query
from arrapi.exceptions import ArrException




def load_regex_patterns(config: ConfigModel) -> Tuple[Optional[re.Pattern], Optional[re.Pattern]]:
    include = "|".join(config.match_patterns or [])
    exclude = "|".join(config.not_match_patterns or [])
    logging.info(f"🚀 Loaded {len(config.match_patterns)} include patterns, "
                 f"{len(config.not_match_patterns)} exclude patterns")
    inc = re.compile(include, re.IGNORECASE) if include else None
    exc = re.compile(exclude, re.IGNORECASE) if exclude else None
    return inc, exc


def filter_title(title: str, inc: Optional[re.Pattern], exc: Optional[re.Pattern]) -> bool:
    if not title:
        return False
    if inc and not inc.search(title):
        return False
    if exc and exc.search(title):
        return False
    return True


def fetch_rss(url: str, attempts: int = 5, base_delay: int = 5) -> Optional[ET.Element]:
    for i in range(1, attempts + 1):
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            return ET.fromstring(r.content)
        except Exception as e:
            logging.error(f"Attempt {i}/{attempts} for {redact_url_query(url)} failed: {e}")
            if i < attempts:
                time.sleep(base_delay * (2 ** (i - 1)))
    logging.error(f"❌ Skipping RSS feed after {attempts} failed attempts: {url}")
    return None

def clean_imdb_id(raw_id: str) -> Optional[str]:
    if not raw_id:
        return None

    digits = re.sub(r"\D", "", raw_id)
    if not digits or digits in {"0", "0000000", "00000000", "00000001"}:
        return None

    digits = digits.lstrip("0")
    if not digits:
        return None

    # Cap to 8 digits max
    if len(digits) > 8:
        return None

    # Pad to at least 7 digits
    digits = digits.zfill(7)

    return f"tt{digits}"



def extract_imdb_id(item: ET.Element) -> Optional[str]:
    for attr in item.iter():
        if attr.tag.endswith("attr") and attr.get("name") == "imdb":
            return attr.get("value")
    return None

def run_rss_processing(
    config: ConfigModel,
    radarr,
    stop_event: threading.Event
) -> Tuple[int, int, int, int]:
    inc, exc = load_regex_patterns(config)

    guid_file = os.path.join(config.config_folder, "scanned_guids.txt")
    seen = set()
    if not config.debug_mode and os.path.exists(guid_file):
        seen = set(open(guid_file, "r", encoding="utf-8").read().splitlines())

    new_guids: List[str] = []
    batch: List[Tuple[str, str]] = []

    for feed in config.rss_feeds:
        name, url = feed.name, os.path.expandvars(feed.url)
        logging.info(f"📡 Fetching {name}")
        root = fetch_rss(url)
        if not root:
            continue  # ✅ Skip this feed if all retries failed

        items = root.findall("./channel/item")

        seen_count = sum(
            1 for itm in items
            if itm.find("guid") is not None
               and itm.find("guid").text.split("/")[-1] in seen
        )
        logging.info(f"✅ {name}: {len(items)} items, {seen_count} already seen")

        for itm in items:
            if stop_event.is_set():
                logging.info("🛑 stop_event set, aborting RSS processing early")
                return 0, 0, 0, 0

            guid_el = itm.find("guid")
            guid = guid_el.text.split("/")[-1] if guid_el is not None else None
            if not guid or (not config.debug_mode and guid in seen):
                continue

            title_el = itm.find("title")
            title = title_el.text or "Unknown Title"

            imdb_id = None
            for ns in ("newznab", "nntmux"):
                for attr in itm.findall(f"{ns}:attr", {
                    "newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/",
                    "nntmux": "https://nzbfinder.ws/rsshelp/"
                }):
                    if attr.get("name") == "imdb":
                        imdb_id = attr.get("value")
                        break
                if imdb_id:
                    break

            if filter_title(title, inc, exc):
                logging.info(f"✅ MATCHED: {title}")
                if imdb_id:
                    valid_id = clean_imdb_id(imdb_id)
                    if valid_id:
                        batch.append((valid_id, title))
                    else:
                        logging.warning(f"⚠️ Ignoring invalid IMDb ID '{imdb_id}' for {title}")
                else:
                    logging.warning(f"⚠️ Matched but missing IMDb for {title}")
            else:
                logging.info(f"❌ NOT MATCHED: {title}")

            new_guids.append(guid)

    # Deduplicate batch by imdb_id
    seen_ids = set()
    deduped: List[Tuple[str, str]] = []
    for imdb, tm in batch:
        if imdb not in seen_ids:
            seen_ids.add(imdb)
            deduped.append((imdb, tm))

    if not config.debug_mode:
        seen.update(new_guids)
        final = list(seen)[-config.max_stored_guids:]
        with open(guid_file, "w", encoding="utf-8") as f:
            f.write("\n".join(final))

    logging.info(f"⚙️ Batch to add ({len(deduped)} titles): {deduped}")

    added = exists = invalid = excluded = 0
    if deduped:
        radarr.respect_list_exclusions_when_adding()
        for imdb, title in deduped:
            try:
                result = radarr.add_multiple_movies([imdb], config.movie_folder, config.quality_profile)
                a, e, i, x = cast(Tuple[List, List, List, List], result)
                added += len(a)
                exists += len(e)
                invalid += len(i)
                excluded += len(x)

                if i:
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(config.invalid_movie_log_file, "a", encoding="utf-8") as f:
                        f.write(f"{ts} - {title} ({imdb})\n")

                logging.info(f"✅ {imdb} → Added:{a} Exists:{e} Invalid:{i} Excl:{x}")

            except ArrException as e:
                logging.error(f"❌ Radarr API error while adding {imdb}: {e}")
            except Exception as e:
                logging.exception(f"❌ Unexpected error adding {imdb}: {e}")

    logging.info(
        f"📈 Summary: Added={added}, Exists={exists}, Invalid={invalid}, "
        f"Excluded={excluded}, Total Sent={len(deduped)}"
    )

    return added, exists, invalid, excluded


    # dedupe by IMDb ID
    seen_ids = set()
    deduped: List[Tuple[str, str]] = []
    for imdb, tm in batch:
        if imdb not in seen_ids:
            seen_ids.add(imdb)
            deduped.append((imdb, tm))

    if not config.debug_mode:
        seen.update(new_guids)
        final = list(seen)[-config.max_stored_guids:]
        with open(guid_file, "w", encoding="utf-8") as f:
            f.write("\n".join(final))

    logging.info(f"⚙️ Batch to add ({len(deduped)} titles): {deduped}")

    # send to Radarr + summary counters
    total_invalid = 0
    total_excluded = 0
    total_exists = 0
    total_added = 0

    if deduped:
        radarr.respect_list_exclusions_when_adding()
        for imdb, tm in deduped:
            try:
                added, exists, invalid, excluded = radarr.add_multiple_movies(
                    [imdb],
                    config.movie_folder,
                    config.quality_profile
                )

                logging.info(
                    f"✅ {imdb} → Added:{added} Exists:{exists} Invalid:{invalid} Excl:{excluded}"
                )

                if invalid:
                    total_invalid += 1
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    with open(config.invalid_movie_log_file, "a", encoding="utf-8") as f:
                        f.write(f"{ts} - {tm} ({imdb})\n")

                total_added += len(added)
                total_exists += len(exists)
                total_excluded += len(excluded)

            except Exception as e:
                logging.error(f"❌ Error adding {imdb}: {e}")

    logging.info(
        f"📈 Summary: Added={total_added}, Exists={total_exists}, "
        f"Invalid={total_invalid}, Excluded={total_excluded}, "
        f"Total Sent={len(deduped)}"
    )
