import os
import math
import time
import sqlite3
import shutil
import warnings
import tiktoken
from collections import Counter, defaultdict, namedtuple
from pathlib import Path
from diskcache import Cache
from grep_ast import TreeContext, filename_to_lang
from pygments.lexers import guess_lexer_for_filename
from pygments.token import Token
from tqdm import tqdm

# tree_sitter is throwing a FutureWarning
warnings.simplefilter("ignore", category=FutureWarning)
from grep_ast.tsl import USING_TSL_PACK, get_language, get_parser

Tag = namedtuple("Tag", "rel_fname fname line name kind".split())
SQLITE_ERRORS = (sqlite3.OperationalError, sqlite3.DatabaseError, OSError)
CACHE_VERSION = 3
if USING_TSL_PACK:
    CACHE_VERSION = 4
SOFT_TOKEN_OVERAGE = 0.10
REPOMAP_HEADER = (
    "# Follow the existing repo structure and module boundaries "
    "unless the task requires improving them.\n\n"
)

# ── Noise directory patterns ──────────────────────────────────────────────────
# Matched by basename — applied anywhere in the tree.
# Primary filter for render_full_tree(); gitignore is secondary.
NOISE_DIRS: frozenset = frozenset({
    # Version control internals
    ".git", ".svn", ".hg", ".fossil",
    # Python environments & caches
    "venv", ".venv", "env", ".env", "virtualenv", ".virtualenv",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".dmypy",
    ".tox", ".nox", ".eggs", "site-packages", "htmlcov",
    # JavaScript / TypeScript
    "node_modules", ".next", ".nuxt", ".svelte-kit", ".turbo",
    ".parcel-cache", ".vite", ".angular", "storybook-static",
    # Rust
    "target",
    # Java / Kotlin
    ".gradle", ".m2", "out", "classes",
    # Ruby
    ".bundle",
    # .NET / C#
    "obj", ".vs", "packages",
    # C / C++
    "CMakeFiles", "_build",
    # Elixir / Erlang
    "deps",
    # Haskell
    ".stack-work", "dist-newstyle",
    # Swift / iOS
    "DerivedData", "Pods", ".swiftpm", "Carthage", ".build",
    # Terraform
    ".terraform",
    # Nix
    "result",
    # IDE (JetBrains; .vscode intentionally excluded — useful for agents)
    ".idea", ".fleet",
    # General cache / temp / coverage
    ".cache", "tmp", "temp", ".nyc_output", "coverage", "logs",
    # Build artifact dirs (ambiguous but almost always output)
    "dist", "build",
    # Vendored dependencies (Rust/Go/PHP/Ruby all use this as a deps sink)
    "vendor",
})

# Directory name suffix patterns (e.g. mypackage.egg-info/)
NOISE_DIR_SUFFIXES: tuple = (".egg-info", ".dist-info")

# Directory name prefix patterns (e.g. bazel-bin/, cmake-build-release/)
NOISE_DIR_PREFIXES: tuple = ("bazel-", "cmake-build-", ".repomap_tags_cache")

# File extensions always shown in the tree, at any depth (documentation)
ALWAYS_SHOW_EXTENSIONS: frozenset = frozenset({
    ".md", ".mdx", ".rst", ".txt", ".adoc",
})

# File extensions never shown in the tree (binaries, compiled output, assets)
NEVER_SHOW_EXTENSIONS: frozenset = frozenset({
    ".pyc", ".pyo", ".class", ".o", ".a", ".so", ".dll", ".exe",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
    ".map", ".lock", ".bin", ".dat",
})


def is_noise_dir(name: str) -> bool:
    """Return True if a directory basename matches known noise patterns."""
    if name in NOISE_DIRS:
        return True
    for suffix in NOISE_DIR_SUFFIXES:
        if name.endswith(suffix):
            return True
    for prefix in NOISE_DIR_PREFIXES:
        if name.startswith(prefix):
            return True
    return False


