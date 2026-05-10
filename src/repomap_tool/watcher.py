import hashlib
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
        patterns = [".git/", ".repomap_tags_cache*", "REPOMAP.md", "venv/", "__pycache__/"]
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r") as f:
                patterns.extend(f.readlines())
        return pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def is_ignored(self, path):
        rel_path = os.path.relpath(path, self.root_dir)
        if path.endswith(os.sep) and not rel_path.endswith(os.sep):
            rel_path += os.sep
        if self.ignore_spec.match_file(rel_path):
            return True

        parts = rel_path.split(os.sep)
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index]) + "/"
            if self.ignore_spec.match_file(parent):
                return True

        return False

    def _content_hash(self, text: str) -> str:
        return hashlib.md5(text.encode("utf-8")).hexdigest()

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

    def get_ignored_top_dirs(self):
        ignored = []
        try:
            for entry in os.scandir(self.root_dir):
                if entry.name.startswith(".") or entry.name in {"__pycache__", "venv"}:
                    continue
                if entry.is_dir() and self.is_ignored(entry.path + "/"):
                    ignored.append(entry.path)
        except PermissionError:
            pass
        return ignored

    def render_dir_tree(self, dir_path: str, max_depth: int = 3) -> str:
        lines = []
        rel_base = os.path.relpath(dir_path, self.root_dir)
        lines.append(f"{rel_base}/")

        def render_children(current_path: str, depth: int):
            if depth >= max_depth:
                return

            try:
                entries = sorted(os.scandir(current_path), key=lambda entry: entry.name)
            except PermissionError:
                return

            dirs = [
                entry for entry in entries
                if entry.is_dir() and not entry.name.startswith(".")
            ]
            files = [entry for entry in entries if entry.is_file()]

            for entry in dirs:
                indent = "  " * (depth + 1)
                lines.append(f"{indent}{entry.name}/")
                render_children(entry.path, depth + 1)

            subindent = "  " * (depth + 1)
            shown_files = files[:20]
            for entry in shown_files:
                lines.append(f"{subindent}{entry.name}")
            if len(files) > 20:
                lines.append(f"{subindent}... ({len(files) - 20} more files)")

        render_children(dir_path, 0)

        return "\n".join(lines)

    def update_repomap(self):
        print(f"[{datetime.datetime.now()}] Generating new repomap...")
        all_files = self.get_all_files()
        repomap_content = self.rm.get_repo_map(all_files)

        ignored_dirs = self.get_ignored_top_dirs()
        if ignored_dirs:
            tier2_lines = ["\n\n## Reference Directories (structure only)\n"]
            for dir_path in sorted(ignored_dirs):
                tier2_lines.append(self.render_dir_tree(dir_path))
                tier2_lines.append("")
            repomap_content += "\n".join(tier2_lines)

        output_path = os.path.join(self.root_dir, "REPOMAP.md")
        new_hash = self._content_hash(repomap_content)
        if os.path.exists(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                existing_hash = self._content_hash(f.read())
            if new_hash == existing_hash:
                print(f"[{datetime.datetime.now()}] No structural changes, skipping write.")
                return
        
        # Suppress events while we write our own files
        self._suppress_events = True
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(repomap_content)
            print(f"[{datetime.datetime.now()}] Saved to REPOMAP.md")
        finally:
            self._suppress_events = False

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
