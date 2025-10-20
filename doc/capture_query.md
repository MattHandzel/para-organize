# capture_query: Raw Capture Search CLI

`scripts/capture_query.py` lets you treat the PARA raw capture folder as a queryable dataset. It scans Markdown files with YAML frontmatter, applies structured filters, and emits matches in automation-friendly formats (Markdown, JSONL, or plain paths). Every frontmatter field shown in the capture schema can be targeted from the CLI, making it easy to chain the output into Taskwarrior or other tools.

## Requirements

- Python 3.8+
- [PyYAML](https://pyyaml.org/) (`pip install pyyaml`)

## Common Flags

- `--root` vault root (defaults to `pwd`)
- `--capture-dir` relative or absolute path to the raw capture folder (`capture/raw_capture` by default)
- `--id`, `--capture-id`, `--timestamp`, `--created-date`, `--last-edited-date` for identity and time filters (repeat each flag as needed)
- `--processing-status` to scope workflow state (e.g., `raw`, `organized`)
- `--tag TAG` with `--require-all-tags` to control OR/AND behaviour
- `--alias VALUE` for alias hits
- `--modality VALUE`, `--context VALUE`, `--source VALUE` for list membership (`--require-all-*` variants enforce AND semantics)
- `--location FIELD=VALUE`, `--metadata FIELD=VALUE`, `--where KEY=VALUE` for dotted-path filters
- `--search TEXT` substring match against Markdown body (`--case-sensitive` optional)
- `--format` output style (`markdown`, `content`, `json`, `paths`) and `--limit N` to cut the stream

## Example Workflows

```bash
# Preview todo captures as Markdown via STDOUT
python scripts/capture_query.py --root ~/notes --tag todo --format markdown | less

# Pipe note bodies tagged with todo into Taskwarrior
python scripts/capture_query.py --root ~/notes --tag todo --format content | \
  while read -r line; do task add "$line" +todo; done

# Fetch captures created on 2025-10-19 that remain raw
python scripts/capture_query.py --root ~/notes --created-date 2025-10-19 \
  --processing-status raw --format paths

# Select notes recorded in Champaign with text modality
python scripts/capture_query.py --root ~/notes --location city=Champaign \
  --modality text --format json | jq '.frontmatter.id'

# Search by exact capture_id
python scripts/capture_query.py --root ~/notes \
  --capture-id 2025-10-19T22:16:42.026156+00:00 --format markdown
```

## Structured Filtering

`--where` accepts dotted keys into the frontmatter map, making it easy to chain predicates:

```bash
python scripts/capture_query.py \
  --root ~/notes \
  --where processing_status=raw \
  --where created_date=2025-10-19 \
  --metadata source=obsidian \
  --format paths
```

Lists are treated as membership checks, so `--where tags=todo` behaves like `--tag todo`. All comparisons stringify the right-hand side, ensuring timestamps captured as strings remain filterable even if YAML formatting varies between captures.

## Output Formats

- `markdown`: original note (frontmatter + body) separated by blank lines
- `content`: Markdown body only, ready for piping into `cat`, `tee`, or NLP tools
- `json`: JSON lines (`frontmatter`, `content`, `path`) for scripting
- `paths`: filesystem paths only

Combine these outputs with UNIX tools (`jq`, `grep`, `awk`) to construct richer automations without leaving the terminal.
