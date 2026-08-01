from fastapi import APIRouter
from datetime import datetime
from pathlib import Path
import re
from fs42.station_manager import StationManager
from fs42.liquid_api import LiquidAPI
from fs42.metadata_io import MetadataIO
from fs42.title_parser import TitleParser
from fs42.live_news import schedule_blocks as live_schedule_blocks
from fs42.artwork_preloader import artwork_url as canonical_artwork_url

router = APIRouter(prefix="/schedules", tags=["schedules"])
EPISODE_RE = re.compile(
    r"(?i)^(?P<series>.*?)[\s._\-\[(]*s(?P<season>\d{1,3})"
    r"[\s._-]*e(?P<episode>\d{1,3}[a-z]*)[\])]*[\s._-]*(?P<title>.*)$"
)
LEADING_X_EPISODE_RE = re.compile(
    r"(?i)^0*(?P<season>\d{1,3})x0*(?P<episode>\d{1,3}[a-z]*)"
    r"[\s._-]*(?P<title>.*)$"
)
SERIES_X_EPISODE_RE = re.compile(
    r"(?i)^(?P<series>.*?)[\s._\-\[(]+0*(?P<season>\d{1,3})x"
    r"0*(?P<episode>\d{1,3}[a-z]*)[\])]*[\s._-]*(?P<title>.*)$"
)
VERBOSE_EPISODE_RE = re.compile(
    r"(?i)^(?P<series>.*?)[\s._\-\[(]+season[\s._-]*0*(?P<season>\d{1,3})"
    r"[\s._-]+episode[\s._-]*0*(?P<episode>\d{1,3}[a-z]*)[\])]*"
    r"[\s._-]*(?P<title>.*)$"
)
LOOSE_EPISODE_RE = re.compile(
    r"(?i)^(?P<collection>.*?)[\s._-]+(?P<episode>\d{1,3})"
    r"[\s._-]+(?P<title>\D.*)$"
)
SEASON_DIR_RE = re.compile(r"(?i)^season[\s._-]*\d+$|^s\d+$")
MOVIE_YEAR_RE = re.compile(r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)")
RELEASE_SUFFIX_RE = re.compile(
    r"(?i)(?:[\s._-]+)(?:480p|720p|1080p|2160p|4k|amzn|amazon|"
    r"web[\s._-]?(?:dl|rip)|blu[\s._-]?ray|bluray|remux|hdr|dv|"
    r"x26[45]|h[\s._-]?26[45]|hevc|av1|aac\d*|ac3|eac3)(?:\b|$).*$"
)
PAREN_RELEASE_SUFFIX_RE = re.compile(
    r"(?i)\s*[\(\[]\s*(?:480p|720p|1080p|2160p|4k|web[\s._-]?(?:dl|rip)|"
    r"blu[\s._-]?ray|bluray|remux|hdr|dv|x26[45]|h[\s._-]?26[45]|"
    r"hevc|av1|aac\d*|ac3|eac3)\b.*$"
)
TRAILING_NOTE_RE = re.compile(
    r"\s*\((?!(?:19|20)\d{2}\)\s*$)[^()]+\)\s*$",
    re.IGNORECASE,
)
TITLE_ALIASES = {
    # Legacy/custom title patterns can truncate this series before the final
    # word, especially for multi-item blocks that have no single media path.
    "game of": "Game of Thrones",
    "game of thrones": "Game of Thrones",
    "schoolhouse rock": "Schoolhouse Rock",
    "shingeki no kyojin": "Attack on Titan",
    "spongebob": "SpongeBob SquarePants",
    "spongebob squarepants": "SpongeBob SquarePants",
    "under the dome": "Under the Dome",
    # Some legacy/custom block builders persist the title before the final
    # word. Keep that known incomplete identity from leaking into the guide.
    "under the": "Under the Dome",
    "king of the hill": "King of the Hill",
}
MOVIE_TITLE_ALIASES = {
    "futurama benders big score": "Futurama: Bender's Big Score",
    "futurama the beast with a billion backs": (
        "Futurama: The Beast with a Billion Backs"
    ),
    "futurama benders game": "Futurama: Bender's Game",
    "futurama into the wild green yonder": (
        "Futurama: Into the Wild Green Yonder"
    ),
    "the super mario bros movie": "The Super Mario Bros. Movie",
}
AUXILIARY_TITLES = {
    "behind the scenes",
    "deleted and extended scenes",
    "deleted scenes",
    "extended scenes",
    "extras",
    "featurettes",
    "interviews",
    "samples",
    "scenes",
    "shorts",
    "trailers",
}
MINOR_TITLE_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "of", "on", "or", "the", "to", "with"}


