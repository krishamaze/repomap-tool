import os
import sys
import time
import datetime
import threading
import pathspec
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from .repomap_logic import RepoMap

class RepomapWatcher(FileSystemEventHandler):
    def __init__(self, root_dir, debounce_seconds=10, map_tokens=2048):
        self.root_dir = os.path.abspath(root_dir)
        self.debounce_seconds = debounce_seconds
        self.map_tokens = map_tokens
        self.last_change_time = 0
        self.pending_update = False
        self.ignore_spec = self.load_gitignore()
        self.rm = RepoMap(map_tokens=map_tokens, root=self.root_dir)
        self.lock = threading.Lock()
        self._suppress_events = False  # Flag to ignore events during our own writes

    def load_gitignore(self):
        gitignore_path = os.path.join(self.root_dir, ".gitignore")
        # Use explicit pattern for REPOMAP files (handles spaces in filename)
        patterns = [".git/", ".repomap_tags_cache*", "REPOMAP.V *", "REPOMAP.V*", "venv/", "__pycache__/"]
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r") as f:
                patterns.extend(f.readlines())
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def is_ignored(self, path):
        rel_path = os.path.relpath(path, self.root_dir)
        basename = os.path.basename(path)
        # Explicitly check for our output files
        if basename.startswith("REPOMAP.V") and basename.endswith(".TXT"):
            return True
        return self.ignore_spec.match_file(rel_path)

    def on_any_event(self, event):
        if event.is_directory: return
        if self._suppress_events: return  # Ignore events while we are writing
        if self.is_ignored(event.src_path): return
        with self.lock:
            self.last_change_time = time.time()
            self.pending_update = True

    def get_all_files(self):
        files = []
        for root, dirs, filenames in os.walk(self.root_dir):
            # Prune ignored directories
            dirs[:] = [d for d in dirs if not self.is_ignored(os.path.join(root, d) + "/")]
            for f in filenames:
                full_path = os.path.join(root, f)
                if not self.is_ignored(full_path) and os.path.isfile(full_path):
                    files.append(full_path)
        return files

    def update_repomap(self):
        print(f"[{datetime.datetime.now()}] Generating new repomap...")
        all_files = self.get_all_files()
        repomap_content = self.rm.get_repo_map(all_files)
        
        timestamp = datetime.datetime.now().strftime("%y%m%d_%H%M%S")
        new_filename = f"REPOMAP.V {timestamp}.TXT"
        new_path = os.path.join(self.root_dir, new_filename)
        
        # Suppress events while we write our own files
        self._suppress_events = True
        try:
            with open(new_path, "w", encoding="utf-8") as f:
                f.write(repomap_content)
            print(f"[{datetime.datetime.now()}] Saved to {new_filename}")
            self.cleanup_old_versions()
        finally:
            self._suppress_events = False

    def cleanup_old_versions(self):
        files = [f for f in os.listdir(self.root_dir) if f.startswith("REPOMAP.V ") and f.endswith(".TXT")]
        files.sort(reverse=True) # Newest first
        if len(files) > 2:
            for old_file in files[2:]:
                os.remove(os.path.join(self.root_dir, old_file))
                print(f"[CLEANUP] Deleted old version: {old_file}")

    def run_loop(self):
        print(
            f"Monitoring {self.root_dir} "
            f"(Idle timeout: {self.debounce_seconds}s, tokens: {self.map_tokens})"
        )
        # Initial update
        self.update_repomap()
        
        while True:
            time.sleep(1)
            with self.lock:
                if self.pending_update and (time.time() - self.last_change_time >= self.debounce_seconds):
                    self.pending_update = False
                    try:
                        self.update_repomap()
                    except Exception as e:
                        print(f"[ERROR] Failed to update repomap: {e}")

if __name__ == "__main__":
    root = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    watcher = RepomapWatcher(root)
    observer = Observer()
    observer.schedule(watcher, root, recursive=True)
    observer.start()
    try:
        watcher.run_loop()
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
