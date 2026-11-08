"""Background filesystem watcher that keeps the index in sync with edits.

Events are collected, de-duplicated and applied by a single worker thread after
a short quiet period (``debounce``), so an editor's save-rename-chmod burst or a
``git checkout`` touching hundreds of files becomes one re-parse per file.

On Linux (inotify) every watched directory costs a kernel watch, and a
``node_modules`` tree can contain 100k+ directories.  There the watcher
schedules each *non-ignored* directory individually instead of using a single
recursive watch.  On macOS and Windows, which provide native recursive
watching, one recursive watch is used and ignored paths are filtered in
software.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .indexer import IGNORED_DIRS, ProjectIndex

log = logging.getLogger("zerotoken.watcher")

_FILE = "file"
_DIR_ADDED = "dir_added"
_DIR_REMOVED = "dir_removed"


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "IndexWatcher"):
        self._w = watcher

    @staticmethod
    def _path(value) -> str:
        return os.fsdecode(value)

    def on_created(self, event: FileSystemEvent) -> None:
        path = self._path(event.src_path)
        self._w.enqueue(_DIR_ADDED if event.is_directory else _FILE, path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._w.enqueue(_FILE, self._path(event.src_path))

    def on_deleted(self, event: FileSystemEvent) -> None:
        path = self._path(event.src_path)
        self._w.enqueue(_DIR_REMOVED if event.is_directory else _FILE, path)

    def on_moved(self, event: FileSystemEvent) -> None:
        src, dest = self._path(event.src_path), self._path(event.dest_path)
        if event.is_directory:
            self._w.enqueue(_DIR_REMOVED, src)
            self._w.enqueue(_DIR_ADDED, dest)
        else:
            self._w.enqueue(_FILE, src)
            self._w.enqueue(_FILE, dest)


class IndexWatcher:
    """Keep a :class:`~zerotoken.indexer.ProjectIndex` fresh as files change.

    Usage::

        with IndexWatcher(index):
            ...  # edits are indexed in the background

    Args:
        index: The index to update.
        debounce: Seconds of quiet to wait before applying a batch of events.
    """

    def __init__(self, index: ProjectIndex, debounce: float = 0.4):
        self.index = index
        self.debounce = debounce
        self._observer = Observer()
        self._handler = _Handler(self)
        self._per_dir = sys.platform.startswith("linux")
        self._watches: dict[str, object] = {}
        self._pending: set[tuple[str, str]] = set()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------- #

    def start(self) -> None:
        root = str(self.index.root)
        if self._per_dir:
            self._schedule_tree(root)
        else:
            self._observer.schedule(self._handler, root, recursive=True)
        self._observer.start()
        self._worker = threading.Thread(target=self._run, name="zerotoken-watcher", daemon=True)
        self._worker.start()
        log.info("watching %s", root)

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()
        try:
            self._observer.stop()
            if self._observer.is_alive():
                self._observer.join(timeout=3)
        except RuntimeError:  # observer never started
            pass
        if self._worker is not None:
            self._worker.join(timeout=3)

    def __enter__(self) -> "IndexWatcher":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- event intake (observer thread) --------------------------------------- #

    def enqueue(self, kind: str, path: str) -> None:
        if kind == _FILE:
            if self.index.classify(path) is None:
                return
        elif self.index.is_ignored_dir(path) and not self._is_root(path):
            return
        with self._cond:
            self._pending.add((kind, path))
            self._cond.notify()

    def _is_root(self, path: str) -> bool:
        return Path(path) == self.index.root

    # -- directory watches (Linux) ---------------------------------------------- #

    def _schedule_dir(self, path: str) -> None:
        if path in self._watches:
            return
        try:
            self._watches[path] = self._observer.schedule(self._handler, path, recursive=False)
        except OSError as exc:  # inotify limit reached, dir vanished, permissions...
            log.warning("cannot watch %s: %s", path, exc)

    def _schedule_tree(self, top: str) -> None:
        for dirpath, dirnames, _ in os.walk(top, followlinks=False):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.endswith(".egg-info")]
            self._schedule_dir(dirpath)

    def _unschedule_under(self, path: str) -> None:
        prefix = path.rstrip(os.sep) + os.sep
        for watched in [w for w in self._watches if w == path or w.startswith(prefix)]:
            watch = self._watches.pop(watched)
            try:
                self._observer.unschedule(watch)
            except Exception:  # already gone
                pass

    # -- worker thread ------------------------------------------------------------ #

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._cond:
                while not self._pending and not self._stop.is_set():
                    self._cond.wait()
                if self._stop.is_set():
                    return
            # Debounce: let a burst of events settle, then take the whole batch.
            time.sleep(self.debounce)
            with self._cond:
                batch, self._pending = self._pending, set()
            try:
                self._apply(batch)
            except Exception:
                log.exception("watcher failed to apply a batch of %d events", len(batch))

    def _apply(self, batch: set[tuple[str, str]]) -> None:
        removed = sorted(p for k, p in batch if k == _DIR_REMOVED)
        files = sorted(p for k, p in batch if k == _FILE)
        added = sorted(p for k, p in batch if k == _DIR_ADDED)

        for path in removed:
            if self._per_dir:
                self._unschedule_under(path)
            n = self.index.remove_prefix(path)
            if n:
                log.info("removed %d files under %s", n, path)
        for path in files:
            if self.index.update_file(path):
                log.debug("reindexed %s", path)
        if added:
            if self._per_dir:
                for path in added:
                    self._schedule_tree(path)
            # New/moved directories: an incremental sync is cheap (mtime+size
            # comparison) and also catches files created before the watch existed.
            stats = self.index.index_directory()
            log.info("directory change: %s", stats)