def _schedule_payload(
    network_name: str,
    start: str | None = None,
    end: str | None = None,
    include_meta: bool = False,
    include_display: bool = False,
):
    """Build one schedule response without putting station names in URL paths."""
    conf = StationManager().station_by_name(network_name)
    if conf is None:
        return {"error": f"Unknown station {network_name}", "schedule_blocks": []}
    sdt = None
    edt = None
    if start and end:
        try:
            sdt = datetime.fromisoformat(start)
            edt = datetime.fromisoformat(end)
        except ValueError:
            return {
                "error": "Invalid date format. Use ISO format (YYYY-MM-DDTHH:MM:SS) for start and end.",
                "schedule_blocks": [],
            }

    if conf.get("network_type") == "live_news":
        sdt = sdt or datetime.now()
        edt = edt or (sdt + __import__("datetime").timedelta(hours=24))
        schedule_blocks = live_schedule_blocks(conf, sdt, edt)
    else:
        schedule_blocks = LiquidAPI.get_blocks(conf, sdt, edt)
    if include_meta or include_display:
        _attach_meta(schedule_blocks, read_meta=include_meta)
    return {"network_name": network_name, "schedule_blocks": schedule_blocks}


def _natural_title_case(title: str) -> str:
    words = title.split()
    return " ".join(
        word.lower() if index and word.casefold() in MINOR_TITLE_WORDS else word
        for index, word in enumerate(words)
    )


def _display_episode_number(value):
    """Normalize file-oriented episode codes for television-style labels."""
    if value in (None, ""):
        return value
    match = re.match(r"\s*0*(\d+)", str(value))
    return int(match.group(1)) if match else value


def _plain_title(title: str) -> str:
    cleaned = re.sub(r"\s+", " ", re.sub(r"[._-]+", " ", title)).strip()
    return " ".join(
        (
            word.capitalize()
            if word.islower() or word.isupper()
            else word
        )
        for word in cleaned.split()
    )


def _guide_title_alias(title: str) -> str:
    # This function also receives already-clean schedule titles. Do not run
    # generic episode-number patterns here: "Channel 42 Live Fixture" is not
    # episode 42.
    title = TRAILING_NOTE_RE.sub("", title)
    title = RELEASE_SUFFIX_RE.sub("", title)
    normalized = _natural_title_case(_plain_title(title))
    lowered = normalized.casefold()
    for source, replacement in TITLE_ALIASES.items():
        if lowered == source or lowered.startswith(source + " "):
            return replacement
    return normalized


def _movie_display(path: str) -> dict:
    media_path = Path(path)
    # Movie libraries commonly place featurettes and documentaries beneath a
    # "Movie Name (Year)" directory. Never let those auxiliary filenames
    # replace the identity of the movie in an already-generated schedule.
    candidates = [media_path.stem, *(parent.name for parent in media_path.parents[:3])]
    for candidate in candidates:
        match = MOVIE_YEAR_RE.search(candidate)
        if match:
            title = candidate[:match.start()].strip(" ._-(")
            if not title:
                continue
            normalized_title = _natural_title_case(_plain_title(title))
            normalized_title = re.sub(
                r"(?i)^(?:movie|film)\s+\d+\s+", "", normalized_title
            )
            canonical_title = None
            canonical_title = MOVIE_TITLE_ALIASES.get(
                normalized_title.casefold()
            )
            if normalized_title.casefold() == "el camino a breaking bad movie":
                canonical_title = "El Camino: a Breaking Bad Movie"
            star_wars = re.match(
                r"(?i)^star wars\s+(?:episode\s+)?"
                r"(?P<episode>[ivxlcdm]+)\s+(?P<subtitle>.+)$",
                normalized_title,
            )
            if star_wars:
                subtitle = _natural_title_case(star_wars.group("subtitle"))
                subtitle = subtitle[:1].upper() + subtitle[1:]
                canonical_title = (
                    "Star Wars: Episode "
                    f"{star_wars.group('episode').upper()} - "
                    f"{subtitle}"
                )
            return {
                "display_title": (
                    f"{canonical_title or _guide_title_alias(normalized_title)} "
                    f"({match.group('year')})"
                )
            }
    return {}


