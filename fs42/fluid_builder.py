import logging
import sqlite3
import sys
import os

sys.path.append(os.getcwd())

from fs42.fluid_statements import FluidStatements
from fs42.media_processor import MediaProcessor
from fs42.station_manager import StationManager
from fs42.database import connect

class FluidBuilder:
    # Version 4 invalidates boundaries generated before the authored-chapter
    # fast path and tighter generic-boundary validation were introduced.
    COMMERCIAL_BREAK_DETECTOR_VERSION = 4

    def __init__(self, db_path=None):
        if db_path is None:
            self.db_path = StationManager().server_conf["db_path"]

        self._l = logging.getLogger("FLUID")
        connection = connect(self.db_path)
        try:
            FluidStatements.init_db(connection)
            connection.commit()
        finally:
            connection.close()

    def scan_file_cache(self, content_dir, media_filter="video"):
        connection = connect(self.db_path)
        try:
            # read all the files in the content dir
            self._l.info(f"Fluid file cache scan - reading {content_dir} with media_filter={media_filter}")
            file_list = MediaProcessor.rich_find_media(content_dir, media_filter)
            self._l.info(f"Comparing cache against {len(file_list)} files")
            # add any that aren't there yet
            FluidStatements.iterate_file_entries(connection, file_list)
            self._l.info("Checking file meta for stale entries.")
        finally:
            connection.close()

    def check_file_cache(self, full_path):
        connection = connect(self.db_path)
        try:
            results = FluidStatements.check_file_cache(connection, full_path)
        finally:
            connection.close()
        return results

    def trim_file_cache(self, from_time):
        connection = connect(self.db_path)
        try:
            self._l.info("Trimming fluid file cache")
            FluidStatements.trim_file_entries(connection, from_time)
        finally:
            connection.close()

    def scan_breaks(self, dir_path):
        connection = connect(self.db_path)
        try:
            self._l.info(f"Scanning directory {dir_path} for breaks")
            if not os.path.isdir(dir_path):
                raise FileNotFoundError(f"Directory does not exist {dir_path}")
            dir_path = os.path.realpath(dir_path)
            file_list = MediaProcessor._rfind_media(dir_path)

            # Check the cache because we require the duration to prococess.
            file_paths = [os.path.realpath(file) for file in file_list]
            cached_files = {}
            for path in file_paths:
                cached = FluidStatements.check_file_cache(connection, path)
                if cached:
                    cached_files[path] = cached

            for file in file_list:
                rfp = os.path.realpath(file)
                if rfp in cached_files:
                    cached = cached_files[rfp]
                    if FluidStatements.get_break_points(connection, rfp):
                        self._l.info(f"Breaks already exists for {rfp}")
                    else:
                        breaks = MediaProcessor.black_detect(rfp, cached.duration)
                        FluidStatements.add_break_points(connection, rfp, breaks)
                else:
                    self._l.warning(f"{rfp} is not in catalog cache - not adding break points.")
            connection.commit()
        finally:
            connection.close()

    def get_breaks(self, full_path):
        #fname = os.path.realpath(fname)
        connection = connect(self.db_path)
        try:
            results = FluidStatements.get_break_points(connection, full_path)
        finally:
            connection.close()
        return results

    def scan_chapters(self, dir_path):
        connection = connect(self.db_path)
        try:
            self._l.info(f"Scanning directory {dir_path} for chapters")
            if not os.path.isdir(dir_path):
                raise FileNotFoundError(f"Directory does not exist {dir_path}")
            dir_path = os.path.realpath(dir_path)
            file_list = MediaProcessor._rfind_media(dir_path)

            # Check the cache because we require the duration to process.
            file_paths = [os.path.realpath(file) for file in file_list]
            cached_files = {}
            for path in file_paths:
                cached = FluidStatements.check_file_cache(connection, path)
                if cached:
                    cached_files[path] = cached

            for file in file_list:
                rfp = os.path.realpath(file)
                if rfp in cached_files:
                    cached = cached_files[rfp]
                    if FluidStatements.get_chapter_points(connection, rfp):
                        self._l.info(f"Chapters already exist for {rfp}")
                    else:
                        chapters = MediaProcessor.chapter_detect(rfp, cached.duration)
                        if chapters:
                            FluidStatements.add_chapter_points(connection, rfp, chapters)
                        else:
                            self._l.info(f"No chapters found in {rfp}")
                else:
                    self._l.warning(f"{rfp} is not in catalog cache - not adding chapter points.")
            connection.commit()
        finally:
            connection.close()

    def get_chapters(self, full_path):
        connection = connect(self.db_path)
        try:
            results = FluidStatements.get_chapter_points(connection, full_path)
        finally:
            connection.close()
        return results

    def scan_chapters_for_entries(self, entries):
        """Scan feature files for conservative commercial break boundaries.

        Generic container chapters are scene navigation, not reliable ad
        markers. A sustained fade to black is stronger evidence of an act
        boundary, so only those transitions are persisted for scheduling.
        Commercials and bumps are intentionally never analyzed or trimmed.
        """
        connection = connect(self.db_path)
        try:
            total = len(entries)
            for index, entry in enumerate(entries, start=1):
                if hasattr(entry, 'realpath') and entry.realpath:
                    # Bumps are never analyzed. Feature scans below reuse only
                    # results from this detector version and exact file state.
                    if getattr(entry, "content_type", "feature") != "feature":
                        FluidStatements.add_chapter_points(
                            connection, entry.realpath, []
                        )
                        connection.commit()
                        continue
                    stat = os.stat(entry.realpath)
                    cached = FluidStatements.get_commercial_break_scan(
                        connection,
                        entry.realpath,
                        self.COMMERCIAL_BREAK_DETECTOR_VERSION,
                        stat.st_size,
                        stat.st_mtime_ns,
                    )
                    if cached is not None:
                        FluidStatements.add_chapter_points(
                            connection, entry.realpath, cached
                        )
                        self._l.info(
                            "Commercial boundaries %s/%s cached: %s",
                            index,
                            total,
                            entry.realpath,
                        )
                        connection.commit()
                        continue
                    self._l.info(
                        "Commercial boundaries %s/%s scanning: %s",
                        index,
                        total,
                        entry.realpath,
                    )
                    chapter_segments = MediaProcessor.chapter_detect(
                        entry.realpath,
                        entry.duration,
                    )
                    # Explicitly authored act/break chapters are already
                    # trustworthy and need no video decode. Generic navigation
                    # chapters still require nearby sustained black evidence.
                    safe_segments = MediaProcessor.safe_commercial_segments(
                        [], entry.duration, chapter_segments
                    )
                    if not safe_segments:
                        black_segments = MediaProcessor.black_detect_at_chapters(
                            entry.realpath,
                            entry.duration,
                            chapter_segments,
                            black_min_duration=0.35,
                        )
                        safe_segments = MediaProcessor.safe_commercial_segments(
                            black_segments,
                            entry.duration,
                            chapter_segments,
                        )
                    FluidStatements.add_commercial_break_scan(
                        connection,
                        entry.realpath,
                        self.COMMERCIAL_BREAK_DETECTOR_VERSION,
                        stat.st_size,
                        stat.st_mtime_ns,
                        safe_segments,
                    )
                    FluidStatements.add_chapter_points(
                        connection, entry.realpath, safe_segments
                    )
                    # Preserve progress across container updates or cancelled
                    # rebuilds. At worst, only the currently-scanning file
                    # needs to be retried.
                    connection.commit()
                    if safe_segments:
                        self._l.info(
                            "Added %s safe commercial segments for %s",
                            len(safe_segments),
                            entry.realpath,
                        )
        finally:
            connection.close()


if __name__ == "__main__":
    logging.basicConfig(format="%(levelname)s:%(name)s:%(message)s", level=logging.INFO)
    builder = FluidBuilder()
    # builder.scan_file_cache("catalog/nbc_content/")
    # exists = builder.check_file_cache("FieldStation42/catalog/public_domain/bextra/post-black.mov")
    builder.scan_breaks("catalog/public_domain/feature/sub/a/")
