-- para-organize/ui/render.lua
-- Rendering functions for PARA organize UI

local M = {}

-- Helper to get PARA type letter (could be moved to helpers)
local function get_type_letter(type)
  if type == "projects" then return "P"
  elseif type == "areas" then return "A"
  elseif type == "resources" then return "R"
  elseif type == "archives" then return "🗑️"
  else return "?" end
end

function M.render_capture(ui_state, capture)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  local ui_config = config.ui

  local content = {}
  if capture then
    if capture.frontmatter then
      for k, v in pairs(capture.frontmatter) do
        table.insert(content, string.format("> **%s**: %s", k, v))
      end
      table.insert(content, "")
    end
    if capture.body then
      for line in capture.body:gmatch("[^
]+") do
        table.insert(content, line)
      end
    end
  else
    table.insert(content, "No capture loaded.")
  end

  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_lines(ui_state.capture_popup.bufnr, 0, -1, false, content)
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "filetype", "markdown")
end

function M.render_suggestions(ui_state, suggestions)
  local content = { "# Suggestions", "" }
  for i, suggestion in ipairs(suggestions or {}) do
    local line = string.format("%d. %s", i, suggestion.display or suggestion.name or "(unnamed)")
    table.insert(content, line)
    if suggestion.reasons then
      for _, reason in ipairs(suggestion.reasons) do
        table.insert(content, "   - " .. reason)
      end
    end
    table.insert(content, "")
  end
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
end

function M.render_directories(ui_state, dirs)
  local content = { "# Directories", "" }
  for _, dir in ipairs(dirs or {}) do
    local type_letter = get_type_letter(dir.type)
    table.insert(content, string.format("[%s] %s", type_letter, dir.name))
  end
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
end

function M.render_merge_view(ui_state)
  if not ui_state.merge_target then return end
  local utils = require("para-organize.utils")
  local config = require("para-organize.config").get()
  local capture_content = utils.read_file(ui_state.current_capture.path) or ""
  local target_content = utils.read_file(ui_state.merge_target.path) or ""
  if not target_content then
    vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, { "Error: Could not read target file" })
    return
  end
  local delimiters = config.patterns.frontmatter_delimiters
  local _, capture_body = utils.extract_frontmatter(capture_content, delimiters)
  ui_state.organize_popup.border:set_text("top", " Merge: " .. (ui_state.merge_target.alias or ui_state.merge_target.name) .. " ")
  local lines = {
    "# Merging: " .. (ui_state.merge_target.alias or ui_state.merge_target.name), "",
    "## Instructions:",
    "1. Edit this note to include content from the capture",
    "2. Press <leader>mc to complete merge",
    "3. Press <leader>mx to cancel", "",
    "## Original Content:", "",
  }
  vim.list_extend(lines, vim.split(target_content, "\n"))
  lines[#lines + 1] = ""
  lines[#lines + 1] = "## Capture Content to Merge:"
  lines[#lines + 1] = ""
  vim.list_extend(lines, vim.split(capture_body, "\n"))
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, lines)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
  local opts = { buffer = ui_state.organize_popup.bufnr, silent = true }
  vim.keymap.set("n", "<leader>mc", function()
    local utils = require("para-organize.utils")
    local move = require("para-organize.move")
    local edited_content = table.concat(vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, 0, -1, false), "\n")
    if utils.write_file_atomic(ui_state.merge_target.path, edited_content) then
      vim.notify("Successfully merged content to " .. ui_state.merge_target.name, vim.log.levels.INFO)
      move.archive_capture(ui_state.current_capture)
      M.render_suggestions(ui_state, ui_state.current_suggestions)
      ui_state.merge_mode = false
      ui_state.merge_target = nil
    end
  end, opts)
  vim.keymap.set("n", "<leader>mx", function()
    ui_state.merge_mode = false
    ui_state.merge_target = nil
    M.render_suggestions(ui_state, ui_state.current_suggestions)
    vim.notify("Merge canceled", vim.log.levels.INFO)
  end, opts)
end

function M.highlight_selection(ui_state)
  local ns_id = vim.api.nvim_create_namespace("para_organize_selection")
  vim.api.nvim_buf_clear_namespace(ui_state.organize_popup.bufnr, ns_id, 0, -1)
  local line_num = 2
  for i = 1, ui_state.selected_suggestion_index - 1 do
    line_num = line_num + 1
    if ui_state.current_suggestions[i].reasons then
      line_num = line_num + #ui_state.current_suggestions[i].reasons
    end
    line_num = line_num + 1
  end
  vim.api.nvim_buf_add_highlight(ui_state.organize_popup.bufnr, ns_id, "Visual", line_num, 0, -1)
end

return M