def _episode_display(path: str, meta: dict | None = None) -> dict:
    """Return stable series and episode labels without changing stored titles."""
    meta = meta or {}
    filename = Path(path).stem
    match = EPISODE_RE.match(filename)
    x_match = SERIES_X_EPISODE_RE.match(filename)
    verbose_match = VERBOSE_EPISODE_RE.match(filename)
    leading_match = LEADING_X_EPISODE_RE.match(filename)
    if (
        meta.get("type") != "episode"
        and not match and not x_match and not verbose_match and not leading_match
    ):
        return {}

    series_title = meta.get("show_title")
    episode_title = meta.get("title")
    season = meta.get("season")
    episode = meta.get("episode")

    if match:
        series_prefix = match.group("series").strip(" ._-")
        parsed_series = (
            TitleParser.parse_title(series_prefix) if series_prefix else ""
        )
        if not series_title:
            series_title = parsed_series
        elif parsed_series.casefold().startswith(
            series_title.casefold().rstrip() + " "
        ):
            # Repair incomplete NFO/tag titles such as "Game of" when the
            # structured SxxExx filename contains the complete series name.
            series_title = parsed_series
        if not episode_title and match.group("title"):
            episode_title = _natural_title_case(
                TitleParser.parse_title(
                    RELEASE_SUFFIX_RE.sub("", match.group("title"))
                )
            )
        season = season if season is not None else int(match.group("season"))
        episode = episode if episode is not None else match.group("episode")

    if x_match:
        if not series_title:
            series_title = TitleParser.parse_title(
                x_match.group("series").strip(" ._-")
            )
        if not episode_title and x_match.group("title"):
            episode_title = _natural_title_case(
                TitleParser.parse_title(
                    RELEASE_SUFFIX_RE.sub("", x_match.group("title"))
                )
            )
        season = season if season is not None else int(x_match.group("season"))
        episode = episode if episode is not None else x_match.group("episode")

    if verbose_match:
        if not series_title:
            series_title = TitleParser.parse_title(
                verbose_match.group("series").strip(" ._-")
            )
        if not episode_title and verbose_match.group("title"):
            episode_title = _natural_title_case(
                TitleParser.parse_title(verbose_match.group("title"))
            )
        season = season if season is not None else int(verbose_match.group("season"))
        episode = episode if episode is not None else verbose_match.group("episode")

    if leading_match:
        if not episode_title and leading_match.group("title"):
            episode_title = _natural_title_case(
                TitleParser.parse_title(
                    RELEASE_SUFFIX_RE.sub("", leading_match.group("title"))
                )
            )
        season = (
            season if season is not None else int(leading_match.group("season"))
        )
        episode = (
            episode if episode is not None else leading_match.group("episode")
        )

    if not series_title:
        parent = Path(path).parent
        if SEASON_DIR_RE.match(parent.name):
            parent = parent.parent
        if parent.name:
            series_title = TitleParser.parse_title(parent.name)

    result = {
        "display_title": _guide_title_alias(
            series_title or TitleParser.parse_title(filename)
        ),
        "episode_title": _natural_title_case(
            PAREN_RELEASE_SUFFIX_RE.sub(
                "", RELEASE_SUFFIX_RE.sub("", episode_title or "")
            )
        ),
        "season": season,
        "episode": _display_episode_number(episode),
    }
    return result


