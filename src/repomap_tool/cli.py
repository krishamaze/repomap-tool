#!/usr/bin/env python3
"""CLI entry point for repomap tool."""
import sys
import os
from watchdog.observers import Observer
from .watcher import RepomapWatcher


def main():
    """Main entry point for repomap command."""
    if len(sys.argv) < 2:
        print("Usage: repomap <project_directory>")
        print("       repomap .                    # Current directory")
        print("       repomap /path/to/project     # Specific path")
        sys.exit(1)
    
    root = sys.argv[1]
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a valid directory")
        sys.exit(1)
    
    watcher = RepomapWatcher(root)
    observer = Observer()
    observer.schedule(watcher, root, recursive=True)
    observer.start()
    
    try:
        watcher.run_loop()
    except KeyboardInterrupt:
        print("\nStopping repomap watcher...")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
