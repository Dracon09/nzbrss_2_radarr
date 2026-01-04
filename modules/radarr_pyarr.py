# modules/radarr_pyarr.py

import logging
import email.utils
import datetime
import json
from typing import Optional, Any, Dict, List
from pyarr import RadarrAPI

log = logging.getLogger(__name__)


class RadarrProcessor:
    """
    Wrapper around pyarr.RadarrAPI with helper logic for processing NZB releases.
    Stores both the Radarr quality profile id and the custom upgrade threshold.
    """

    def __init__(self, url: str, api_key: str, quality_profile: int, threshold: int, root_folder: str):
        self.api = RadarrAPI(url, api_key)
        self.base_url = url.rstrip("/") if url else ""
        self.api_key = api_key
        self.quality_profile = quality_profile
        self.threshold = threshold
        # Use the configured root folder from config.yaml
        self.root_folder = root_folder or ""

        # Try to list quality profiles for visibility (non-fatal)
        try:
            profiles = None
            if hasattr(self.api, "get_quality_profiles"):
                profiles = self.api.get_quality_profiles()
            elif hasattr(self.api, "get_profiles"):
                profiles = self.api.get_profiles()
            if profiles:
                for p in profiles:
                    log.info("🎬 Radarr Quality Profile: %s - %s", p.get("id"), p.get("name"))
        except Exception as e:
            log.debug("Could not fetch Radarr quality profiles: %s", e, exc_info=True)

    def _safe_get_quality_id(self, file_entry: dict) -> Optional[int]:
        """
        Safely extract the quality id from a movie file entry returned by the API.
        Different API versions/clients may nest this differently, so check common shapes.
        """
        try:
            q = file_entry.get("quality")
            if isinstance(q, dict):
                inner = q.get("quality")
                if isinstance(inner, dict) and "id" in inner:
                    return int(inner["id"])
                if "id" in q:
                    return int(q["id"])
        except Exception:
            log.debug("Could not parse quality id from file entry", exc_info=True)
        return None

    def _parse_pubdate(self, rss_date: Optional[str]) -> datetime.datetime:
        """
        Parse RSS pubDate into a datetime. If parsing fails, return now().
        """
        if not rss_date:
            return datetime.datetime.now(datetime.timezone.utc)
        try:
            dt = email.utils.parsedate_to_datetime(rss_date)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            log.debug("Failed to parse rss pubDate, using now()", exc_info=True)
            return datetime.datetime.now(datetime.timezone.utc)

    def _lookup_movie(self, imdb_id: str, title: Optional[str] = None) -> Optional[Dict]:
        """
        Try multiple lookup strategies against the Radarr API to find a movie record.
        Returns the movie dict if found, otherwise None.
        """
        imdb_key = imdb_id
        try:
            if hasattr(self.api, "get_movie_by_imdb"):
                try:
                    m = self.api.get_movie_by_imdb(imdb_id)
                    if m:
                        return m
                except Exception:
                    log.debug("get_movie_by_imdb failed for %s", imdb_id, exc_info=True)

            if hasattr(self.api, "lookup_movie"):
                try:
                    key = imdb_key if imdb_key.startswith("imdb:") else f"imdb:{imdb_key}"
                    res = self.api.lookup_movie(key)
                    if isinstance(res, list) and res:
                        return res[0]
                    if isinstance(res, dict) and res.get("id"):
                        return res
                except Exception:
                    log.debug("lookup_movie failed for %s", imdb_id, exc_info=True)
        except Exception:
            log.debug("Error while attempting imdb-based lookups", exc_info=True)

        try:
            if hasattr(self.api, "get_movie"):
                try:
                    m = self.api.get_movie(imdbId=imdb_id)
                    if m:
                        return m
                except TypeError:
                    try:
                        m = self.api.get_movie(imdb_id)
                        if m:
                            return m
                    except Exception:
                        log.debug("get_movie positional attempt failed for %s", imdb_id, exc_info=True)
                except Exception:
                    log.debug("get_movie(imdbId=...) failed for %s", imdb_id, exc_info=True)
        except Exception:
            log.debug("Error while attempting get_movie", exc_info=True)

        if title:
            try:
                if hasattr(self.api, "get_movies"):
                    try:
                        movies = self.api.get_movies()
                        for m in movies:
                            if m and m.get("title") and title.lower() in m.get("title").lower():
                                return m
                    except Exception:
                        log.debug("get_movies search failed", exc_info=True)
                if hasattr(self.api, "get_movie_by_title"):
                    try:
                        m = self.api.get_movie_by_title(title)
                        if m:
                            return m
                    except Exception:
                        log.debug("get_movie_by_title failed", exc_info=True)
            except Exception:
                log.debug("Title-based lookup failed", exc_info=True)

        return None

    def _ensure_movie_in_radarr(self, imdb_id: str, title: Optional[str] = None, force_search: bool = False) -> bool:
        """
        Ensure the movie exists in Radarr. Try multiple client methods to add it,
        then verify by re-running a lookup. Returns True only if the movie is
        confirmed present in Radarr after the add/search attempt.
        """
        try:
            imdb_key = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"

            # Try to obtain lookup metadata (tmdbId etc.) to build a proper payload
            lookup_meta = None
            try:
                if hasattr(self.api, "lookup_movie"):
                    try:
                        res = self.api.lookup_movie(f"imdb:{imdb_key}")
                        if isinstance(res, list) and res:
                            lookup_meta = res[0]
                        elif isinstance(res, dict) and res.get("tmdbId"):
                            lookup_meta = res
                    except Exception:
                        log.debug("api.lookup_movie failed", exc_info=True)

                if not lookup_meta and hasattr(self.api, "client") and hasattr(self.api.client, "get"):
                    try:
                        resp = self.api.client.get(f"/api/movie/lookup?term=imdb:{imdb_key}")
                        if resp is not None:
                            lookup_meta = resp[0] if isinstance(resp, list) and len(resp) > 0 else resp
                    except Exception:
                        log.debug("raw client lookup failed", exc_info=True)
            except Exception:
                log.debug("lookup attempts raised", exc_info=True)

            # Build add payload
            payload = {
                "title": title or "",
                "qualityProfileId": int(self.quality_profile),
                "rootFolderPath": self.root_folder,
                "monitored": True,
                "addOptions": {"searchForMovie": bool(force_search)},
                "images": []
            }

            if lookup_meta:
                tmdb = lookup_meta.get("tmdbId")
                if tmdb:
                    payload["tmdbId"] = int(tmdb)
                if not payload["title"] and lookup_meta.get("title"):
                    payload["title"] = lookup_meta.get("title")

            if "tmdbId" not in payload:
                payload["imdbId"] = imdb_key

            # Pretty-print payload for debug logs
            try:
                pretty = json.dumps(payload, indent=2, sort_keys=True)
                log.debug("Prepared Radarr add payload:\n%s", pretty)
            except Exception:
                log.debug("Add payload prepared: %s", payload)

            # Preflight check for required fields
            required = ["qualityProfileId", "rootFolderPath"]
            missing = [k for k in required if k not in payload or payload.get(k) in (None, "")]
            if missing:
                log.warning("Add payload missing required fields %s for imdb %s", missing, imdb_key)
            else:
                log.debug("Add payload has required fields for imdb %s", imdb_key)

            # Try pyarr convenience add methods, handle add_movie_by_imdb specially
            # so we can pass explicit parameters Radarr expects.
            # We still attempt other add helpers for compatibility.
            # Do not assume success from return value; verify below.
            try:
                if hasattr(self.api, "add_movie_by_imdb"):
                    try:
                        # call with explicit args Radarr expects
                        res = self.api.add_movie_by_imdb(
                            imdb_key,
                            qualityProfileId=int(self.quality_profile),
                            rootFolderPath=self.root_folder,
                            monitored=True,
                            addOptions={"searchForMovie": bool(force_search)}
                        )
                        log.debug("add_movie_by_imdb returned: %s", res)
                    except TypeError:
                        # fallback if signature differs
                        try:
                            res = self.api.add_movie_by_imdb(imdb_key)
                            log.debug("add_movie_by_imdb (simple) returned: %s", res)
                        except Exception:
                            log.debug("add_movie_by_imdb simple call failed", exc_info=True)
                    except Exception:
                        log.debug("add_movie_by_imdb call failed", exc_info=True)
            except Exception:
                log.debug("add_movie_by_imdb path error", exc_info=True)

            # Try other convenience methods (payload or imdb key)
            for method_name in ("add_movie", "addMovie", "add"):
                if hasattr(self.api, method_name):
                    try:
                        method = getattr(self.api, method_name)
                        try:
                            res = method(payload)
                        except TypeError:
                            try:
                                res = method(imdb_key)
                            except Exception:
                                res = None
                        log.debug("%s returned: %s", method_name, res)
                    except Exception:
                        log.debug("%s failed", method_name, exc_info=True)

            # Fallback: direct POST to /api/movie using requests so we always capture Radarr's response
            try:
                if ("tmdbId" in payload or "imdbId" in payload) and self.base_url and self.api_key:
                    try:
                        import requests
                        url = f"{self.base_url}/api/v3/movie"
                        headers = {"X-Api-Key": self.api_key, "Content-Type": "application/json"}
                        log.debug("Attempting direct POST to Radarr: %s", url)
                        # Log the payload being sent (already pretty-printed above)
                        resp = requests.post(url, json=payload, headers=headers, timeout=15)
                        # Log status and body for diagnostics
                        log.debug("Direct /api/movie POST status: %s", resp.status_code)
                        try:
                            j = resp.json()
                            log.debug("Direct /api/movie POST json: %s", j)
                        except Exception:
                            log.debug("Direct /api/movie POST text: %s", resp.text)
                    except Exception:
                        log.exception("Direct requests.post to /api/movie failed", exc_info=True)
            except Exception:
                log.debug("Raw client add attempt failed", exc_info=True)

            # Verification: re-run lookup a few times (Radarr may take a moment)
            import time
            for attempt in range(8):
                try:
                    found = self._lookup_movie(imdb_key, title=title)
                    if found and found.get("id"):
                        log.debug("Verified movie present in Radarr after add: %s", found.get("id"))
                        return True
                except Exception:
                    log.debug("Verification lookup attempt %d failed", attempt + 1, exc_info=True)
                time.sleep(1.0)

        except Exception:
            log.exception("Unexpected error while trying to add movie %s", imdb_id)

        return False

    def process_release(self, title: str, imdb_id: str, nzb_url: str, rss_date: Optional[str]) -> str:
        """
        Main logic: Lookup movie by IMDB id, ensure monitored, check quality, and push NZB if needed.
        Behavior:
          - If movie not in Radarr, attempt to add it (returns 'ADDED' on success).
          - If movie exists, ensure monitored, check current quality and push if needed.
          - If push is rejected (e.g., custom formats), attempt fallback add/search.
        Returns one of: PUSHED, QUALITY_MET, NOT_IN_LIBRARY, API_ERROR, REJECTED, ERROR, UNKNOWN_RESPONSE, ADDED
        """
        log.debug("Analyzing: %s (%s)", title, imdb_id)

        publish_date = self._parse_pubdate(rss_date)

        # Lookup
        try:
            movie = self._lookup_movie(imdb_id, title=title)
        except Exception as e:
            log.error("API Error during lookup for %s: %s", imdb_id, e, exc_info=True)
            return "API_ERROR"

        # If not found, attempt to add
        if not movie or not movie.get("id"):
            log.info("Movie %s (%s) not found in Radarr. Attempting to add.", title, imdb_id)
            added_ok = self._ensure_movie_in_radarr(imdb_id, title=title, force_search=True)
            if not added_ok:
                log.warning("Failed to add movie %s (%s) to Radarr.", title, imdb_id)
                return "NOT_IN_LIBRARY"

            # Movie was added (or search triggered). Attempt to push the NZB you already have.
            log.info("Added movie %s (%s) to Radarr. Attempting to push NZB...", title, imdb_id)
            push_result = self.push_nzb(title, nzb_url, publish_date)

            # If push succeeded or was rejected, return that result; otherwise return ADDED as fallback.
            if push_result == "PUSHED":
                return "PUSHED"
            if push_result == "REJECTED":
                # If Radarr rejected the push, we already added the movie; return REJECTED so caller can handle it.
                return "REJECTED"

            # If push returned ERROR/UNKNOWN_RESPONSE, still report that the movie was added.
            log.info("Movie added but push returned %s; returning ADDED", push_result)
            return "ADDED"

        # Ensure monitored
        try:
            if not movie.get("monitored"):
                log.info("'%s' is unmonitored. Fixing...", movie.get("title", "unknown"))
                movie["monitored"] = True
                try:
                    if hasattr(self.api, "upd_movie"):
                        self.api.upd_movie(movie)
                    elif hasattr(self.api, "update_movie"):
                        self.api.update_movie(movie)
                    elif hasattr(self.api, "updateMovie"):
                        self.api.updateMovie(movie)
                    else:
                        log.warning("No known update method on RadarrAPI client to set monitored flag.")
                except Exception:
                    log.warning("Failed to update monitor status via API", exc_info=True)
        except Exception:
            log.warning("Failed to update monitor status for %s", movie.get("title", imdb_id), exc_info=True)

        # Quality evaluation and upgrade decision
        try:
            if movie.get("hasFile"):
                files: Optional[List[Dict]] = None
                try:
                    if hasattr(self.api, "get_movie_files_by_movie_id"):
                        files = self.api.get_movie_files_by_movie_id(movie["id"])
                    elif hasattr(self.api, "get_movie_files"):
                        files = self.api.get_movie_files(movie["id"])
                    elif hasattr(self.api, "get_files"):
                        files = self.api.get_files(movie["id"])
                except Exception:
                    log.debug("Fetching movie files failed, will attempt push", exc_info=True)
                    files = None

                if files:
                    file_entry = files[0]
                    current_q = self._safe_get_quality_id(file_entry)
                    # If current quality is None treat as 0
                    current_q_val = int(current_q) if current_q is not None else 0
                    # If current quality meets or exceeds threshold, skip
                    if current_q_val >= int(self.threshold):
                        log.info("QUALITY_MET: current %s >= threshold %s", current_q_val, self.threshold)
                        return "QUALITY_MET"
                    # If RSS release is better than current and below threshold, attempt push
                    log.info("⬆️ Upgrade needed: Current %s < Threshold %s; attempting push", current_q_val, self.threshold)
                else:
                    log.info("🆕 Missing file for '%s'. Pushing...", movie.get("title", "unknown"))
            else:
                log.info("🆕 Missing file for '%s'. Pushing...", movie.get("title", "unknown"))
        except Exception:
            log.exception("Error while evaluating current quality; proceeding to push attempt.")

        # Push release
        push_result = self.push_nzb(title, nzb_url, publish_date)

        # If push rejected (often due to custom formats), attempt fallback add/search
        if push_result == "REJECTED":
            try:
                log.info("Push rejected for %s (%s). Attempting fallback: add/search movie in Radarr.", title, imdb_id)
                added_ok = self._ensure_movie_in_radarr(imdb_id, title=title, force_search=True)
                if added_ok:
                    log.info("Fallback add/search triggered for %s (%s).", title, imdb_id)
                    return "ADDED"
                else:
                    log.warning("Fallback add/search failed for %s (%s).", title, imdb_id)
            except Exception:
                log.exception("Fallback after push rejection failed for %s (%s).", title, imdb_id)

        return push_result

    def push_nzb(self, title: str, url: str, date_obj: datetime.datetime) -> str:
        """
        Push an NZB to Radarr using the client's push endpoint.
        """
        try:
            res: Any = None
            # Try common push method names
            try:
                res = self.api.post_release_push(
                    title=title, download_url=url, protocol="usenet", publish_date=date_obj
                )
            except Exception:
                try:
                    res = self.api.release_push(title=title, download_url=url, protocol="usenet", publish_date=date_obj)
                except Exception:
                    try:
                        res = self.api.push_release(title=title, download_url=url, protocol="usenet", publish_date=date_obj)
                    except Exception as e:
                        log.error("No push method available on RadarrAPI client: %s", e, exc_info=True)
                        return "ERROR"

            # Handle list response
            if isinstance(res, list) and len(res) > 0:
                first = res[0]
                if first.get("approved"):
                    log.info("✅ PUSH APPROVED: %s", title)
                    return "PUSHED"
                else:
                    log.info("⛔ PUSH REJECTED: %s", first.get("rejections"))
                    return "REJECTED"

            # Handle dict response
            if isinstance(res, dict):
                if res.get("approved"):
                    log.info("✅ PUSH APPROVED: %s", title)
                    return "PUSHED"
                if res.get("rejections"):
                    log.info("⛔ PUSH REJECTED: %s", res.get("rejections"))
                    return "REJECTED"

            log.warning("Unknown push response shape: %s", type(res))
            return "UNKNOWN_RESPONSE"
        except Exception as e:
            log.error("Push Exception: %s", e, exc_info=True)
            return "ERROR"
