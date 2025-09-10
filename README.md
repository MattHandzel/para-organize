# para-organize.nvim

A Neovim plugin to supercharge the "Organize" phase of your P.A.R.A. second brain.

`para-organize.nvim` is designed for a fast, keyboard-centric workflow, helping you rapidly process captured notes. It provides a two-pane interface to view a note, see intelligent suggestions for where to file it, and move or merge it across your `Projects`, `Areas`, and `Resources` folders.

## Features

- **Two-Pane Organizer UI**: View your capture note on the left and a list of actions on the right.
- **Intelligent Suggestions**: Get a ranked list of suggested destinations based on the note's tags and aliases.
- **Fuzzy-Finding Everywhere**: Uses `telescope.nvim` to let you quickly find notes and folders.
- **Safe File Operations**: Never deletes your notes. Moves are handled with a copy-then-archive strategy to prevent data loss.
- **Configurable**: Customize folders, UI elements, and suggestion scoring to fit your workflow.

## Installation

Install with your favorite plugin manager. Here is an example using `lazy.nvim`:

```lua
{
  'MattHandzel/para-organize',
  dependencies = {
    'nvim-lua/plenary.nvim',
    'nvim-telescope/telescope.nvim',
    'MunifTanjim/nui.nvim',
  },
  config = function()
    require('para_org').setup({
      -- Your configuration goes here
      -- See the example below
    })
  end,
}
```

## Usage

1.  **Index Your Notes**: Run `:PARAOrganize reindex` to scan your notes directory. This builds the search index that powers the plugin.
2.  **Start Organizing**: Run `:PARAOrganize start`. A Telescope window will open with a list of your notes.
3.  **Select a Note**: Choose a note from the list to begin organizing.
4.  **Process Your Notes**:

    - **Single Note**: Press `<CR>` on a single note to open the Organizer UI.
    - **Batch Processing**: Use `<Tab>` to select multiple notes, then press `<C-p>` (Process) to organize them one after another in a session.

5.  **Use the Organizer UI**:
    - The left pane shows the content of your note. You can edit it freely.
    - The right pane shows a ranked list of suggested destinations.
    - **Accept a suggestion**: Press `<CR>` on a suggestion to move the note to that folder.
    - **Find another folder**: Press `/` to open a Telescope picker to find any P/A/R folder. This is the start of the **Merge Flow**.
    - **Quit**: Press `q` in any window to close the organizer.

## Configuration Example

Here is an example configuration with all the default values. You only need to include the fields you want to override.

```lua
require('para_org').setup({
  -- The root directory for your PARA structure.
  root_dir = vim.fn.getcwd(),

  -- Names for the top-level PARA folders.
  folders = {
    Projects = 'projects',
    Areas = 'areas',
    Resources = 'resources',
    Archives = 'archives',
  },

  -- Path under Archives to store original captures after a move.
  archive_capture_path = 'capture/raw_capture',

  -- File and content patterns.
  patterns = {
    file_glob = '*.md',
    frontmatter_delimiters = {'---', '---'},
  },

  -- UI settings.
  ui = {
    layout = 'floats', -- 'floats' or 'splits'
    border_style = 'rounded',
    show_scores = true,
    timestamp_format = '%Y-%m-%d %H:%M',
  },
})
```

## Next Milestones

- **🧠 Learning Engine**: Suggestions will improve over time based on where you file your notes.
- **🗂️ Batch Processing**: Organize multiple notes at once from the capture list.
- **✅ Comprehensive Tests**: A full test suite to ensure stability.

## TODOs

- [ ] Improve frontmatter parsing to handle complex types (e.g., lists of tags).
- [ ] Enhance suggestion scoring with more heuristics (e.g., content analysis).
- [ ] Add icons and custom highlights for a richer UI.
- [ ] Support for custom Telescope themes and layouts.