def _directory_episode_display(path: str) -> dict:
    """Parse numbered episodes whose filenames omit SxxExx notation.

    Disc and short-form collections commonly use names such as
    ``Schoolhouse Rock Multiplication Rock 08 Figure Eight``.  A matching
    show directory gives us enough context to separate the program identity
    from the collection label, track number, and actual episode title.
    """
    media_path = Path(path)
    filename = _plain_title(media_path.stem)
    filename_folded = filename.casefold()
    for parent in media_path.parents[:4]:
        series = _plain_title(parent.name)
        if not series or not filename_folded.startswith(series.casefold() + " "):
            continue
        remainder = filename[len(series):].strip()
        match = LOOSE_EPISODE_RE.match(remainder)
        if not match:
            continue
        episode_title = RELEASE_SUFFIX_RE.sub("", match.group("title"))
        return {
            "display_title": _guide_title_alias(series),
            "episode_title": _natural_title_case(_plain_title(episode_title)),
            "season": None,
            "episode": int(match.group("episode")),
        }
    return {}


def _known_series_episode_display(path: str) -> dict:
    """Parse numbered shorts even when they live in a shared channel folder."""
    filename = _plain_title(Path(path).stem)
    filename_folded = filename.casefold()
    for source, display_title in TITLE_ALIASES.items():
        if not filename_folded.startswith(source + " "):
            continue
        remainder = filename[len(source):].strip()
        match = LOOSE_EPISODE_RE.match(remainder)
        if not match:
            continue
        episode_title = RELEASE_SUFFIX_RE.sub("", match.group("title"))
        return {
            "display_title": display_title,
            "episode_title": _natural_title_case(_plain_title(episode_title)),
            "season": None,
            "episode": int(match.group("episode")),
        }
    return {}


def _known_series_display(path: str) -> dict:
    """Recover a known series name when a stored schedule title is truncated."""
    media_path = Path(path)
    candidates = [media_path.stem, *(parent.name for parent in media_path.parents[:4])]
    for candidate in candidates:
        normalized = _plain_title(candidate).casefold()
        for source, display_title in TITLE_ALIASES.items():
            if normalized == source or normalized.startswith(source + " "):
                return {"display_title": display_title}
    return {}


def _supplemental_display(path: str) -> dict:
    parts = Path(path).parts
    for index, part in enumerate(parts):
        normalized = re.sub(r"[^a-z0-9]+", " ", part.casefold()).strip()
        if normalized not in AUXILIARY_TITLES or index == 0:
            continue
        parent = Path(parts[index - 1])
        if SEASON_DIR_RE.match(parent.name) and index >= 2:
            parent = Path(parts[index - 2])
        parent_series = _guide_title_alias(parent.name)
        normalized_parent = _plain_title(parent.name).casefold()
        if any(
            normalized_parent == source
            or normalized_parent.startswith(source + " ")
            for source in TITLE_ALIASES
        ):
            return {"display_title": parent_series}
        parent_movie = _movie_display(str(parent))
        if parent_movie:
            return parent_movie
        return {"display_title": parent_series}
    return {}


def program_display(path: str, fallback: str = "", meta: dict | None = None) -> dict:
    """Return one canonical program identity for the guide and watch client."""
    meta = meta or {}
    display = (
        _episode_display(path, meta)
        or _directory_episode_display(path)
        or _known_series_episode_display(path)
        or _known_series_display(path)
        or _supplemental_display(path)
        or _movie_display(path)
    )
    if not display:
        display = {
            "display_title": _guide_title_alias(fallback or Path(path).stem)
        }

    # Bad or overly eager episode tags occasionally accompany movie files
    # (notably disc rips with numbered audio tracks). A movie identity must
    # never expose those values as season/episode information in the UI.
    media_type = str(meta.get("type", "")).casefold()
    title_is_movie = re.search(
        r"\bmovie\b", str(display.get("display_title", "")), re.IGNORECASE
    )
    lives_in_movie_library = any(
        part.casefold() in {"movie", "movies", "film", "films"}
        for part in Path(path).parts[:-1]
    )
    if media_type in {"movie", "film"} or title_is_movie or lives_in_movie_library:
        movie = _movie_display(path)
        if movie:
            display = movie
        else:
            display.pop("season", None)
            display.pop("episode", None)
            display.pop("episode_title", None)

    season = display.get("season")
    episode = display.get("episode")
    episode_title = display.get("episode_title", "")
    if season not in (None, "") and episode not in (None, ""):
        display["program_details"] = f"S{season}E{str(episode).upper()}"
    elif season not in (None, ""):
        display["program_details"] = f"S{season}"
    elif episode not in (None, ""):
        display["program_details"] = f"E{str(episode).upper()}"
    else:
        display["program_details"] = ""
    if episode_title:
        display["program_details"] += (
            ": " if display["program_details"] else ""
        ) + episode_title
    return display


