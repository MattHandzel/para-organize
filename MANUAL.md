# para-organize.nvim User Manual

A comprehensive guide to using the para-organize.nvim plugin for organizing notes using the PARA method.

## Table of Contents

1. [Introduction](#introduction)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Basic Usage](#basic-usage)
5. [Advanced Features](#advanced-features)
6. [Keybindings](#keybindings)
7. [Customization](#customization)
8. [Troubleshooting](#troubleshooting)
9. [Tips and Best Practices](#tips-and-best-practices)

## Introduction

para-organize.nvim is a Neovim plugin designed to help you quickly organize your captured notes using the PARA method (Projects, Areas, Resources, Archives). It provides an intuitive two-pane interface, intelligent suggestions for where to file your notes, and learns from your organization patterns over time.

### What is PARA?

PARA is a method for organizing digital information developed by Tiago Forte:

- **Projects**: Short-term efforts with deadlines and specific goals
- **Areas**: Ongoing responsibilities that require maintenance over time
- **Resources**: Topics or themes of ongoing interest
- **Archives**: Inactive items from the other three categories

This plugin helps you quickly decide which of these categories (and which specific folder) a captured note should go into.

### Key Features

- **Intelligent Suggestions**: Based on tags, sources, and learned patterns
- **Two-pane Interface**: View and edit your capture while seeing destination suggestions
- **Learning System**: Improves suggestions based on your organizational habits
- **Safe File Operations**: Never deletes files, always archives originals
- **Merge Workflow**: Easily merge captures into existing notes
- **Search Integration**: Powerful search with Telescope
- **Quick Folder Creation**: Create new projects/areas/resources on the fly

## Installation

### Prerequisites

- Neovim >= 0.9.0
- Required dependencies:
  - [plenary.nvim](https://github.com/nvim-lua/plenary.nvim)
  - [telescope.nvim](https://github.com/nvim-telescope/telescope.nvim)
- Recommended dependencies:
  - [nui.nvim](https://github.com/MunifTanjim/nui.nvim)
  - [which-key.nvim](https://github.com/folke/which-key.nvim) (optional)
  - [nvim-web-devicons](https://github.com/nvim-tree/nvim-web-devicons) (optional)

### Using lazy.nvim

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

### Using packer.nvim

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

At a minimum, you'll need to specify your vault directory:

```lua
require("para-organize").setup({
  paths = {
    vault_dir = "~/notes",  -- Path to your notes vault
  }
})
```

For a complete configuration with all options, see the [README.md](README.md) file.

## Basic Usage

### Starting an Organizing Session

To start organizing your captured notes:

```vim
:ParaOrganize start
```

You can filter which captures to process:

```vim
:ParaOrganize start tags=project-x,meeting
:ParaOrganize start status=raw
:ParaOrganize start since=2024-01-01
:ParaOrganize start modalities=audio
```

### The Two-Pane Interface

When you start an organizing session, you'll see a two-pane interface:

- **Left Pane**: Shows the current capture note (fully editable with normal cursor movement)
- **Right Pane**: Shows suggested destinations for the note (navigable with normal cursor movement)

### Processing Notes

Navigate the interface with these basic commands:

- **Select a suggestion**: Use `j`/`k` to navigate and `<CR>` to accept
- **Navigate between captures**: Use `<Tab>` for next and `<S-Tab>` for previous
- **Skip a capture**: Press `s` to skip the current capture
- **Archive immediately**: Press `a` to archive without moving
- **Search for destination**: Press `/` to open a fuzzy finder
- **Switch between panes**: Use `<C-h>` to focus left pane, `<C-l>` to focus right pane
- **Normal navigation**: All standard Vim movement keys work in both panes

## Advanced Features

### Merge Mode

There are two ways to merge a capture into an existing note:

**Method 1 - Direct navigation:**
1. In the Organization Pane, navigate to a PARA folder using cursor keys
2. Press `Enter` to open that folder
3. Navigate to the target note and press `Enter` to start merge
4. Edit the merged content in the right pane (both original and capture content will be shown)
5. Press `<leader>mc` to complete the merge or `<leader>mx` to cancel

**Method 2 - Using merge command:**
1. Press `m` to enter merge mode
2. Select a destination folder from the picker
3. Select a target note from the picker
4. Edit the merged content in the right pane
5. Press `<leader>mc` to complete the merge or `<leader>mx` to cancel

**What happens during merge:**
- The right pane shows both the original note content and the capture content
- You can edit this content to merge them together however you want
- The original capture will be archived after merging
- Tags and metadata will be combined automatically

### Creating New Folders

Create new folders on the fly:

- `<leader>np` - Create a new project
- `<leader>na` - Create a new area
- `<leader>nr` - Create a new resource

If `auto_move_to_new_folder` is enabled (default), the current note will automatically move to the newly created folder.

### Learning System

The plugin learns from your organization patterns over time:

- Tags that frequently go to specific folders get higher suggestion scores
- Sources (like "email" or "meeting") develop associations with destinations
- Recency and frequency affect suggestion ranking
- Old patterns gradually decay in importance

To see what the plugin has learned:

```lua
:lua print(vim.inspect(require("para-organize.learn").get_statistics()))
```

## Keybindings

### Default Buffer Mappings

When the para-organize UI is active:

| Key | Action |
|-----|--------|
| `<CR>` | Accept selected suggestion |
| `<Esc>` | Cancel and close |
| `<Tab>` | Next capture |
| `<S-Tab>` | Previous capture |
| `j`/`k` | Navigate suggestions |
| `s` | Skip current capture |
| `a` | Archive immediately |
| `m` | Enter merge mode |
| `/` | Search for destination |
| `?` | Show help |
| `<leader>np` | Create new project |
| `<leader>na` | Create new area |
| `<leader>nr` | Create new resource |
| `r` | Refresh suggestions |

### Custom Global Mappings

You can create custom global mappings using the provided `<Plug>` mappings:

```vim
" In your init.vim or init.lua
nmap <leader>oo <Plug>(ParaOrganizeStart)
nmap <leader>om <Plug>(ParaOrganizeMerge)
nmap <leader>oa <Plug>(ParaOrganizeArchive)
```

## Customization

### UI Configuration

Customize the appearance of the UI:

```lua
require("para-organize").setup({
  ui = {
    layout = "float",  -- "float" or "split"
    float_opts = {
      width = 0.9,     -- 90% of editor width
      height = 0.8,    -- 80% of editor height
      border = "rounded",
    },
    icons = {
      enabled = true,
      project = "",
      area = "",
      resource = "",
      archive = "🗑",
    },
    display = {
      show_scores = true,  -- Show confidence scores
      show_timestamps = true,
    },
  },
})
```

### Custom Keybindings

Customize the buffer-local keybindings:

```lua
require("para-organize").setup({
  keymaps = {
    buffer = {
      accept = "<CR>",
      cancel = "<Esc>",
      next = "<Tab>",
      prev = "<S-Tab>",
      skip = "s",
      archive = "a",
      merge = "m",
      search = "/",
      help = "?",
      -- Add or change mappings as needed
    },
  },
})
```

### Suggestion Algorithm

Adjust the suggestion algorithm to favor different matching types:

```lua
require("para-organize").setup({
  suggestions = {
    weights = {
      exact_tag_match = 2.0,      -- Tag exactly matches folder name
      normalized_tag_match = 1.5,  -- Tag matches after normalization
      learned_association = 1.8,   -- Previously successful pattern
      source_match = 1.3,          -- Source matches folder
      alias_similarity = 1.1,      -- Alias similar to folder name
    },
  },
})
```

## Troubleshooting

### Debug Mode

Use the debug command to get comprehensive diagnostic information:

```vim
:ParaOrganize debug
```

This will show:
- Configuration details and paths
- Directory existence and file counts
- Index statistics
- Capture detection information
- Session status

### Health Check

Run a health check to diagnose issues:

```vim
:checkhealth para-organize
```

### Enable Debug Logging

Enable debug logging for detailed information:

```lua
require("para-organize").setup({
  debug = {
    enabled = true,
    log_level = "debug",  -- "trace", "debug", "info", "warn", "error"
  },
})
```

The log file is located at: `vim.fn.stdpath("cache") .. "/para-organize.log"`

### Common Issues

**Issue**: UI not showing or crashing
- Check if nui.nvim is installed
- Verify your Neovim version is >= 0.9.0
- Look for error messages with `:messages`

**Issue**: No suggestions appearing
- Ensure PARA folders exist in your vault
- Check that captures have tags or metadata
- Run `:ParaOrganize reindex` to rebuild the index

**Issue**: Files not moving correctly
- Check file permissions in your vault directory
- Review operation log at `vim.fn.stdpath("data") .. "/para-organize/operations.log"`
- Enable debug logging for more information

## Tips and Best Practices

### Organizing Workflow

1. **Capture First**: Focus on capturing ideas without worrying about organization
2. **Schedule Processing**: Set regular times to run through your captures
3. **Process in Batches**: Use filters to process related captures together
4. **Use Tags Consistently**: Consistent tagging improves suggestion quality
5. **Create Templates**: Use templates for common note types to ensure consistent metadata

### Tag Conventions

- Use project names as tags to help the plugin suggest the right destinations
- Consider using hierarchical tags like `project/subproject`
- Be consistent with tag formats (e.g., always use kebab-case)

### Keyboard-Driven Workflow

For maximum efficiency, learn these key combinations:

- `<Tab>`, `<CR>` - Process a note and move to next
- `s`, `<Tab>` - Skip a note and move to next
- `a`, `<Tab>` - Archive and move to next
- `/` + search + `<CR>` - Quick search for destination

### Extending the Plugin

para-organize.nvim exposes a Lua API you can use to extend its functionality:

```lua
-- In your config files
local para = require("para-organize")

-- Custom command to process all notes from a specific source
vim.api.nvim_create_user_command("ProcessMeetingNotes", function()
  para.start({ sources = "meeting" })
end, {})
```

## Additional Resources

- [GitHub Repository](https://github.com/yourusername/para-organize.nvim)
- [PARA Method by Tiago Forte](https://fortelabs.co/blog/para/)
- [Help Documentation](doc/para-organize.txt)