ROOT_IMPORTANT_FILES = [
    ".gitignore", ".gitattributes", "README", "README.md", "README.txt", "README.rst",
    "CONTRIBUTING", "CONTRIBUTING.md", "LICENSE", "LICENSE.md", "LICENSE.txt",
    "requirements.txt", "Pipfile", "pyproject.toml", "setup.py", "package.json",
    "package-lock.json", "yarn.lock", "go.mod", "Cargo.toml", "Dockerfile",
    "docker-compose.yml", ".env", ".env.example", "tsconfig.json", "jsconfig.json"
]
NORMALIZED_ROOT_IMPORTANT_FILES = set(os.path.normpath(path) for path in ROOT_IMPORTANT_FILES)

def is_important(file_path):
    file_name = os.path.basename(file_path)
    dir_name = os.path.normpath(os.path.dirname(file_path))
    normalized_path = os.path.normpath(file_path)
    ext = os.path.splitext(file_name)[1].lower()
    
    if ext in ALWAYS_SHOW_EXTENSIONS:
        return True
    if dir_name == os.path.normpath(".github/workflows") and file_name.endswith(".yml"):
        return True
    return normalized_path in NORMALIZED_ROOT_IMPORTANT_FILES

def filter_important_files(file_paths):
    return list(filter(is_important, file_paths))

class MockModel:
    def __init__(self, model_name="gpt-4o"):
        try:
            self.encoding = tiktoken.encoding_for_model(model_name)
        except Exception:
            self.encoding = tiktoken.get_encoding("cl100k_base")

    def token_count(self, text):
        return len(self.encoding.encode(text, disallowed_special=()))

class ConsoleIO:
    def tool_output(self, msg): print(f"[INFO] {msg}")
    def tool_warning(self, msg): print(f"[WARN] {msg}")
    def tool_error(self, msg): print(f"[ERROR] {msg}")
    def read_text(self, fname):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return None

