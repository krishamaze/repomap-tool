import hashlib
import os
import sys
import time
import datetime
import threading
import pathspec
import tempfile
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from .repomap_logic import (
    RepoMap, is_noise_dir, ALWAYS_SHOW_EXTENSIONS, NEVER_SHOW_EXTENSIONS
)

class RepomapWatcher(FileSystemEventHandler):
    def __init__(self, root_dir, debounce_seconds=10, map_tokens=8192):
        self.root_dir = os.path.abspath(root_dir)
        self.debounce_seconds = debounce_seconds
        self.map_tokens = map_tokens
        self.last_change_time = 0
        self.ignore_spec = self.load_gitignore()
        self.rm = RepoMap(map_tokens=map_tokens, root=self.root_dir)
        self._suppress_events = False  # Flag to ignore events during our own writes
        self._change_event = threading.Event()  # Signals that a real file change occurred

    def load_gitignore(self):
        gitignore_path = os.path.join(self.root_dir, ".gitignore")
        patterns = [".git/", ".repomap_tags_cache*", "REPOMAP.md", "venv/", "__pycache__/"]
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r") as f:
                patterns.extend(f.readlines())
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def is_ignored(self, path):
        basename = os.path.basename(path)
        if is_noise_dir(basename) or basename == "REPOMAP.md" or basename.endswith(".tmp") or basename == ".repomap.pid":
            return True

        rel_path = os.path.relpath(path, self.root_dir)
        if path.endswith(os.sep) and not rel_path.endswith(os.sep):
            rel_path += os.sep
            
        if self.ignore_spec.match_file(rel_path):
            return True

        parts = rel_path.split(os.sep)
        for part in parts:
            if is_noise_dir(part):
                return True

        for index in range(1, len(parts)):
            parent = "/".join(parts[:index]) + "/"
            if self.ignore_spec.match_file(parent):
                return True

        return False

    def _content_hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def on_any_event(self, event):
        if event.is_directory: return
        if self._suppress_events: return
        if self.is_ignored(event.src_path): return
        self.last_change_time = time.time()
        self._change_event.set()  # Wake up the run_loop

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
        print(f"[{datetime.datetime.now()}] Checking for repomap updates...")
        all_files = self.get_all_files()
        try:
            repomap_content = self.rm.get_repo_map(all_files)
        except Exception as e:
            print(f"[ERROR] Failed to generate map: {e}")
            return

        if not repomap_content:
            return

        output_path = os.path.join(self.root_dir, "REPOMAP.md")
        new_hash = self._content_hash(repomap_content)

        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                existing_hash = self._content_hash(f.read())
            if new_hash == existing_hash:
                print(f"[{datetime.datetime.now()}] No structural changes detected, keeping existing REPOMAP.md.")
                return
        
        # Suppress events while we write our own files
        self._suppress_events = True
        try:
            tmp_path = output_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(repomap_content)
            os.replace(tmp_path, output_path)
            print(f"[{datetime.datetime.now()}] Saved to REPOMAP.md")
        finally:
            self._suppress_events = False

    def run_loop(self):
        pid_file = os.path.join(self.root_dir, ".repomap.pid")
        my_pid = os.getpid()
        if os.path.exists(pid_file):
            try:
                with open(pid_file, "r") as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, 0)
                print(f"[ERROR] Another repomap process (PID {old_pid}) is already monitoring this directory. Exiting.")
                return
            except (ValueError, OSError):
                # Process not found or invalid pid file, safe to overwrite
                pass

        try:
            with open(pid_file, "w") as f:
                f.write(str(my_pid))
        except Exception as e:
            print(f"[WARN] Could not write PID file: {e}")

        print(
            f"Monitoring {self.root_dir} "
            f"(Idle timeout: {self.debounce_seconds}s, tokens: {self.map_tokens})"
        )
        # Initial update
        self.update_repomap()
        
        while True:
            # Sleep indefinitely until a real file change wakes us up
            self._change_event.wait()
            self._change_event.clear()

            # Settle-down: wait until no new changes for debounce_seconds
            while True:
                idle_for = time.time() - self.last_change_time
                remaining = self.debounce_seconds - idle_for
                if remaining <= 0:
                    break
                time.sleep(remaining)

            try:
                self.update_repomap()
            except Exception as e:
                print(f"[ERROR] Failed to update repomap: {e}")
                        
    def cleanup(self):
        pid_file = os.path.join(self.root_dir, ".repomap.pid")
        try:
            if os.path.exists(pid_file):
                with open(pid_file, "r") as f:
                    pid = int(f.read().strip())
                if pid == os.getpid():
                    os.remove(pid_file)
        except Exception:
            pass

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
