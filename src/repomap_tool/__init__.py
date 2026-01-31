"""Repomap Tool - Standalone repository mapping with auto-regeneration."""
from .repomap_logic import RepoMap
from .watcher import RepomapWatcher

__version__ = "1.0.0"
__all__ = ["RepoMap", "RepomapWatcher"]
