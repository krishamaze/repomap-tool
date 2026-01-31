# Repomap Tool

Standalone advanced repository mapping tool with auto-regeneration on file changes.

Extracted and adapted from [Aider](https://github.com/Aider-AI/aider)'s repomap functionality.

## Features

- **Syntax-aware code analysis** using tree-sitter
- **PageRank-based file ranking** - most important files appear first
- **Auto-regeneration** - watches for file changes, regenerates after 10s idle
- **Version management** - keeps last 2 versions, deletes older ones
- **LLM-optimized output** - includes header to guide AI agents

## Installation

```bash
pipx install git+https://github.com/flonest/repomap-tool
```

Or with pip:
```bash
pip install git+https://github.com/flonest/repomap-tool
```

## Usage

```bash
# Monitor current directory
repomap .

# Monitor specific project
repomap /path/to/your/project
```

The tool will:
1. Generate an initial `REPOMAP.V YYMMDD_HHMMSS.TXT` in the project root
2. Watch for file changes
3. Regenerate the map 10 seconds after the last edit
4. Keep only the 2 most recent versions

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
```

## License

MIT
