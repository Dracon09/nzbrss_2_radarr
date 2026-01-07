# modules/radarr_pyarr.py
import logging
import time
import json
import re
from typing import Optional, Tuple, Any, Dict
from urllib.parse import urljoin

import requests

log = logging.getLogger(__name__)


class RadarrProcessor:
    """
    Lightweight Radarr helper used by the RSS pipeline.
    - Prefers direct HTTP calls to Radarr's API for predictable payload shapes.
    - Will attempt to use a pyarr-like client if provided on self.api, but falls back to requests.
    """

    def __init__(
        self,
        url: str,
        api_key: str,
        quality_profile: int = 1,
        threshold: int = 0,
        root_folder: str = "/",
        tmdb_api_key: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ):
        self.base_url = url.rstrip("/") if url else None
        self.api_key = api_key
        self.quality_profile = int(quality_profile)
        self.threshold = int(threshold)
        self.root_folder = root_folder
        self.tmdb_api_key = tmdb_api_key

        # Optional wrapper client (pyarr) may be attached externally; keep compatibility
        self.api = None

        # HTTP session with a small retry/backoff could be provided; otherwise create one
        self._session = session or requests.Session()
        self._session.headers.update({"X-Api-Key": self.api_key, "Content-Type": "application/json"})

        # Simple on-disk cache for imdb->tmdb mappings (path relative to config folder)
        self._cache = {}
        self._cache_path = "cache_imdb_to_tmdb.json"
        self._load_cache()

    # -------------------------
    # Cache helpers
    # -------------------------
    def _load_cache(self):
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                self._cache = json.load(f)
        except Exception:
            self._cache = {}

    def _save_cache(self):
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
        except Exception:
            log.debug("Failed to save imdb->tmdb cache", exc_info=True)

    # -------------------------
    # Low-level HTTP helpers
    # -------------------------
    def _get(self, path: str, params: Optional[Dict[str, Any]] = None, timeout: int = 15,
             retries: int = 3) -> requests.Response:
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                return self._session.get(url, params=params, timeout=timeout)
            except requests.exceptions.ReadTimeout as e:
                last_exc = e
                log.debug("GET %s timed out (attempt %s/%s)", path, attempt, retries)
                time.sleep(0.5 * attempt)
            except Exception as e:
                last_exc = e
                log.debug("GET %s failed (attempt %s/%s): %s", path, attempt, retries, e)
                time.sleep(0.5 * attempt)
        raise last_exc

    def _post(self, path: str, json_payload: Dict[str, Any], timeout: int = 15) -> requests.Response:
        if not self.base_url:
            raise RuntimeError("Radarr base_url not configured")
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        return self._session.post(url, json=json_payload, timeout=timeout)

    # -------------------------
    # Lookup & existence helpers
    # -------------------------
    def _movie_exists_locally(self, imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """
        Targeted check for a local Radarr movie. Avoids fetching the entire library.
        """
        try:
            # Prefer lookup by tmdbId
            if tmdb_id:
                term = f"tmdb:{int(tmdb_id)}"
                log.debug("Targeted lookup by term=%s", term)
                resp = self._get("/api/v3/movie/lookup", params={"term": term}, timeout=15)
                if resp.ok:
                    candidates = resp.json() or []
                    if candidates:
                        cand = candidates[0]
                        radarr_id = cand.get("id")
                        if radarr_id:
                            r2 = self._get(f"/api/v3/movie/{radarr_id}", timeout=15)
                            if r2.ok:
                                return r2.json()
                        return cand

            # Fallback: lookup by imdbId
            if imdb_id:
                imdb_key = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
                term = f"imdb:{imdb_key}"
                log.debug("Targeted lookup by term=%s", term)
                resp = self._get("/api/v3/movie/lookup", params={"term": term}, timeout=15)
                if resp.ok:
                    candidates = resp.json() or []
                    if candidates:
                        cand = candidates[0]
                        radarr_id = cand.get("id")
                        if radarr_id:
                            r2 = self._get(f"/api/v3/movie/{radarr_id}", timeout=15)
                            if r2.ok:
                                return r2.json()
                        return cand

        except requests.exceptions.ReadTimeout:
            log.debug("Radarr targeted lookup timed out", exc_info=True)
        except Exception:
            log.debug("Local movie check failed", exc_info=True)

        return None

    def _lookup_movie_candidates(self, term: str) -> Optional[Any]:
        """
        Ask Radarr's external lookup providers for candidates.
        term examples: "imdb:tt1234567" or "tmdb:12345"
        """
        try:
            resp = self._get("/api/v3/movie/lookup", params={"term": term})
            if resp.ok:
                return resp.json()
        except Exception:
            log.debug("Radarr lookup failed for term=%s", term, exc_info=True)
        return None

    # -------------------------
    # Resolve tmdb from imdb
    # -------------------------
    def _resolve_tmdb_from_imdb(self, imdb_id: str, force_refresh: bool = False) -> Tuple[Optional[int], str]:
        """
        Resolve a tmdbId for a given imdbId using:
          1) cache (unless force_refresh=True)
          2) local Radarr DB
          3) Radarr external lookup
          4) TMDb API fallback (requires tmdb_api_key)
        Returns (tmdb_id or None, source)
        """
        if not imdb_id:
            return None, "no_imdb"

        imdb_key = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"

        # Cache (skip when force_refresh requested)
        if not force_refresh and imdb_key in self._cache:
            return self._cache[imdb_key], "cache"

        # 1) Local DB
        try:
            local = self._movie_exists_locally(imdb_id=imdb_key)
            if local and local.get("tmdbId"):
                tmdb = local.get("tmdbId")
                self._cache[imdb_key] = tmdb
                self._save_cache()
                return tmdb, "local"
        except Exception:
            log.debug("Local lookup error", exc_info=True)

        # 2) Radarr external lookup
        try:
            candidates = self._lookup_movie_candidates(f"imdb:{imdb_key}")
            if candidates:
                tmdb = candidates[0].get("tmdbId")
                if tmdb:
                    self._cache[imdb_key] = tmdb
                    self._save_cache()
                    return tmdb, "radarr_lookup"
        except Exception:
            log.debug("Radarr external lookup error", exc_info=True)

        # 3) TMDb API fallback
        if self.tmdb_api_key:
            try:
                log.debug("Attempting TMDb fallback for %s; tmdb_api_key present=%s", imdb_key, bool(self.tmdb_api_key))

                r = requests.get(
                    f"https://api.themoviedb.org/3/find/{imdb_key}",
                    params={"external_source": "imdb_id", "api_key": self.tmdb_api_key},
                    timeout=10
                )

                # log status and a short preview of the body for debugging
                body_preview = (r.text[:1000] + '...') if r.text and len(r.text) > 1000 else r.text
                log.debug("TMDb API response status=%s body_preview=%s", r.status_code, body_preview)

                if r.ok:
                    jr = r.json()
                    movie_results = jr.get("movie_results") or []
                    if movie_results:
                        tmdb = int(movie_results[0].get("id"))
                        self._cache[imdb_key] = tmdb
                        self._save_cache()
                        log.info("Resolved tmdb from TMDb API: %s -> %s", imdb_key, tmdb)
                        return tmdb, "tmdb_api"
                    log.debug("TMDb API returned no movie_results for %s", imdb_key)
                else:
                    log.warning("TMDb API returned non-OK status %s for %s", r.status_code, imdb_key)
            except Exception:
                log.exception("TMDb API call failed for %s", imdb_key)

        return None, "not_found"

    # -------------------------
    # Add helper: add movie and return Radarr id
    # -------------------------
    def _add_movie_and_get_id(self, imdb_id: Optional[str], tmdb_id: Optional[int], title: Optional[str]) -> Optional[int]:
        """
        Ensure movie exists in Radarr and return the Radarr movie id (int) if available.
        Handles 201 Created, MovieExistsValidator (400) and targeted lookup fallback.
        """
        payload = {
            "title": title or "",
            "qualityProfileId": int(self.quality_profile),
            "rootFolderPath": self.root_folder,
            "monitored": True,
            "addOptions": {"searchForMovie": True},
            "images": []
        }
        if tmdb_id:
            payload["tmdbId"] = int(tmdb_id)
        # Use string 'tt...' form for imdbId when adding (Radarr expects string here)
        if imdb_id:
            imdb_key = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"
            payload["imdbId"] = imdb_key

        # Try wrapper client first if available
        try:
            if self.api:
                for method_name in ("add_movie_by_imdb", "add_movie", "add"):
                    if hasattr(self.api, method_name):
                        method = getattr(self.api, method_name)
                        try:
                            try:
                                res = method(payload)
                            except TypeError:
                                res = method(tmdb_id if tmdb_id else imdb_id)
                            log.debug("Radarr client %s returned: %s", method_name, res)
                            if isinstance(res, dict) and res.get("id"):
                                return int(res.get("id"))
                            break
                        except Exception:
                            log.debug("Radarr client method %s failed", method_name, exc_info=True)
        except Exception:
            log.debug("Radarr client add attempts raised", exc_info=True)

        # Direct POST fallback
        try:
            resp = self._post("/api/v3/movie", payload)
        except Exception:
            log.exception("Direct POST to /api/v3/movie failed", exc_info=True)
            return None

        # Parse response
        try:
            body = resp.json() if resp.text else None
        except Exception:
            body = resp.text

        log.debug("Direct /api/v3/movie POST status=%s body=%s", resp.status_code, body)

        # 201 Created -> extract id from Location or body
        if resp.status_code == 201:
            loc = resp.headers.get("Location")
            if loc:
                try:
                    radarr_id = int(loc.rstrip("/").split("/")[-1])
                    return radarr_id
                except Exception:
                    log.debug("Failed to parse Location header for id", exc_info=True)
            try:
                if isinstance(body, dict) and body.get("id"):
                    return int(body.get("id"))
            except Exception:
                log.debug("Failed to parse body for id", exc_info=True)

        # 400 MovieExistsValidator -> targeted lookup to find id
        if resp.status_code == 400 and isinstance(body, list):
            for err in body:
                if err.get("errorCode") == "MovieExistsValidator":
                    local = self._movie_exists_locally(imdb_id=imdb_id, tmdb_id=tmdb_id)
                    if local and local.get("id"):
                        return int(local.get("id"))
                    return None

        # As a last resort, try targeted lookup once
        local = self._movie_exists_locally(imdb_id=imdb_id, tmdb_id=tmdb_id)
        if local and local.get("id"):
            return int(local.get("id"))

        return None

    # -------------------------
    # Add / ensure movie exists
    # -------------------------
    def _ensure_movie_in_radarr(self, imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None,
                                title: Optional[str] = None, force_search: bool = False, force_refresh: bool = False) -> bool:
        """
        Ensure a movie exists in Radarr. Requires a valid tmdbId to add.
        Returns True if the movie is present after the call, False otherwise.

        force_refresh: if True, skip the local cache when resolving tmdb from imdb.
        """
        imdb_key = None
        if imdb_id:
            imdb_key = imdb_id if imdb_id.startswith("tt") else f"tt{imdb_id}"

        # If we don't have a tmdb_id yet, try to resolve it now (local -> radarr lookup -> tmdb API)
        if not tmdb_id and imdb_key:
            try:
                resolved_tmdb, src = self._resolve_tmdb_from_imdb(imdb_key, force_refresh=force_refresh)
                log.debug("Resolved tmdb for %s -> %s (source=%s)", imdb_key, resolved_tmdb, src)
                if resolved_tmdb:
                    tmdb_id = int(resolved_tmdb)
            except Exception:
                log.debug("Error resolving tmdb for %s", imdb_key, exc_info=True)

        # If we still don't have a valid tmdb_id, do not attempt to add (Radarr requires tmdbId)
        if not tmdb_id or int(tmdb_id) <= 0:
            log.warning("Cannot add movie: missing valid tmdbId for imdb=%s (tmdb=%s)", imdb_key, tmdb_id)
            return False

        # Build payload using available metadata; include tmdbId only when valid (>0)
        payload = {
            "title": title or "",
            "qualityProfileId": int(self.quality_profile),
            "rootFolderPath": self.root_folder,
            "monitored": True,
            "addOptions": {"searchForMovie": bool(force_search)},
            "images": []
        }

        payload["tmdbId"] = int(tmdb_id)
        # include imdbId as string (tt-prefixed) for add payload
        if imdb_key:
            payload["imdbId"] = imdb_key

        # Try pyarr-like convenience methods if available
        tried = False
        try:
            if self.api:
                for method_name in ("add_movie_by_imdb", "add_movie", "add"):
                    if hasattr(self.api, method_name):
                        method = getattr(self.api, method_name)
                        try:
                            try:
                                res = method(payload)
                            except TypeError:
                                # some wrappers accept just an id
                                res = method(tmdb_id if tmdb_id else imdb_key)
                            log.debug("Radarr client %s returned: %s", method_name, res)
                            tried = True
                            break
                        except Exception:
                            log.debug("Radarr client method %s failed", method_name, exc_info=True)
        except Exception:
            log.debug("Radarr client add attempts raised", exc_info=True)

        # Direct POST fallback to /api/v3/movie
        if not tried and self.base_url and self.api_key:
            try:
                resp = self._post("/api/v3/movie", payload)
                # after resp = self._post("/api/v3/movie", payload)
                try:
                    body = resp.json() if resp.text else None
                except Exception:
                    body = resp.text

                log.debug("Direct /api/v3/movie POST status=%s body=%s", resp.status_code, body)

                # 1) Created
                if resp.status_code == 201:
                    # Prefer to extract Radarr id from Location header if present
                    loc = resp.headers.get("Location")
                    if loc:
                        try:
                            radarr_id = int(loc.rstrip("/").split("/")[-1])
                            r = self._get(f"/api/v3/movie/{radarr_id}", timeout=15)
                            if r.ok:
                                log.info("Movie added and confirmed via /api/v3/movie/%s", radarr_id)
                                return True
                        except Exception:
                            log.debug("Failed to fetch movie by Location id", exc_info=True)
                    # fallback: if body contains tmdbId/imdbId treat as success
                    log.info("Movie added (201) but could not fetch by id immediately; treating as present.")
                    return True

                # 2) Movie already exists (400 with MovieExistsValidator)
                if resp.status_code == 400 and isinstance(body, list):
                    for err in body:
                        if err.get("errorCode") == "MovieExistsValidator":
                            log.info("Radarr reports movie already exists (tmdb=%s). Attempting targeted lookup.",
                                     payload.get("tmdbId"))
                            local = self._movie_exists_locally(imdb_id=payload.get("imdbId"),
                                                               tmdb_id=payload.get("tmdbId"))
                            if local:
                                log.info("Found existing movie in Radarr: %s (tmdb=%s)", local.get("title"),
                                         local.get("tmdbId"))
                                return True
                            log.warning("Radarr reported movie exists but targeted lookup failed; treating as present.")
                            return True

                # otherwise handle as before (log and return False)
                if resp.status_code >= 400:
                    log.warning("Radarr add returned status %s: %s", resp.status_code, body)
                    return False

            except Exception:
                log.exception("Direct POST to /api/v3/movie failed", exc_info=True)

        # Verify presence (Radarr may take a moment)
        for attempt in range(6):
            found = self._movie_exists_locally(imdb_id=imdb_key, tmdb_id=tmdb_id)
            if found:
                log.info("Movie present in Radarr after add/lookup: %s (tmdb=%s)", found.get("title"),
                         found.get("tmdbId"))
                return True
            time.sleep(1.0)

        log.warning("Failed to confirm movie in Radarr: imdb=%s tmdb=%s", imdb_key, tmdb_id)
        return False

    # -------------------------
    # Push NZB / release to Radarr
    # -------------------------
    def push_nzb(self, title: str, download_url: str, publish_date: Optional[str] = None,
                 imdb_id: Optional[str] = None, tmdb_id: Optional[int] = None,
                 size: Optional[int] = None, release_group: Optional[str] = None) -> Tuple[str, Any]:
        """
        Push an NZB/release to Radarr. Returns a tuple (status, details).
        Status values: "PUSHED", "REJECTED", "UNKNOWN_RESPONSE", "ERROR", "ALREADY_HAVE_BETTER"
        """
        try:
            # Try wrapper methods first if available
            if self.api:
                for method_name in ("post_release_push", "release_push", "push_release", "pushRelease"):
                    if hasattr(self.api, method_name):
                        method = getattr(self.api, method_name)
                        try:
                            # try to include identifiers if method accepts kwargs
                            try:
                                res = method(title=title, download_url=download_url, publish_date=publish_date,
                                             imdbId=imdb_id, tmdbId=tmdb_id)
                            except TypeError:
                                res = method(title, download_url)
                            log.debug("Radarr client push method %s returned: %s", method_name, res)
                            # Normalize response below
                            break
                        except Exception:
                            log.debug("Radarr client push method %s failed", method_name, exc_info=True)

            # Ensure movie exists in Radarr and obtain movieId when possible
            movie_id = None
            local = self._movie_exists_locally(imdb_id=imdb_id, tmdb_id=tmdb_id)
            if local and local.get("id"):
                movie_id = int(local.get("id"))
                log.debug("Found existing Radarr movie id=%s", movie_id)
            else:
                movie_id = self._add_movie_and_get_id(imdb_id=imdb_id, tmdb_id=tmdb_id, title=title)
                if movie_id:
                    log.info("Added movie to Radarr id=%s", movie_id)
                else:
                    log.debug("Could not add/find movie before push; will attempt push with tmdbId only")

            # Build push payload (type-correct). Do NOT include string imdbId here.
            payload = {
                "title": title,
                "downloadUrl": download_url,
                "protocol": "usenet",
                "publishDate": publish_date or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "releaseTitle": title
            }
            if tmdb_id:
                payload["tmdbId"] = int(tmdb_id)
            if size:
                try:
                    payload["size"] = int(size)
                except Exception:
                    pass
            if release_group:
                payload["releaseGroup"] = release_group

            # If we have a movie_id, give Radarr a short moment to index it, then include movieId
            if movie_id:
                # small delay to avoid race conditions between add and push
                time.sleep(1.5)
                payload["movieId"] = int(movie_id)

            # POST to the versioned endpoint
            try:
                resp = self._post("/api/v3/release/push", payload)
            except Exception as e:
                log.exception("Direct push to Radarr failed: %s", e)
                return "ERROR", str(e)

            # normalize response
            try:
                res_json = resp.json()
            except Exception:
                res_json = {"status_code": resp.status_code, "text": resp.text}

            log.debug("Push response status=%s body=%s", resp.status_code, res_json)

            # handle 405 explicitly and log Allow header
            if resp.status_code == 405:
                allow = resp.headers.get("Allow")
                log.warning("Radarr push endpoint returned 405 Method Not Allowed; Allow=%s", allow)
                return "UNKNOWN_RESPONSE", {"status_code": 405, "allow": allow, "text": resp.text}

            # If Radarr returned a list (per-release), inspect first element
            if isinstance(res_json, list) and len(res_json) > 0:
                first = res_json[0]
                # Approved
                if first.get("approved"):
                    return "PUSHED", None
                # Rejected with rejections array
                rejections = first.get("rejections") or []
                if rejections:
                    # Known case: existing equal/higher custom format score
                    if any("Existing file on disk has a equal or higher Custom Format score" in str(r) for r in rejections):
                        # Try to extract pushed score and quality weight from the response (first element)
                        pushed_score = None
                        pushed_quality_weight = None
                        try:
                            pushed_score = first.get("customFormatScore")
                            pushed_quality_weight = first.get("qualityWeight")
                        except Exception:
                            pushed_score = None
                            pushed_quality_weight = None

                        # Try to parse an existing/current score from the rejection messages (best-effort)
                        existing_score = None
                        for r in rejections:
                            try:
                                text = str(r)
                                # look for patterns like "score 2" or "Custom Format score 2"
                                m = re.search(r"score\s*[:=]?\s*(\d+)", text, re.IGNORECASE)
                                if not m:
                                    m = re.search(r"(\d+)\s*(?:points|score)", text, re.IGNORECASE)
                                if m:
                                    existing_score = int(m.group(1))
                                    break
                            except Exception:
                                continue

                        # Log a clear informational message with both scores
                        log.info(
                            "Release rejected: existing equal-or-better file; existing_score=%s pushed_score=%s pushed_quality_weight=%s; rejections=%s",
                            existing_score,
                            pushed_score,
                            pushed_quality_weight,
                            rejections
                        )
                        return "ALREADY_HAVE_BETTER", rejections

                    # If Unknown Movie, attempt targeted lookup and retry with movieId
                    if any("Unknown Movie" in str(r) for r in rejections):
                        log.info("Push rejected: Unknown Movie. Attempting targeted lookup and retry.")
                        local = self._movie_exists_locally(imdb_id=imdb_id, tmdb_id=tmdb_id)
                        if local and local.get("id"):
                            payload["movieId"] = int(local.get("id"))
                            log.info("Retrying push with movieId=%s", payload["movieId"])
                            try:
                                resp2 = self._post("/api/v3/release/push", payload)
                                try:
                                    res2 = resp2.json()
                                except Exception:
                                    res2 = {"status_code": resp2.status_code, "text": resp2.text}
                                log.debug("Retry push response status=%s body=%s", resp2.status_code, res2)
                                if isinstance(res2, list) and res2 and res2[0].get("approved"):
                                    return "PUSHED", None
                                return "REJECTED", res2
                            except Exception:
                                log.exception("Retry push failed", exc_info=True)
                                return "ERROR", "retry_failed"
                        return "REJECTED", rejections
                    # Other rejections
                    return "REJECTED", rejections

            # If dict response with validation errors
            if isinstance(res_json, dict):
                if res_json.get("approved"):
                    return "PUSHED", None
                if res_json.get("errors") or res_json.get("rejections"):
                    return "REJECTED", res_json

            return "UNKNOWN_RESPONSE", res_json

        except Exception as e:
            log.exception("Push Exception: %s", e)
            return "ERROR", str(e)

    # -------------------------
    # High-level release processing
    # -------------------------
    def process_release(self, release_title: str, imdb_id: Optional[str], download_url: str,
                        publish_date: Optional[str] = None, size: Optional[int] = None,
                        release_group: Optional[str] = None, extra: Optional[Dict[str, Any]] = None,
                        force_refresh: bool = False) -> str:
        """
        Main entry used by the RSS pipeline.
        Steps:
          - Resolve tmdbId (cache -> local -> radarr lookup -> tmdb API)
          - If movie not present locally, attempt to add it (using tmdbId/imdbId)
          - Push the release including identifiers

        force_refresh: if True, skip the imdb->tmdb cache for this run.
        Returns a status string used by the caller.
        """

        tmdb_id, src = self._resolve_tmdb_from_imdb(imdb_id, force_refresh=force_refresh) if imdb_id else (None, "no_imdb")
        log.debug("Resolver returned tmdb_id=%s source=%s for imdb=%s", tmdb_id, src, imdb_id)

        try:
            tmdb_id, src = self._resolve_tmdb_from_imdb(imdb_id, force_refresh=force_refresh) if imdb_id else (None, "no_imdb")
            log.debug("Resolved tmdb_id=%s source=%s for imdb=%s", tmdb_id, src, imdb_id)

            # If movie exists locally, we can skip add
            local = self._movie_exists_locally(imdb_id=imdb_id, tmdb_id=tmdb_id)
            if not local:
                # Try to add the movie so Radarr has a local record
                added = self._ensure_movie_in_radarr(imdb_id=imdb_id, tmdb_id=tmdb_id, title=release_title, force_search=True, force_refresh=force_refresh)
                if not added:
                    log.info("Movie %s not in library and add failed", release_title)
                    return "NOT_IN_LIBRARY"

            # Attempt push
            status, details = self.push_nzb(
                title=release_title,
                download_url=download_url,
                publish_date=publish_date,
                imdb_id=imdb_id,
                tmdb_id=tmdb_id,
                size=size,
                release_group=release_group
            )

            if status == "PUSHED":
                log.info("Push approved for %s", release_title)
                return "PUSHED"
            if status == "ALREADY_HAVE_BETTER":
                # Treat equal-or-better as informational rather than unexpected
                log.info("Push skipped for %s: already have equal or better file", release_title)
                return "ALREADY_HAVE_BETTER"
            if status == "REJECTED":
                log.info("Push rejected for %s: %s", release_title, details)
                # If rejected because movie unknown, caller may attempt fallback add/search
                return "REJECTED"
            if status == "UNKNOWN_RESPONSE":
                log.warning("Unknown push response for %s: %s", release_title, details)
                return "UNKNOWN_RESPONSE"
            return "API_ERROR"

        except Exception:
            log.exception("process_release failed for %s", release_title)
            return "API_ERROR"