class RepoMap:
    TAGS_CACHE_DIR = f".repomap_tags_cache.v{CACHE_VERSION}"
    warned_files = set()

    def __init__(self, map_tokens=1024, root=None, verbose=False):
        self.io = ConsoleIO()
        self.verbose = verbose
        if not root:
            root = os.getcwd()
        self.root = root
        self.load_tags_cache()
        self.max_map_tokens = map_tokens
        self.main_model = MockModel()
        self.tree_cache = {}
        self.tree_context_cache = {}
        self.map_cache = {}
        self.map_processing_time = 0
        self.last_map = None

    def token_count(self, text):
        return self.main_model.token_count(text)

    def load_tags_cache(self):
        path = Path(self.root) / self.TAGS_CACHE_DIR
        try:
            self.TAGS_CACHE = Cache(path)
        except SQLITE_ERRORS as err:
            self.tags_cache_error(err)

    def tags_cache_error(self, original_error=None):
        if self.verbose and original_error:
            self.io.tool_warning(f"Tags cache error: {original_error}")

        if isinstance(getattr(self, "TAGS_CACHE", None), dict):
            return

        path = Path(self.root) / self.TAGS_CACHE_DIR
        try:
            if path.exists():
                shutil.rmtree(path)

            new_cache = Cache(path)
            new_cache["test"] = "test"
            _ = new_cache["test"]
            del new_cache["test"]
            self.TAGS_CACHE = new_cache
            return
        except SQLITE_ERRORS as err:
            self.io.tool_warning(
                f"Unable to use tags cache at {path}, falling back to memory cache"
            )
            if self.verbose:
                self.io.tool_warning(f"Cache recreation error: {err}")

            self.TAGS_CACHE = dict()

    def save_tags_cache(self):
        pass

    def get_rel_fname(self, fname):
        try:
            return os.path.relpath(fname, self.root)
        except ValueError:
            return fname

    def get_mtime(self, fname):
        try:
            return os.path.getmtime(fname)
        except FileNotFoundError:
            return None

    def get_tags(self, fname, rel_fname):
        file_mtime = self.get_mtime(fname)
        if file_mtime is None: return []
        cache_key = fname
        try:
            val = self.TAGS_CACHE.get(cache_key)
        except SQLITE_ERRORS as err:
            self.tags_cache_error(err)
            val = self.TAGS_CACHE.get(cache_key)

        if val is not None and val.get("mtime") == file_mtime:
            try:
                return self.TAGS_CACHE[cache_key]["data"]
            except SQLITE_ERRORS as err:
                self.tags_cache_error(err)
                return self.TAGS_CACHE[cache_key]["data"]

        data = list(self.get_tags_raw(fname, rel_fname))
        try:
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
            self.save_tags_cache()
        except SQLITE_ERRORS as err:
            self.tags_cache_error(err)
            self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}

        return data

    def _run_captures(self, query, node):
        if hasattr(query, "captures"):
            return query.captures(node)

        from tree_sitter import QueryCursor

        cursor = QueryCursor(query)
        return cursor.captures(node)

    def get_tags_raw(self, fname, rel_fname):
        lang = filename_to_lang(fname)
        if not lang: return
        try:
            language = get_language(lang)
            parser = get_parser(lang)
        except Exception: return
        
        # Look for queries in the tool's queries directory
        tool_dir = os.path.dirname(os.path.abspath(__file__))
        query_scm_path = Path(tool_dir) / "queries" / "tree-sitter-language-pack" / f"{lang}-tags.scm"
        if not query_scm_path.exists():
            query_scm_path = Path(tool_dir) / "queries" / "tree-sitter-languages" / f"{lang}-tags.scm"
        
        if not query_scm_path.exists(): return
        query_scm = query_scm_path.read_text()
        code = self.io.read_text(fname)
        if not code: return
        tree = parser.parse(bytes(code, "utf-8"))
        try:
            from tree_sitter import Query

            query = Query(language, query_scm)
        except (ImportError, TypeError):
            query = language.query(query_scm)
        
        captures = self._run_captures(query, tree.root_node)
        
        saw = set()
        all_nodes = []
        # TSL pack seems to return a dict of {tag: [nodes]}
        if isinstance(captures, dict):
            for tag, nodes in captures.items():
                all_nodes += [(node, tag) for node in nodes]
        else:
            all_nodes = list(captures)

        for node, tag in all_nodes:
            if tag.startswith("name.definition."): kind = "def"
            elif tag.startswith("name.reference."): kind = "ref"
            else: continue
            saw.add(kind)
            yield Tag(rel_fname=rel_fname, fname=fname, name=node.text.decode("utf-8"), kind=kind, line=node.start_point[0])

        if "ref" in saw or "def" not in saw: return
        try:
            lexer = guess_lexer_for_filename(fname, code)
        except Exception: return
        tokens = [token[1] for token in lexer.get_tokens(code) if token[0] in Token.Name]
        for token in tokens:
            yield Tag(rel_fname=rel_fname, fname=fname, name=token, kind="ref", line=-1)

    def get_ranked_tags(self, other_fnames):
        import networkx as nx
        defines = defaultdict(set); references = defaultdict(list); definitions = defaultdict(set)
        fnames = sorted(other_fnames)
        if not fnames: return []
        
        for fname in fnames:
            if not Path(fname).is_file(): continue
            rel_fname = self.get_rel_fname(fname)
            tags = self.get_tags(fname, rel_fname)
            for tag in tags:
                if tag.kind == "def":
                    defines[tag.name].add(rel_fname)
                    definitions[(rel_fname, tag.name)].add(tag)
                elif tag.kind == "ref":
                    references[tag.name].append(rel_fname)

        if not references: references = {k: list(v) for k, v in defines.items()}
        idents = set(defines.keys()).intersection(set(references.keys()))
        G = nx.MultiDiGraph()
        for ident in defines.keys():
            if ident not in references:
                for definer in defines[ident]:
                    G.add_edge(definer, definer, weight=0.1, ident=ident)
        
        for ident in idents:
            definers = defines[ident]
            mul = 1.0
            if ("_" in ident or "-" in ident or any(c.isupper() for c in ident)) and len(ident) >= 8: mul *= 10
            if ident.startswith("_"): mul *= 0.1
            if len(defines[ident]) > 5: mul *= 0.1
            for referencer, num_refs in Counter(references[ident]).items():
                for definer in definers:
                    G.add_edge(referencer, definer, weight=mul * math.sqrt(num_refs), ident=ident)

        try:
            if G.nodes:
                ranked = nx.pagerank(G, weight="weight")
            else:
                ranked = {}
        except Exception:
            ranked = {}

        ranked_definitions = defaultdict(float)
        for src in G.nodes:
            src_rank = ranked.get(src, 1.0)
            out_edges = G.out_edges(src, data=True)
            total_weight = sum(data["weight"] for _src, _dst, data in out_edges)
            if total_weight == 0:
                # If no out-edges, maybe it's a sink. Distributed its rank? 
                # For now just continue.
                continue
            for _src, dst, data in out_edges:
                data["rank"] = src_rank * data["weight"] / total_weight
                ranked_definitions[(dst, data["ident"])] += data["rank"]

        sorted_defs = sorted(ranked_definitions.items(), reverse=True, key=lambda x: (x[1], x[0]))
        ranked_tags = []
        for (fname, ident), rank in sorted_defs:
            ranked_tags += list(definitions.get((fname, ident), []))
        
        included_fnames = set(rt[0] for rt in ranked_tags)
        top_rank = sorted([(rank, node) for (node, rank) in ranked.items()], reverse=True)
        for rank, fname in top_rank:
            if fname not in included_fnames:
                ranked_tags.append((fname,))
                included_fnames.add(fname)
        
        rel_other = set(self.get_rel_fname(f) for f in other_fnames)
        for f in rel_other:
            if f not in included_fnames:
                ranked_tags.append((f,))
        return ranked_tags

    def get_repo_map(self, other_files):
        if self.max_map_tokens <= 0 or not other_files: return ""
        
        header = REPOMAP_HEADER
        soft_limit = math.ceil(self.max_map_tokens * (1 + SOFT_TOKEN_OVERAGE))

        # 1. Short-circuit: If the pure structural tree exceeds the budget, skip all semantic parsing
        base_tree = self.to_tree([], other_files)
        base_tokens = self.token_count(header + base_tree)
        if base_tokens > self.max_map_tokens:
            self.io.tool_warning(
                f"Map is {base_tokens} tokens, over requested {self.max_map_tokens} token limit."
            )
            return header + base_tree

        # 2. Get ranked tags
        ranked_tags = self.get_ranked_tags(other_files)

        if not hasattr(self, '_render_cache'):
            self._render_cache = {}

        other_rel = sorted(set(self.get_rel_fname(f) for f in other_files))
        special_fnames = filter_important_files(other_rel)
        special_set = set(special_fnames)
        ranked_tags = [tag for tag in ranked_tags if tag[0] not in special_set]
        ranked_tags = [(fn,) for fn in special_fnames] + ranked_tags

        num_tags = len(ranked_tags)
        low, high = 0, num_tags
        soft_limit = math.ceil(self.max_map_tokens * (1 + SOFT_TOKEN_OVERAGE))
        header = REPOMAP_HEADER
        best_tree = ""
        best_tokens = self.token_count(header)
        
        while low <= high:
            mid = (low + high) // 2
            tree = self.to_tree(ranked_tags[:mid], other_files)
            tokens = self.token_count(header + tree)
            if tokens <= soft_limit:
                best_tree, best_tokens = tree, tokens
                low = mid + 1
            else:
                high = mid - 1
                
        if not best_tree:
            best_tree = base_tree
            best_tokens = base_tokens
        
        if best_tokens > self.max_map_tokens:
            self.io.tool_warning(
                f"Map is {best_tokens} tokens, over requested {self.max_map_tokens} token limit."
            )

        return header + best_tree

    def to_tree(self, tags, all_files):
        def make_node():
            return {"dirs": {}, "files": set(), "covered": False}

        def normalize_rel_fname(fname):
            if not fname:
                return None
            if os.path.isabs(fname):
                rel_fname = self.get_rel_fname(fname)
            else:
                rel_fname = fname
            rel_fname = os.path.normpath(rel_fname)
            if rel_fname in ("", ".", "..") or rel_fname.startswith(f"..{os.sep}"):
                return None
            return rel_fname

        root = make_node()
        file_to_tags = defaultdict(list)
        file_to_abs = {}
        covered_files = set()

        for tag in tags:
            rel_fname = normalize_rel_fname(tag[0])
            if not rel_fname:
                continue
            covered_files.add(rel_fname)
            file_to_tags.setdefault(rel_fname, [])
            if type(tag) is Tag:
                file_to_tags[rel_fname].append(tag.line)
                file_to_abs[rel_fname] = tag.fname

        all_rel_fnames = set()
        for fname in all_files:
            rel_fname = normalize_rel_fname(fname)
            if rel_fname:
                all_rel_fnames.add(rel_fname)

        for rel_fname in sorted(all_rel_fnames | covered_files):
            parts = rel_fname.split(os.sep)
            node = root
            for part in parts[:-1]:
                node = node["dirs"].setdefault(part, make_node())
            node["files"].add(parts[-1])

        for rel_fname in covered_files:
            parts = rel_fname.split(os.sep)
            node = root
            node["covered"] = True
            for part in parts[:-1]:
                node = node["dirs"].setdefault(part, make_node())
                node["covered"] = True

        lines = []

        def render_file(rel_fname, depth):
            indent = "  " * depth
            file_name = os.path.basename(rel_fname)
            lois = sorted(set(file_to_tags.get(rel_fname, [])))
            if not lois:
                lines.append(f"{indent}{file_name}")
                return

            lines.append(f"{indent}{file_name}:")
            abs_fname = file_to_abs.get(rel_fname)
            if not abs_fname:
                return
            for line in self.render_tree(abs_fname, rel_fname, lois).splitlines():
                lines.append(f"{indent}{line}")

        def render_dir(node, rel_dir, depth):
            dirs = node["dirs"]
            files = node["files"]
            is_leaf = not dirs
            shown_leaf_files = 0

            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext in NEVER_SHOW_EXTENSIONS:
                    continue

                rel_fname = os.path.join(rel_dir, name) if rel_dir else name
                if node["covered"]:
                    if rel_fname not in covered_files:
                        continue
                    render_file(rel_fname, depth)
                elif is_leaf:
                    if shown_leaf_files >= 20:
                        remaining = len([f for f in files if os.path.splitext(f)[1].lower() not in NEVER_SHOW_EXTENSIONS]) - shown_leaf_files
                        lines.append(f"{'  ' * depth}... ({remaining} more files)")
                        break
                    render_file(rel_fname, depth)
                    shown_leaf_files += 1

            for name in sorted(dirs):
                child = dirs[name]
                child_rel = os.path.join(rel_dir, name) if rel_dir else name
                lines.append(f"{'  ' * depth}{name}/")
                render_dir(child, child_rel, depth + 1)

        render_dir(root, "", 0)

        if not lines:
            return ""
        return "\n".join([line[:100] for line in lines]) + "\n"

    def render_tree(self, abs_fname, rel_fname, lois):
        mtime = self.get_mtime(abs_fname)
        cache_key = (abs_fname, mtime, tuple(lois))
        
        if not hasattr(self, '_render_cache'):
            self._render_cache = {}
            
        if cache_key in self._render_cache:
            return self._render_cache[cache_key]

        code = self.io.read_text(abs_fname) or ""
        if not code.endswith("\n"): code += "\n"
        context = TreeContext(rel_fname, code, color=False, line_number=False, child_context=False, last_line=False, margin=0, mark_lois=False, loi_pad=0, show_top_of_file_parent_scope=False)
        context.add_lines_of_interest(lois); context.add_context()
        res = context.format()

        self._render_cache[cache_key] = res
        return res
