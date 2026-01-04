# modules/radarr_pyarr.py

import logging
import email.utils
import datetime
import json
import time
from typing import Optional, Any, Dict, List, Tuple
from pyarr import RadarrAPI

log = logging.getLogger(__name__)


class RadarrProcessor:
    """
    Wrapper around pyarr.RadarrAPI with helper logic for processing NZB releases.
    Stores both the Radarr quality profile id and the custom upgrade threshold.
    """

    def _info(self, msg: str, *args, **kwargs) -> None:
        """
        Log an INFO message with consistent indentation so icons line up with root logs.
        Use this for all user-facing INFO messages that include icons.
        """
        try:
            log.info("    " + msg, *args, **kwargs)
        except Exception:
            # fallback to plain logging if formatting fails
            log.info(msg, *args, **kwargs)

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
                    # use helper so emoji lines align with root logs
                    self._info("🎬 Radarr Quality Profile: %s - %s", p.get("id"), p.get("name"))
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

    def _get_existing_file_score(self, movie_id: int) -> Optional[int]:
        """
        Try to fetch the movie's file entry and extract a best-effort score for
        custom formats / quality. Returns an int score if found, otherwise None.

        Checks common shapes:
          - file_entry['customFormatScore']
          - file_entry['customFormatScoreTotal']
          - file_entry['score']
          - file_entry['quality']['score']
          - file_entry.get('quality', {}).get('customFormatScore')
        """
        try:
            files: Optional[List[Dict]] = None
            try:
                if hasattr(self.api, "get_movie_files_by_movie_id"):
                    files = self.api.get_movie_files_by_movie_id(movie_id)
                elif hasattr(self.api, "get_movie_files"):
                    files = self.api.get_movie_files(movie_id)
                elif hasattr(self.api, "get_files"):
                    files = self.api.get_files(movie_id)
            except Exception:
                log.debug("Fetching movie files for score extraction failed", exc_info=True)
                files = None

            if not files:
                return None

            file_entry = files[0]
            # Try several common keys
            candidates = []
            try:
                if isinstance(file_entry, dict):
                    # direct keys
                    for k in ("customFormatScore", "customFormatScoreTotal", "score"):
                        v = file_entry.get(k)
                        if v is not None:
                            candidates.append(v)
                    # nested quality keys
                    q = file_entry.get("quality")
                    if isinstance(q, dict):
                        for k in ("score", "customFormatScore", "customFormatScoreTotal"):
                            v = q.get(k)
                            if v is not None:
                                candidates.append(v)
                    # some clients embed a 'quality' -> 'quality' dict
                    if isinstance(q, dict):
                        inner = q.get("quality")
                        if isinstance(inner, dict):
                            for k in ("score", "customFormatScore"):
                                v = inner.get(k)
                                if v is not None:
                                    candidates.append(v)
            except Exception:
                log.debug("Error while inspecting file entry for score", exc_info=True)

            # Normalize candidate to int if possible
            for c in candidates:
                try:
                    if isinstance(c, (int, float)):
                        return int(c)
                    if isinstance(c, str) and c.isdigit():
                        return int(c)
                    # sometimes it's like "1.0" or "1/10" — try float then int
                    try:
                        f = float(str(c))
                        return int(f)
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            log.debug("Unexpected error extracting existing file score", exc_info=True)
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
        Main logic: Lookup movie by IMDB id, ensure monitored, check current quality, and push NZB if needed.
        Behavior:
          - If movie not in Radarr, attempt to add it (returns 'ADDED' on success).
          - If movie exists, ensure monitored, check current quality and push if needed.
          - If push is rejected (e.g., custom formats), attempt fallback add/search.
        Returns one of: PUSHED, QUALITY_MET, NOT_IN_LIBRARY, API_ERROR, REJECTED, ERROR, UNKNOWN_RESPONSE, ADDED
        """
        log.debug("Analyzing: %s (%s)", title, imdb_id)

        publish_date = self._parse_pubdate(rss_date)

        # Lookup (may return lookup/search result or library record)
        try:
            movie = self._lookup_movie(imdb_id, title=title)
        except Exception as e:
            log.error("API Error during lookup for %s: %s", imdb_id, e, exc_info=True)
            return "API_ERROR"

        # Try to resolve to the authoritative library record (so we can check hasFile)
        try:
            # If lookup returned an entry with an id, try to fetch the full library record
            if movie and movie.get("id"):
                try:
                    if hasattr(self.api, "get_movie"):
                        try:
                            full = self.api.get_movie(movie["id"])
                        except Exception:
                            try:
                                full = self.api.get_movie(movie.get("id"))
                            except Exception:
                                full = None
                        if full:
                            movie = full
                except Exception:
                    log.debug("Failed to fetch full library record for id %s", movie.get("id"), exc_info=True)

            # If lookup returned a remote result without id, check library by imdb
            if (not movie or not movie.get("id")) and hasattr(self.api, "get_movie_by_imdb"):
                try:
                    m = self.api.get_movie_by_imdb(imdb_id)
                    if m and m.get("id"):
                        movie = m
                except Exception:
                    log.debug("get_movie_by_imdb check failed for %s", imdb_id, exc_info=True)
        except Exception:
            log.debug("Post-lookup resolution failed for %s", imdb_id, exc_info=True)

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

            # Normalize push_result to (status, details)
            if isinstance(push_result, tuple):
                status, details = push_result
            else:
                status, details = push_result, None

            if status == "PUSHED":
                return "PUSHED"
            if status == "REJECTED":
                # Single INFO log here with the rejection details (push_nzb logged details at DEBUG)
                self._info("⛔ PUSH REJECTED: %s", details)
                return "REJECTED"

            # If push returned ERROR/UNKNOWN_RESPONSE, still report that the movie was added.
            log.info("Movie added but push returned %s; returning ADDED", status)
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
                        self._info("ℹ️ QUALITY MET: current %s >= threshold %s", current_q_val, self.threshold)
                        return "QUALITY_MET"
                    # If RSS release is better than current and below threshold, attempt push
                    self._info("⬆️ Upgrade needed: Current %s < Threshold %s; attempting push", current_q_val, self.threshold)
                else:
                    self._info("🆕 Missing file for '%s'. Pushing...", movie.get("title", "unknown"))
            else:
                self._info("🆕 Missing file for '%s'. Pushing...", movie.get("title", "unknown"))
        except Exception:
            log.exception("Error while evaluating current quality; proceeding to push attempt.")

        # Push release
        push_result = self.push_nzb(title, nzb_url, publish_date)

        # Normalize push_result to (status, details)
        if isinstance(push_result, tuple):
            status, details = push_result
        else:
            status, details = push_result, None

        # If push rejected (often due to custom formats), attempt fallback add/search
        if status == "REJECTED":
            try:
                # Single INFO log here with the rejection details (push_nzb logged details at DEBUG)
                self._info("⛔ PUSH REJECTED: %s", details)
                log.debug("Inspecting rejection for %s (%s)...", title, imdb_id)

                # Re-fetch authoritative movie record to see if it has a file now
                movie_after = None
                try:
                    movie_after = self._lookup_movie(imdb_id, title=title)
                    if movie_after and movie_after.get("id") and hasattr(self.api, "get_movie"):
                        try:
                            full = self.api.get_movie(movie_after["id"])
                            if full:
                                movie_after = full
                        except Exception:
                            pass
                except Exception:
                    movie_after = None

                # If movie exists and hasFile True, the rejection likely means existing file meets cutoff.
                if movie_after and movie_after.get("hasFile"):
                    # Normalize details into a single lowercase string for robust substring checks
                    reason_text = ""
                    try:
                        if details:
                            if isinstance(details, (list, tuple)):
                                reason_text = " ".join(str(x) for x in details)
                            else:
                                reason_text = str(details)
                        reason_lc = reason_text.lower()
                    except Exception:
                        reason_lc = ""

                    # Common phrases that indicate the rejection already explains "existing file" reasons
                    existing_phrases = [
                        "existing file meets cutoff",
                        "existing file on disk",
                        "existing file",
                        "meets cutoff",
                        "equal or higher",
                        "custom format",
                        "custom formats"
                    ]

                    # Try to fetch the existing file's score and include it in the log
                    existing_score = None
                    try:
                        existing_score = self._get_existing_file_score(movie_after.get("id"))
                    except Exception:
                        existing_score = None

                    # If any phrase appears in the rejection details, keep the inspection message quiet (DEBUG).
                    if any(p in reason_lc for p in existing_phrases):
                        log.debug(
                            "Existing file present for %s (id=%s); skipping fallback add/search. Reason: %s; existing_score=%s",
                            title, movie_after.get("id"), reason_text, existing_score
                        )
                    else:
                        # Otherwise log at INFO so operators still see the reason
                        self._info(
                            "Existing file present for %s (id=%s); skipping fallback add/search. Reason: %s; existing_score=%s",
                            title, movie_after.get("id"), reason_text, existing_score
                        )
                    return "QUALITY_MET"

                # Otherwise, attempt fallback add/search (only when movie missing or no file)
                self._info("Attempting fallback: add/search movie in Radarr for %s (%s).", title, imdb_id)
                added_ok = self._ensure_movie_in_radarr(imdb_id, title=title, force_search=True)
                if added_ok:
                    self._info("Fallback add/search triggered for %s (%s).", title, imdb_id)
                    return "ADDED"
                else:
                    log.warning("Fallback add/search failed for %s (%s).", title, imdb_id)
                    return "NOT_IN_LIBRARY"

            except Exception:
                log.exception("Fallback after push rejection failed for %s (%s).", title, imdb_id)
                return "ERROR"

        # If pushed successfully
        if status == "PUSHED":
            return "PUSHED"

        # Unknown or error states
        if status == "UNKNOWN_RESPONSE":
            return "UNKNOWN_RESPONSE"
        if status == "ERROR":
            return "ERROR"

        return "ERROR"

    def push_nzb(self, title: str, url: str, date_obj: datetime.datetime) -> Tuple[str, Optional[Any]]:
        """
        Push an NZB to Radarr using the client's push endpoint.

        Returns a tuple: (status, details)
          - ("PUSHED", None) on success
          - ("REJECTED", rejection_details) when Radarr rejects the push
          - ("UNKNOWN_RESPONSE", raw_response) for unexpected shapes
          - ("ERROR", error_info) on exceptions
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
                        return ("ERROR", str(e))

            # Handle list response
            if isinstance(res, list) and len(res) > 0:
                first = res[0]
                if first.get("approved"):
                    # use helper so emoji lines align with root logs
                    self._info("✅ PUSH APPROVED: %s", title)
                    return ("PUSHED", None)
                else:
                    # keep detailed rejection info at DEBUG; return structured tuple
                    log.debug("Push rejected details: %s", first.get("rejections"))
                    return ("REJECTED", first.get("rejections"))

            # Handle dict response
            if isinstance(res, dict):
                if res.get("approved"):
                    self._info("✅ PUSH APPROVED: %s", title)
                    return ("PUSHED", None)
                if res.get("rejections"):
                    log.debug("Push rejected details: %s", res.get("rejections"))
                    return ("REJECTED", res.get("rejections"))

            log.debug("Unknown push response shape: %s", type(res))
            return ("UNKNOWN_RESPONSE", res)
        except Exception as e:
            log.exception("Push Exception: %s", e, exc_info=True)
            return ("ERROR", str(e))
