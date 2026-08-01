"""Key-free TVmaze fallback for series and episode metadata."""

from __future__ import annotations

import html
import logging
import re

import requests

LOG = logging.getLogger("TVmazeHelper")
BASE_URL = "https://api.tvmaze.com"


def _plain(value: str | None) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", value or "")).strip()


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


class TVmazeHelper:
    """Small adapter returning the same shape used by TMDB enrichment."""

    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "myHomeTV/1.0"})

    def is_configured(self) -> bool:
        return True

    def search_tv(self, title: str):
        try:
            response = self.session.get(
                f"{BASE_URL}/search/shows", params={"q": title}, timeout=8
            )
            response.raise_for_status()
            results = response.json() or []
            if not results:
                return None
            wanted = _key(title)
            result = max(
                results,
                key=lambda item: (
                    _key(str((item.get("show") or {}).get("name", ""))) == wanted,
                    float(item.get("score") or 0),
                ),
            )
            show = result.get("show") or {}
            image = show.get("image") or {}
            return {
                "tvmaze_id": show.get("id"),
                "name": show.get("name"),
                "original_name": show.get("name"),
                "overview": _plain(show.get("summary")),
                "first_air_date": show.get("premiered") or "",
                "genre": show.get("genres") or [],
                "poster_url": image.get("original") or image.get("medium"),
                "backdrop_url": image.get("original") or image.get("medium"),
                "metadata_source": "tvmaze",
            }
        except Exception as exc:
            LOG.warning("TVmaze series lookup failed for %s: %s", title, exc)
            return None

    def get_tv_episode(self, show_id: int, season: int, episode: int):
        try:
            response = self.session.get(
                f"{BASE_URL}/shows/{show_id}/episodebynumber",
                params={"season": season, "number": episode}, timeout=8,
            )
            response.raise_for_status()
            data = response.json()
            image = data.get("image") or {}
            return {
                "name": data.get("name"),
                "overview": _plain(data.get("summary")),
                "air_date": data.get("airdate") or "",
                "still_url": image.get("original") or image.get("medium"),
            }
        except Exception as exc:
            LOG.info("TVmaze episode lookup missed show=%s S%sE%s: %s", show_id, season, episode, exc)
            return None
