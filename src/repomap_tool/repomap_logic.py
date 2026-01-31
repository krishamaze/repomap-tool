import os
import math
import time
import sqlite3
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
        except SQLITE_ERRORS:
            self.TAGS_CACHE = dict()

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
        val = self.TAGS_CACHE.get(cache_key)
        if val is not None and val.get("mtime") == file_mtime:
            return val["data"]
        data = list(self.get_tags_raw(fname, rel_fname))
        self.TAGS_CACHE[cache_key] = {"mtime": file_mtime, "data": data}
        return data

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
            query_scm_path = Path(tool_dir) / "queries" / "tree-sitter-languages" / f"tree-sitter-{lang}" / "tags.scm"
        
        if not query_scm_path.exists(): return
        query_scm = query_scm_path.read_text()
        code = self.io.read_text(fname)
        if not code: return
        tree = parser.parse(bytes(code, "utf-8"))
        query = language.query(query_scm)
        
        # In tree-sitter 0.22+, Query objects don't have .captures()
        # We use QueryCursor instead
        import tree_sitter
        cursor = tree_sitter.QueryCursor(query)
        captures = cursor.captures(tree.root_node)
        
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
        ranked_tags = self.get_ranked_tags(other_files)
        
        other_rel = sorted(set(self.get_rel_fname(f) for f in other_files))
        special = filter_important_files(other_rel)
        ranked_fnames = set(tag[0] for tag in ranked_tags)
        special = [(fn,) for fn in special if fn not in ranked_fnames]
        ranked_tags = special + ranked_tags

        num_tags = len(ranked_tags)
        low, high = 0, num_tags
        best_tree = ""; best_tokens = 0
        
        while low <= high:
            mid = (low + high) // 2
            tree = self.to_tree(ranked_tags[:mid])
            tokens = self.token_count(tree)
            if tokens <= self.max_map_tokens:
                best_tree, best_tokens = tree, tokens
                low = mid + 1
            else:
                high = mid - 1
        
        # Add guiding header for LLM agents
        header = "# Respect the existing repo structure and module boundaries.\n\n"
        return header + best_tree

    def to_tree(self, tags):
        if not tags: return ""
        cur_fname = None; cur_abs = None; lois = None; output = ""
        for tag in sorted(tags) + [(None,)]:
            this_rel = tag[0]
            if this_rel != cur_fname:
                if lois is not None:
                    output += f"\n{cur_fname}:\n{self.render_tree(cur_abs, cur_fname, lois)}"
                    lois = None
                elif cur_fname: output += f"\n{cur_fname}\n"
                if type(tag) is Tag:
                    lois = []; cur_abs = tag.fname
                cur_fname = this_rel
            if lois is not None: lois.append(tag.line)
        return "\n".join([l[:100] for l in output.splitlines()]) + "\n"

    def render_tree(self, abs_fname, rel_fname, lois):
        mtime = self.get_mtime(abs_fname)
        code = self.io.read_text(abs_fname) or ""
        if not code.endswith("\n"): code += "\n"
        context = TreeContext(rel_fname, code, color=False, line_number=False, child_context=False, last_line=False, margin=0, mark_lois=False, loi_pad=0, show_top_of_file_parent_scope=False)
        context.add_lines_of_interest(lois); context.add_context()
        return context.format()
