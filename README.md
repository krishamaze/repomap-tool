# Repomap Tool

Standalone advanced repository mapping tool with auto-regeneration on file changes.

Extracted and adapted from [Aider](https://github.com/Aider-AI/aider)'s repomap functionality.

## Features

- **Syntax-aware code analysis** using tree-sitter
- **PageRank-based file ranking** - most important files appear first
- **Auto-regeneration** - watches for file changes, regenerates after 10s idle
- **Stable output path** - writes `REPOMAP.md` and skips unchanged rewrites
- **Reference directory outline** - appends structure-only trees for ignored top-level dirs
- **LLM-optimized output** - includes header to guide AI agents

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

# Use a larger repo map token budget
repomap . --tokens 4096
```

The map generator may exceed the requested token budget by up to 10% when that preserves useful structure. It prints a `[WARN]` message when the final map is over the requested limit.

The tool will:
1. Generate an initial `REPOMAP.md` in the project root
2. Watch for file changes
3. Regenerate the map 10 seconds after the last edit
4. Skip rewriting `REPOMAP.md` when the generated content is unchanged
5. Include a compact structure-only section for ignored top-level directories

## Output Format

```
# Respect the existing repo structure and module boundaries.

src/main.py:
⋮...
│def main():
⋮...

src/utils.py:
⋮...
│class Helper:
⋮...

## Reference Directories (structure only)

_reference/
  external-tool/
    README.md
```

## License

MIT
