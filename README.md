markdown
# README

## Overview
A lightweight RSS → Radarr bridge that scans NZB RSS feeds, matches releases by IMDb, and pushes NZB releases into Radarr when they improve on existing files. Supports multiple feeds (NZBFinder, NZBgeek), handles Radarr push rejections and fallbacks, and persists processed GUIDs to avoid duplicates.

---

## Quick start
**Clone repository**
```bash
git clone <repo-url>
cd nzbfinder_2_radarr
Create virtual environment and install

bash
python -m venv .venv
# macOS Linux
source .venv/bin/activate
# Windows
.venv\Scripts\activate
pip install -r requirements.txt
Run

bash
python main.py
Common commands

Run once: python main.py

Run in debug: set DEBUG=true in environment or enable debug in config.yaml.

Configuration
Config file location: config/config.yaml

Important fields

rss_feeds list of feeds to poll; supports environment variable expansion.

radarr URL, API key, quality profile id, root folder, and threshold.

max_stored_guids how many GUIDs to keep for dedupe.

match_patterns and not_match_patterns include and exclude regex lists for titles.

Example config snippet

yaml
rss_feeds:
  - name: "NZBFinder"
    url: "https://nzbfinder.ws/rss/category?id=2040&dl=1&num=50&api_token=${NZBFINDER_API_KEY}"
  - name: "NZBgeek"
    url: "https://api.nzbgeek.info/rss?t=2040&limit=100&dl=1&r=${NZBGEEK_API_KEY}"

radarr:
  url: "http://192.168.0.218:7878"
  api_key: "YOUR_API_KEY"
  quality_profile: 8
  quality_threshold: 18
  root_folder: "/data/media/movies"

max_stored_guids: 1000
match_patterns:
  - "1080p"
not_match_patterns:
  - "CAM"
Notes

Use environment variables for API keys. The code expands ${VAR} in feed URLs.

quality_threshold is the minimum quality score you want to exceed before attempting upgrades.

RSS parsing details
Supported fields

title, link or enclosure, pubDate, and newznab:attr entries such as imdb, guid, size.

IMDB normalization

Feed imdb values are normalized and converted to tt format. Padded values like 00101531 are corrected so Radarr lookups succeed.

Deduplication

GUIDs are normalized (last path segment or id/guid query param) and persisted to config/scanned_guids.txt.

Behavior

Items missing an NZB URL or IMDb id are skipped and marked seen to avoid repeated retries.

NZBgeek entries are parsed defensively: the parser extracts imdb, guid, enclosure and falls back to scanning the description when needed.

Logging and troubleshooting
Log output

Console logging by default; configure logging in main.py or config.yaml.

Key log statuses

✅ MATCHED title matched and IMDb found.

⬆️ Upgrade needed existing file below threshold; attempting push.

✅ PUSHED Radarr accepted the pushed NZB.

⛔ PUSH REJECTED Radarr rejected the push; rejection details are logged.

ℹ️ QUALITY MET existing file meets or exceeds threshold; no action taken.

➕ ADDED movie added to Radarr via fallback.

When pushes are rejected

Check the rejection reason in the logs (custom format score, existing file meets cutoff).

The program inspects the authoritative Radarr record and logs the existing file score when available.

Debugging tips

Enable debug logging to see raw Radarr responses and payloads.

Verify Radarr API access with a curl to /api/v3/movie using your API key.

Confirm feed URLs in a browser to ensure API keys and query parameters are correct.

If NZBgeek items are missing imdb, inspect the <description> for newznab:attr entries.