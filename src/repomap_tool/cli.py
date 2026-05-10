#!/usr/bin/env python3
"""CLI entry point for repomap tool."""
import argparse
import os
import sys
from watchdog.observers import Observer
from .watcher import RepomapWatcher


def main():
    """Main entry point for repomap command."""
    parser = argparse.ArgumentParser(description="Generate and watch an LLM-friendly repo map.")
    parser.add_argument("project_directory", help="Project directory to monitor")
    parser.add_argument(
        "--tokens",
        type=int,
        default=2048,
        help="Maximum repo map token budget (default: 2048)",
    )
    args = parser.parse_args()
    
    root = args.project_directory
    if not os.path.isdir(root):
        print(f"Error: '{root}' is not a valid directory")
        sys.exit(1)
    
    watcher = RepomapWatcher(root, map_tokens=args.tokens)
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
