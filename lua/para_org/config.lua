-- lua/para_org/config.lua

-- This module handles the plugin's configuration.
-- It provides default values and merges them with the user's custom settings.

local M = {}

-- Default configuration values.
-- These are based on the provided specification.
M.defaults = {
  -- The root directory for the PARA structure.
  -- Defaults to the current working directory if not set.
  root_dir = vim.fn.getcwd(),

  -- Names for the top-level PARA folders.
  folders = {
    Projects = 'Projects',
    Areas = 'Areas',
    Resources = 'Resources',
    Archives = 'Archives',
  },

  -- Path under the Archives folder to store original capture files.
  archive_capture_path = 'capture/raw_capture',

  -- File and content patterns.
  patterns = {
    file_glob = '*.md', -- Glob for finding note files.
    frontmatter_delimiters = {'---', '---'}, -- Delimiters for YAML frontmatter.
  },

  -- Indexing-related settings.
  indexing = {
    -- Paths to ignore during indexing, relative to root_dir.
    ignore_paths = { '**/node_modules/**', '**/.git/**' },
    -- Backend for the index. 'json' is the default.
    backend = 'json',
  },

  -- Suggestion engine settings.
  suggestions = {
    -- Weights for different matching criteria.
    weights = {
      tag_folder_match = 10.0,
      learned_association = 8.0,
      alias_folder_match = 5.0,
    },
    -- Decay constants for learned associations.
    recency_decay = 0.95,
    frequency_decay = 0.99,
  },

  -- UI settings.
  ui = {
    -- 'floats' or 'splits'
    layout = 'floats',
    border_style = 'rounded',
    show_scores = true,
    timestamp_format = '%Y-%m-%d %H:%M',
  },

  -- Telescope integration settings.
  telescope = {
    picker_theme = 'dropdown',
  },
}

-- The active configuration, starting with defaults.
M.options = vim.deepcopy(M.defaults)

-- Merges user-provided configuration with the defaults.
-- This function is called from the main `setup` function.
function M.setup(user_config)
  user_config = user_config or {}
  M.options = vim.tbl_deep_extend('force', M.options, user_config)
  -- TODO: Add validation using vim.validate as per best practices.
end

return M