def _attach_meta(blocks, read_meta: bool = True):
    """Attach cached NFO/tag metadata to each single-content block in place.

    Multi-content blocks (clip shows, loops) and off-air blocks have no single
    feature to describe, so they are left without a `meta` field and consumers
    fall back to the block title.
    """
    if not blocks:
        return blocks
    for block in blocks:
        content = getattr(block, "content", None)
        if content is None or isinstance(content, list):
            continue
        path = getattr(content, "path", None)
        if not path:
            continue
        meta = MetadataIO.read(path) if read_meta else None
        if meta:
            block.meta = meta
        display = program_display(
            path,
            getattr(block, "raw_title", None) or getattr(block, "title", ""),
            meta,
        )
        for key, value in display.items():
            setattr(block, key, value)
        block.artwork_url = canonical_artwork_url(path, meta)
        programming = getattr(block, "programming", None)
        if isinstance(programming, dict):
            block.airing_kind = programming.get("airing_kind")
    return blocks

@router.get("/search_all")
async def search_all_schedules(query: str = None):
    if not query:
        # If no query, get all blocks from all stations
        station_manager = StationManager()
        all_results = []
        
        for station in station_manager.stations:
            if station.get("_has_schedule", False):
                try:
                    schedule_blocks = LiquidAPI.get_blocks(station)
                    if schedule_blocks:
                        all_results.append({
                            "network_name": station["network_name"],
                            "schedule_blocks": schedule_blocks
                        })
                except Exception as e:
                    all_results.append({
                        "network_name": station["network_name"],
                        "error": str(e),
                        "schedule_blocks": []
                    })
        
        return {"query": query, "results": all_results}
    else:
        # Search across all stations at once
        try:
            search_results = LiquidAPI.search_all_blocks(query)
            all_results = []
            
            for station_name, blocks in search_results.items():
                if blocks:
                    all_results.append({
                        "network_name": station_name,
                        "schedule_blocks": blocks
                    })
            
            return {"query": query, "results": all_results}
        except Exception as e:
            return {"query": query, "error": str(e), "results": []}

@router.get("/search/{network_name}")
async def search_schedule(network_name: str, query: str = None):
    conf = StationManager().station_by_name(network_name)
    if conf is None:
        return {"error": f"Unknown station {network_name}", "schedule_blocks": []}
    if query:
        schedule_blocks = LiquidAPI.search_blocks(conf, query)
    else:
        schedule_blocks = LiquidAPI.get_blocks(conf)

    return {"network_name": network_name, "query": query, "schedule_blocks": schedule_blocks}


@router.get("")
@router.get("/")
async def get_schedule_by_query(
    network_name: str,
    start: str = None,
    end: str = None,
    include_meta: bool = False,
    include_display: bool = False,
):
    """Query-safe schedule route for names containing '/', '#', or Unicode."""
    return _schedule_payload(
        network_name, start, end, include_meta, include_display
    )

@router.get("/{network_name}")
async def get_schedule(
    network_name: str,
    start: str = None,
    end: str = None,
    include_meta: bool = False,
    include_display: bool = False,
):
    return _schedule_payload(
        network_name, start, end, include_meta, include_display
    )
