# modules/radarr_pyarr.py

import logging
import email.utils
import datetime
from pyarr import RadarrAPI

log = logging.getLogger(__name__)


class RadarrProcessor:
    def __init__(self, url, api_key, threshold):
        self.api = RadarrAPI(url, api_key)
        self.threshold = threshold

    def process_release(self, title, imdb_id, nzb_url, rss_date):
        """
        Main logic: Check library -> Check Monitor -> Check Quality -> Push
        """
        log.debug(f"🔍 Analyzing: {title} ({imdb_id})")

        # 1. Parse Date
        try:
            publish_date = email.utils.parsedate_to_datetime(rss_date)
        except Exception:
            publish_date = datetime.datetime.now()

        # 2. Lookup (Targeted & Fast)
        try:
            results = self.api.lookup_movie(f"imdb:{imdb_id}")
        except Exception as e:
            log.error(f"❌ API Error looking up {imdb_id}: {e}")
            return "API_ERROR"

        if not results or not results[0].get("id"):
            # Movie not found in your library, skipping.
            return "NOT_IN_LIBRARY"

        movie = results[0]

        # 3. Auto-Monitor Fix
        if not movie.get("monitored"):
            log.info(f"🚩 '{movie['title']}' is unmonitored. Fixing...")
            movie['monitored'] = True
            try:
                self.api.upd_movie(movie)
            except Exception as e:
                log.warning(f"⚠️ Failed to update monitor status: {e}")

        # 4. Quality Evaluation
        if movie.get("hasFile"):
            try:
                files = self.api.get_movie_files_by_movie_id(movie['id'])
                if files:
                    current_q = files[0]["quality"]["quality"]["id"]
                    if current_q >= self.threshold:
                        return "QUALITY_MET"

                    log.info(f"⬆️ Upgrade needed: Current {current_q} < Threshold {self.threshold}")
            except Exception:
                pass  # If fetching files fails, assume we need to push just in case
        else:
            log.info(f"🆕 Missing file for '{movie['title']}'. Pushing...")

        # 5. Push Release
        return self.push_nzb(title, nzb_url, publish_date)

    def push_nzb(self, title, url, date_obj):
        try:
            res = self.api.post_release_push(
                title=title, download_url=url, protocol="usenet", publish_date=date_obj
            )

            # Radarr returns a list of results
            if isinstance(res, list) and len(res) > 0:
                if res[0].get('approved'):
                    log.info(f"✅ PUSH APPROVED: {title}")
                    return "PUSHED"
                else:
                    log.info(f"⛔ PUSH REJECTED: {res[0].get('rejections')}")
                    return "REJECTED"

            return "UNKNOWN_RESPONSE"
        except Exception as e:
            log.error(f"❌ Push Exception: {e}")
            return "ERROR"