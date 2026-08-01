"""
TMDB (The Movie Database) API helper for fetching movie metadata.
"""
import requests
import logging
import json
from typing import Optional, Dict, Any
from pathlib import Path
from fs42.station_manager import StationManager

logger = logging.getLogger("TMDB_Helper")

# TMDB API configuration
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE_URL = "https://image.tmdb.org/t/p/w780"

# Cache directory for TMDB results
CACHE_DIR = Path("catalog/.tmdb_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class TMDBHelper:
    """Helper class for fetching movie metadata from TMDB."""

    def __init__(self, api_key: Optional[str] = None):

        if api_key:
            self.api_key = api_key
        else:
            # Get API key from StationManager's server_conf (loaded from main_config.json)
            station_manager = StationManager()
            self.api_key = station_manager.server_conf.get("tmdb_api_key", "")

        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json"
        })

    def is_configured(self) -> bool:
        """Check if TMDB API key is configured."""
        return bool(self.api_key)

    def _parse_title_and_year(self, filename: str) -> tuple[str, Optional[int]]:
        """
        Parse filename to extract clean title and optional year.

        Supports formats:
        - Movie Title (1983)
        - Movie_Title_1983
        - Movie-Title-1983
        - Movie.Title.1983

        Args:
            filename: Raw filename without extension

        Returns:
            Tuple of (clean_title, year) where year is None if not found
        """
        import re

        # First, try to extract year from parentheses: "Movie Title (1983)"
        year_match = re.search(r'\((\d{4})\)', filename)
        year = None
        clean_filename = filename

        if year_match:
            year = int(year_match.group(1))
            # Remove the year part from filename
            clean_filename = re.sub(r'\s*\(\d{4}\)\s*', ' ', filename)
        else:
            # Try to find 4-digit year at the end: "Movie_Title_1983"
            year_match = re.search(r'[_\-\.\s](\d{4})$', filename)
            if year_match:
                year = int(year_match.group(1))
                # Remove the year from filename
                clean_filename = re.sub(r'[_\-\.\s]\d{4}$', '', filename)

        # Normalize separators: replace underscores, dashes, dots with spaces
        clean_title = re.sub(r'[_\-\.]+', ' ', clean_filename)

        # Clean up extra whitespace
        clean_title = ' '.join(clean_title.split())

        return clean_title, year

    def _get_cache_path(self, title: str, kind: str = "movie") -> Path:
        """Get cache file path for a given title."""
        # Sanitize title for filename
        safe_title = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in title)
        return CACHE_DIR / f"{kind}-{safe_title}.json"

    def _load_from_cache(self, title: str, kind: str = "movie") -> Optional[Dict[str, Any]]:
        """Load cached TMDB data for a title."""
        cache_path = self._get_cache_path(title, kind)
        if cache_path.exists():
            try:
                with open(cache_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"Loaded TMDB data from cache for '{title}'")
                    return data
            except Exception as e:
                logger.warning(f"Failed to load cache for '{title}': {e}")
        return None

    def _save_to_cache(self, title: str, data: Dict[str, Any], kind: str = "movie"):
        """Save TMDB data to cache."""
        cache_path = self._get_cache_path(title, kind)
        try:
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            logger.info(f"Saved TMDB data to cache for '{title}'")
        except Exception as e:
            logger.warning(f"Failed to save cache for '{title}': {e}")

    def search_movie(self, title: str) -> Optional[Dict[str, Any]]:

        if not self.is_configured():
            logger.warning("TMDB API key not configured, skipping search")
            return None

        # Parse title to extract year if present
        clean_title, year = self._parse_title_and_year(title)

        # Check cache first (using original title as cache key)
        cached = self._load_from_cache(title, "movie")
        if cached:
            return cached

        # Search TMDB
        try:
            search_url = f"{TMDB_BASE_URL}/search/movie"
            params = {
                "api_key": self.api_key,
                "query": clean_title,
                "language": "en-US",
                "page": 1
            }

            # Add year parameter if we found one in the filename
            if year:
                params["year"] = year
                logger.info(f"Searching TMDB for '{clean_title}' ({year})")
            else:
                logger.info(f"Searching TMDB for '{clean_title}'")

            response = self.session.get(search_url, params=params, timeout=5)
            response.raise_for_status()

            data = response.json()
            results = data.get("results", [])

            if not results:
                logger.info(f"No TMDB results found for '{clean_title}'" + (f" ({year})" if year else ""))
                return None

            # Get the first (best) match
            movie = results[0]

            # Extract relevant data
            movie_data = {
                "tmdb_id": movie.get("id"),
                "title": movie.get("title"),
                "original_title": movie.get("original_title"),
                "overview": movie.get("overview", ""),
                "release_date": movie.get("release_date", ""),
                "poster_path": movie.get("poster_path"),
                "poster_url": f"{TMDB_IMAGE_BASE_URL}{movie.get('poster_path')}" if movie.get("poster_path") else None,
                "backdrop_path": movie.get("backdrop_path"),
                "backdrop_url": f"{TMDB_IMAGE_BASE_URL}{movie.get('backdrop_path')}" if movie.get("backdrop_path") else None,
                "vote_average": movie.get("vote_average"),
                "popularity": movie.get("popularity")
            }

            # Save to cache
            self._save_to_cache(title, movie_data, "movie")

            logger.info(f"Found TMDB match for '{title}': {movie_data['title']} ({movie_data['release_date'][:4] if movie_data['release_date'] else 'N/A'})")

            return movie_data

        except requests.exceptions.RequestException as e:
            logger.error(f"TMDB API request failed for '{title}': {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error searching TMDB for '{title}': {e}")
            return None

    def search_tv(self, title: str) -> Optional[Dict[str, Any]]:
        """Return the best English-localized television series match."""
        if not self.is_configured():
            logger.warning("TMDB API key not configured, skipping TV search")
            return None
        clean_title, year = self._parse_title_and_year(title)
        cached = self._load_from_cache(title, "tv")
        if cached:
            return cached
        try:
            params = {
                "api_key": self.api_key,
                "query": clean_title,
                "language": "en-US",
                "page": 1,
            }
            if year:
                params["first_air_date_year"] = year
            response = self.session.get(
                f"{TMDB_BASE_URL}/search/tv", params=params, timeout=8
            )
            response.raise_for_status()
            results = response.json().get("results", [])
            if not results:
                return None
            show = results[0]
            details = self.get_tv_details(show["id"]) or {}
            genres = [item.get("name") for item in details.get("genres", []) if item.get("name")]
            data = {
                "tmdb_id": show.get("id"),
                "name": show.get("name"),
                "original_name": show.get("original_name"),
                "overview": details.get("overview") or show.get("overview", ""),
                "first_air_date": show.get("first_air_date", ""),
                "genre": genres,
                "poster_url": f"{TMDB_IMAGE_BASE_URL}{show.get('poster_path')}" if show.get("poster_path") else None,
                "backdrop_url": f"{TMDB_IMAGE_BASE_URL}{show.get('backdrop_path')}" if show.get("backdrop_path") else None,
            }
            self._save_to_cache(title, data, "tv")
            return data
        except Exception as exc:
            logger.error("TMDB TV search failed for %s: %s", title, exc)
            return None

    def get_tv_details(self, tmdb_id: int) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None
        try:
            response = self.session.get(
                f"{TMDB_BASE_URL}/tv/{tmdb_id}",
                params={"api_key": self.api_key, "language": "en-US"},
                timeout=8,
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            logger.error("TMDB TV details failed for %s: %s", tmdb_id, exc)
            return None

    def get_tv_episode(
        self, tmdb_id: int, season: int, episode: int
    ) -> Optional[Dict[str, Any]]:
        if not self.is_configured():
            return None
        try:
            response = self.session.get(
                f"{TMDB_BASE_URL}/tv/{tmdb_id}/season/{season}/episode/{episode}",
                params={"api_key": self.api_key, "language": "en-US"},
                timeout=8,
            )
            response.raise_for_status()
            data = response.json()
            still = data.get("still_path")
            return {
                "name": data.get("name"),
                "overview": data.get("overview", ""),
                "air_date": data.get("air_date", ""),
                "still_url": f"{TMDB_IMAGE_BASE_URL}{still}" if still else None,
            }
        except Exception as exc:
            logger.warning(
                "TMDB episode lookup failed for %s S%sE%s: %s",
                tmdb_id, season, episode, exc,
            )
            return None
    def get_movie_details(self, tmdb_id: int) -> Optional[Dict[str, Any]]:

        if not self.is_configured():
            return None

        try:
            details_url = f"{TMDB_BASE_URL}/movie/{tmdb_id}"
            params = {
                "api_key": self.api_key,
                "language": "en-US"
            }

            response = self.session.get(details_url, params=params, timeout=5)
            response.raise_for_status()

            return response.json()

        except Exception as e:
            logger.error(f"Failed to get TMDB details for ID {tmdb_id}: {e}")
            return None


# Global instance
_tmdb_helper = None


def get_tmdb_helper() -> TMDBHelper:
    """Get or create global TMDB helper instance."""
    global _tmdb_helper
    if _tmdb_helper is None:
        _tmdb_helper = TMDBHelper()
    return _tmdb_helper
