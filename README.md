# para-organize.nvim

A powerful Neovim plugin for organizing captured notes using the PARA method (Projects, Areas, Resources, Archives) with intelligent suggestions, safe file operations, and a beautiful two-pane interface.

## Features

- 🎯 **Intelligent Suggestions**: Learn from your organization patterns to suggest the best destinations for notes
- 🔍 **Powerful Search**: Filter notes by tags, sources, modalities, metadata, and time windows
- 🎨 **Beautiful UI**: Two-pane interface built with nui.nvim for seamless note organization
- 🔒 **Safe Operations**: Never lose data - all originals are archived, not deleted
- 🧠 **Learning System**: Improves suggestions over time based on your organization patterns
- ⚡ **Fast & Async**: Non-blocking operations keep Neovim responsive
- 🔌 **Telescope Integration**: Familiar fuzzy finding interface for navigation
- ⌨️ **Keyboard-Centric**: Fully keyboard-driven with discoverable shortcuts

## Screenshots

[Coming soon]

## Requirements

- Neovim >= 0.9.0
- [plenary.nvim](https://github.com/nvim-lua/plenary.nvim) (required)
- [telescope.nvim](https://github.com/nvim-telescope/telescope.nvim) (required)
- [nui.nvim](https://github.com/MunifTanjim/nui.nvim) (recommended)
- [which-key.nvim](https://github.com/folke/which-key.nvim) (optional)
- [nvim-web-devicons](https://github.com/nvim-tree/nvim-web-devicons) (optional)

## Installation

### Using [lazy.nvim](https://github.com/folke/lazy.nvim)

```lua
{
  "para-organize.nvim",
  dependencies = {
    "nvim-lua/plenary.nvim",
    "nvim-telescope/telescope.nvim",
    "MunifTanjim/nui.nvim",
    -- Optional dependencies
    "folke/which-key.nvim",
    "nvim-tree/nvim-web-devicons",
  },
  config = function()
    require("para-organize").setup({
      -- your configuration
    })
  end,
}
```

### Using [packer.nvim](https://github.com/wbthomason/packer.nvim)

```lua
use {
  "para-organize.nvim",
  requires = {
    "nvim-lua/plenary.nvim",
    "nvim-telescope/telescope.nvim",
    "MunifTanjim/nui.nvim",
    -- Optional
    "folke/which-key.nvim",
    "nvim-tree/nvim-web-devicons",
  },
  config = function()
    require("para-organize").setup({
      -- your configuration
    })
  end,
}
```

## Configuration

Here's the complete default configuration:

```lua
require("para-organize").setup({
  -- Path configuration
  paths = {
    -- Base directory for your notes vault
    vault_dir = "~/notes",
    
    -- Relative path from vault_dir where captured notes are stored
    capture_folder = "capture/raw_capture",
    
    -- PARA folder structure (relative to vault_dir)
    para_folders = {
      projects = "projects",
      areas = "areas",
      resources = "resources",
      archives = "archives",
    },
    
    -- Where to archive processed captures (relative to archives folder)
    archive_capture_path = "capture/raw_capture",
  },

  -- File patterns and parsing
  patterns = {
    -- Glob pattern for note files
    file_glob = "**/*.md",
    
    -- Frontmatter delimiters
    frontmatter_delimiters = { "---", "---" },
    
    -- How to extract aliases from frontmatter
    alias_extraction = "aliases",
    
    -- Tag normalization rules
    tag_normalization = {
      ["project"] = "projects",
      ["area"] = "areas",
      ["resource"] = "resources",
    },
    
    -- Case sensitivity for matching
    case_sensitive = false,
  },

  -- Indexing configuration
  indexing = {
    -- Patterns to ignore when scanning
    ignore_patterns = {
      "*.tmp",
      ".git",
      "node_modules",
      ".obsidian",
    },
    
    -- Maximum file size to index (in bytes)
    max_file_size = 1048576, -- 1MB
    
    -- Debounce time for incremental updates (milliseconds)
    incremental_debounce = 500,
    
    -- Backend for storing index
    backend = "json", -- "json" or "sqlite"
    
    -- Auto-reindex on BufWritePost
    auto_reindex = true,
  },

  -- Suggestion engine configuration
  suggestions = {
    -- Weight multipliers for different matching types
    weights = {
      exact_tag_match = 2.0,      -- Tag exactly matches folder name
      normalized_tag_match = 1.5, -- Tag matches after normalization
      learned_association = 1.8,  -- Previously successful pattern
      source_match = 1.3,         -- Source matches folder
      alias_similarity = 1.1,     -- Alias similar to folder name
      context_match = 1.0,        -- Context matches folder
    },
    
    -- Learning system parameters
    learning = {
      recency_decay = 0.9,       -- How quickly old patterns lose weight
      frequency_boost = 1.2,     -- Boost for frequently used patterns
      min_confidence = 0.3,      -- Minimum confidence to show suggestion
      max_history = 1000,        -- Maximum learning records to keep
    },
    
    -- Maximum number of suggestions to show
    max_suggestions = 10,
    
    -- Always show archive option
    always_show_archive = true,
  },

  -- UI configuration
  ui = {
    -- Layout type: "float" or "split"
    layout = "float",
    
    -- Window dimensions (for float layout)
    float_opts = {
      width = 0.9,      -- Percentage of editor width
      height = 0.8,     -- Percentage of editor height
      border = "rounded",
      position = "center",
    },
    
    -- Split configuration
    split_opts = {
      direction = "vertical",
      size = 0.5, -- 50% split
    },
    
    -- Visual options
    icons = {
      enabled = true,
      project = "",
      area = "",
      resource = "",
      archive = "🗑",
      folder = "",
      file = "",
      tag = "",
    },
    
    -- Display options
    display = {
      show_scores = true,          -- Show confidence scores
      show_counts = true,          -- Show file counts in folders
      show_timestamps = true,      -- Show note timestamps
      timestamp_format = "%b %d, %I:%M %p", -- Sep 10, 11:57 AM
      hide_capture_id = true,      -- Hide capture_id in aliases
      hide_modalities = true,      -- Hide modalities field
      hide_location = true,        -- Hide location field
    },
    
    -- Colors and highlights
    highlights = {
      selected = "Visual",
      project = "Function",
      area = "Keyword",
      resource = "String",
      archive = "Comment",
      tag = "Label",
      score_high = "DiagnosticOk",
      score_medium = "DiagnosticWarn",
      score_low = "DiagnosticHint",
    },
    
    -- Auto-move to newly created folders
    auto_move_to_new_folder = true,
  },

  -- Telescope configuration
  telescope = {
    -- Theme: "dropdown", "ivy", "cursor", or custom
    theme = "dropdown",
    
    -- Layout strategy
    layout_strategy = "horizontal",
    layout_config = {
      horizontal = {
        preview_width = 0.5,
      },
    },
    
    -- Enable multi-select for batch operations
    multi_select = true,
    
    -- Show preview by default
    previewer = true,
    
    -- Custom picker options
    picker_opts = {
      -- Options for folder picker
      folders = {
        show_files_count = true,
        include_empty = false,
      },
      -- Options for notes picker
      notes = {
        show_tags = true,
        show_modified = true,
      },
    },
  },

  -- File operations configuration
  file_ops = {
    -- Use atomic writes (write to temp, then rename)
    atomic_writes = true,
    
    -- Create backups before operations
    create_backups = true,
    
    -- Backup directory (relative to vault)
    backup_dir = ".backups",
    
    -- Operation log file
    log_operations = true,
    log_file = vim.fn.stdpath("data") .. "/para-organize/operations.log",
    
    -- Auto-create destination folders if they don't exist
    auto_create_folders = true,
    
    -- Confirm before destructive operations
    confirm_operations = false, -- We never delete, so this is safe
  },

  -- Keymaps (set to false to disable default mappings)
  keymaps = {
    -- Global mappings (disabled by default, use <Plug> mappings)
    global = false,
    
    -- Buffer-local mappings in organize UI
    buffer = {
      accept = "<CR>",          -- Accept selected suggestion
      cancel = "<Esc>",         -- Cancel and close
      next = "<Tab>",           -- Next capture
      prev = "<S-Tab>",         -- Previous capture
      skip = "s",               -- Skip current capture
      archive = "a",            -- Archive immediately
      merge = "m",              -- Enter merge mode
      search = "/",             -- Open search
      new_project = "<leader>np", -- Create new project
      new_area = "<leader>na",    -- Create new area
      new_resource = "<leader>nr", -- Create new resource
      refresh = "r",            -- Refresh suggestions
      toggle_preview = "p",     -- Toggle preview pane
      help = "?",               -- Show help
    },
  },

  -- Development and debugging
  debug = {
    -- Enable debug logging
    enabled = false,
    
    -- Log level: "trace", "debug", "info", "warn", "error"
    log_level = "info",
    
    -- Log file location
    log_file = vim.fn.stdpath("cache") .. "/para-organize.log",
    
    -- Performance profiling
    profile = false,
  },
})
```

## Usage

### Basic Workflow

1. **Start an organizing session**:
   ```vim
   :ParaOrganize start
   ```

2. **Process captures with filters**:
   ```vim
   :ParaOrganize start tags=meeting sources=email since=2024-01-01
   ```

3. **Navigate through captures**:
   - `<Tab>` - Next capture
   - `<S-Tab>` - Previous capture
   - `s` - Skip current capture

4. **Organize notes**:
   - Normal Vim movement keys (`j`, `k`, etc.) work in both panes for cursor movement
   - `Alt+j`/`Alt+k` - Navigate through suggestions
   - `<CR>` - Accept selected suggestion, open folder, or select file for merge
   - `m` - Merge with existing note (via picker)
   - `a` - Archive immediately
   - `/` - Context-aware search (searches within current folder)
   - `<BS>` - Go back to parent directory
   - `<C-h>`/`<C-l>` - Switch between left/right panes

5. **Create new destinations**:
   - `<leader>np` - New project folder
   - `<leader>na` - New area folder
   - `<leader>nr` - New resource folder

### Commands

- `:ParaOrganize start [filters...]` - Start organizing session
- `:ParaOrganize stop` - End current session
- `:ParaOrganize next` - Go to next capture
- `:ParaOrganize prev` - Go to previous capture
- `:ParaOrganize skip` - Skip current capture
- `:ParaOrganize move <destination>` - Move to specific destination
- `:ParaOrganize merge` - Enter merge mode
- `:ParaOrganize archive` - Archive current capture
- `:ParaOrganize reindex` - Rebuild the index
- `:ParaOrganize debug` - Show debug information and diagnostics
- `:ParaOrganize help` - Show help

### Filter Options

When starting a session, you can filter captures:

- `tags=tag1,tag2` - Filter by tags
- `sources=source1,source2` - Filter by sources
- `modalities=text,audio` - Filter by modalities
- `since=2024-01-01` - Notes created after date
- `until_date=2024-12-31` - Notes created before date
- `status=raw` - Filter by processing status

### Merge Workflow

Two ways to merge a file:

**Method 1 - Direct navigation:**
1. Navigate through folders in the right pane using cursor keys
2. Press Enter on a folder to open it
3. Navigate to a file and press Enter to start the merge
4. Edit the merged content in the right pane
5. Use `<leader>mc` to complete the merge (save changes)
6. Use `<leader>mx` to cancel the merge

**Method 2 - Using merge command:**
1. Press `m` to enter merge mode
2. Select a destination folder in the picker
3. Select a note to merge in the picker
4. Edit the merged content in the right pane
5. Use `<leader>mc` to complete the merge (save changes)
6. Use `<leader>mx` to cancel the merge

### <Plug> Mappings

If you prefer custom mappings, use the provided <Plug> mappings:

```lua
vim.keymap.set('n', '<leader>os', '<Plug>(ParaOrganizeStart)')
vim.keymap.set('n', '<leader>oa', '<Plug>(ParaOrganizeAccept)')
vim.keymap.set('n', '<leader>om', '<Plug>(ParaOrganizeMerge)')
vim.keymap.set('n', '<leader>ox', '<Plug>(ParaOrganizeArchive)')
vim.keymap.set('n', '<leader>on', '<Plug>(ParaOrganizeNext)')
vim.keymap.set('n', '<leader>op', '<Plug>(ParaOrganizePrev)')
vim.keymap.set('n', '<leader>ok', '<Plug>(ParaOrganizeSkip)')
vim.keymap.set('n', '<leader>o/', '<Plug>(ParaOrganizeSearch)')
```

## Note Format

The plugin works with Markdown files containing optional YAML frontmatter:

```markdown
---
timestamp: 2024-01-15T10:30:00Z
id: unique-id-123
aliases:
  - Meeting Notes
  - Project Kickoff
tags:
  - meeting
  - project-alpha
sources:
  - email
  - calendar
modalities:
  - text
  - diagram
context: "Q1 planning session"
metadata:
  attendees: ["Alice", "Bob"]
  duration: "1h"
processing_status: raw
created_date: 2024-01-15
last_edited_date: 2024-01-16
---

# Meeting Notes

Content of your note...
```

All frontmatter fields are optional - the plugin handles missing metadata gracefully.

## PARA Method

The plugin organizes notes following the PARA method:

- **Projects**: Things with a deadline and specific outcome
- **Areas**: Ongoing responsibilities to maintain
- **Resources**: Topics of ongoing interest
- **Archives**: Inactive items from the above categories

## Development

### Running Tests

```bash
# Run all tests
make test

# Run specific test file
nvim -l tests/indexer_spec.lua
```

### Health Check

Check if everything is set up correctly:

```vim
:checkhealth para-organize
```

### Debug Mode

Enable debug logging for troubleshooting:

```lua
require("para-organize").setup({
  debug = {
    enabled = true,
    log_level = "debug",
  }
})
```

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Acknowledgments

- Inspired by the [PARA Method](https://fortelabs.co/blog/para/) by Tiago Forte
- UI components powered by [nui.nvim](https://github.com/MunifTanjim/nui.nvim)
- Search interface built with [telescope.nvim](https://github.com/nvim-telescope/telescope.nvim)

## Support

- Report issues on [GitHub Issues](https://github.com/yourusername/para-organize.nvim/issues)
- Ask questions in [Discussions](https://github.com/yourusername/para-organize.nvim/discussions)
- See [wiki](https://github.com/yourusername/para-organize.nvim/wiki) for detailed guides
