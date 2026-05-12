# Repomap Tool

Standalone advanced repository mapping tool with auto-regeneration on file changes.

Extracted and adapted from [Aider](https://github.com/Aider-AI/aider)'s repomap functionality.

## Features

- **Hybrid tree output** — unified directory hierarchy merging structural skeleton with semantic code snippets; directories covered by tree-sitter show deep symbol context, uncovered directories show full file listings
- **Syntax-aware code analysis** using tree-sitter (30+ languages)
- **PageRank-based file ranking** — most important files surface first
- **Event-driven watcher** — zero CPU usage while idle; wakes only on real file changes, then waits for a 10-second settle window before regenerating
- **Token-budget aware** — binary-search fits the map within your limit; short-circuits immediately if the bare structure already exceeds the budget
- **Persistent AST cache** — tree-sitter renders are cached by file + mtime; unchanged files are never re-parsed across binary-search iterations
- **Atomic writes** — `REPOMAP.md` is written via a temp file + `os.replace` to prevent corruption during concurrent reads
- **PID lock** — prevents two `repomap` instances from racing on the same directory
- **Startup setup wizard** — offers to gitignore `REPOMAP.md` and integrate with `AGENTS.md` on first run

## Installation

```bash
pipx install git+https://github.com/krishamaze/repomap-tool
```

Or with pip:
```bash
pip install git+https://github.com/krishamaze/repomap-tool
```

## Usage

```bash
# Monitor current directory
repomap .

# Monitor specific project
repomap /path/to/your/project

# Custom token budget (default: 8192)
repomap . --tokens 16384

# Skip the first-run setup prompts (useful for CI)
repomap . --no-setup
```

On first run the tool will prompt you to:
1. Add `REPOMAP.md` to `.gitignore` (recommended — it is auto-generated)
2. Integrate with `AGENTS.md` — prepend a living-doc reminder, or symlink `AGENTS.md → REPOMAP.md`

After setup it will:
1. Generate an initial `REPOMAP.md` in the project root
2. Sleep with zero CPU until the OS signals a file change
3. Wait for 10 seconds of inactivity (settle window)
4. Regenerate the map and write only if the content changed

## Output Format

The map is a single unified tree. Directories where tree-sitter found symbols show deep code context; directories without coverage show the raw file listing so you always know the full structure.

```
# Follow the existing repo structure and module boundaries unless the task requires improving them.

src/
  repomap_tool/
    cli.py:
    ⋮
    │def main():
    ⋮
    repomap_logic.py:
    ⋮
    │class RepoMap:
    │    def get_repo_map(self, other_files):
    ⋮
    queries/
      tree-sitter-language-pack/
        python-tags.scm
        rust-tags.scm
        ...
```

The map may exceed the requested token budget by up to 10% when that preserves useful structure. A `[WARN]` is printed when the final map is over the limit.

## License

MIT
