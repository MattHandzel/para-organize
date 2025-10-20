-- para-organize/config/defaults.lua
-- Default configuration values for PARA organize

local M = {}

M.defaults = {
  ui = {
    float_opts = { border = "rounded", width = 0.8, height = 0.8, position = "center" },
    split_opts = { direction = "vertical", size = 0.5 },
    layout = "float",
    icons = { enabled = true, project = "", area = "", resource = "", archive = "🗑", folder = "", file = "", tag = "" },
    display = { show_scores = true, show_counts = true, show_timestamps = true, timestamp_format = "%b %d, %I:%M %p", hide_capture_id = true, hide_modalities = true, hide_location = true },
    highlights = { selected = "Visual", project = "Function", area = "Keyword", resource = "String", archive = "Comment", tag = "Label", score_high = "DiagnosticOk", score_medium = "DiagnosticWarn", score_low = "DiagnosticHint" },
    auto_move_to_new_folder = true,
  },
  indexing = {
    max_file_size = 1048576,
    ignore_patterns = { "*.tmp", ".git", "node_modules", ".obsidian" },
    incremental_debounce = 500,
    backend = "json",
    auto_reindex = true,
  },
  paths = {
    vault_dir = vim.fn.expand("~/notes"),
    capture_folder = "capture/raw_capture",
    para_folders = { projects = "projects", areas = "areas", resources = "resources", archives = "archives" },
    archive_capture_path = "capture/raw_capture",
  },
  suggestions = {
    weights = { exact_tag_match = 2.0, normalized_tag_match = 1.5, learned_association = 1.8, source_match = 1.3, alias_similarity = 1.1, context_match = 1.0 },
    learning = { recency_decay = 0.9, frequency_boost = 1.2, min_confidence = 0.3, max_history = 1000 },
    max_suggestions = 10,
    always_show_archive = true,
  },
  file_ops = {
    atomic_writes = true,
    create_backups = true,
    backup_dir = ".backups",
    log_operations = true,
    log_file = vim.fn.stdpath("data") .. "/para-organize/operations.log",
    auto_create_folders = true,
    confirm_operations = false,
  },
  keymaps = {
    global = false,
    buffer = {
      accept = "<CR>", cancel = "<Esc>", next = "<Tab>", prev = "<S-Tab>", skip = "s", archive = "a", merge = "m", search = "/", new_project = "<leader>np", new_area = "<leader>na", new_resource = "<leader>nr", refresh = "r", toggle_preview = "p", help = "?",
    },
  },
  patterns = {
    file_glob = "**/*.md",
    frontmatter_delimiters = { "---", "---" },
    alias_extraction = "aliases",
    tag_normalization = { ["project"] = "projects", ["area"] = "areas", ["resource"] = "resources" },
    case_sensitive = false,
  },
  telescope = {
    theme = "dropdown",
    layout_strategy = "horizontal",
    layout_config = { horizontal = { preview_width = 0.5 } },
    multi_select = true,
    previewer = true,
    picker_opts = {
      folders = { show_files_count = true, include_empty = false },
      notes = { show_tags = true, show_modified = true },
    },
  },
  debug = {
    enabled = false,
    log_level = "info",
    log_file = vim.fn.stdpath("cache") .. "/para-organize.log",
    profile = false,
  },
}


return M
