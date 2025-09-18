-- para-organize/move.lua
-- Safe file operations for moving and archiving notes

local M = {}

local backup_mod = require("para-organize.move.backup")
local log_mod = require("para-organize.move.log")
local frontmatter_mod = require("para-organize.move.frontmatter")

M.create_backup = backup_mod.create_backup
M.log_operation = log_mod.log_operation
M.get_undo_info = log_mod.get_undo_info
M.update_tags = frontmatter_mod.update_tags
M.build_frontmatter = frontmatter_mod.build_frontmatter

-- Move file to destination folder
function M.move_to_destination(source_path, destination_folder)
  local config = config_mod.get()
  local utils = require("para-organize.utils")

  utils.log("INFO", "Moving %s to %s", source_path, destination_folder)

  -- Validate paths
  if not utils.path_exists(source_path) then
    local error_msg = "Source file does not exist"
    M.log_operation("move", source_path, destination_folder, false, error_msg)
    return false, error_msg
  end

  -- Ensure destination folder exists
  if not utils.is_directory(destination_folder) then
    if config.file_ops.auto_create_folders then
      vim.fn.mkdir(destination_folder, "p")
      utils.log("INFO", "Created folder: %s", destination_folder)
    else
      local error_msg = "Destination folder does not exist"
      M.log_operation("move", source_path, destination_folder, false, error_msg)
      return false, error_msg
    end
  end

  -- Create backup first
  if config.file_ops.create_backups then
    M.create_backup(source_path)
  end

  -- Determine destination filename
  local filename = vim.fn.fnamemodify(source_path, ":t")
  local destination_path = destination_folder .. "/" .. filename

  -- Handle filename collision
  if utils.path_exists(destination_path) then
    local base_name = vim.fn.fnamemodify(filename, ":r")
    local extension = vim.fn.fnamemodify(filename, ":e")
    local counter = 1

    repeat
      destination_path =
        string.format("%s/%s_%d.%s", destination_folder, base_name, counter, extension)
      counter = counter + 1
    until not utils.path_exists(destination_path)

    utils.log("WARN", "File exists, using: %s", destination_path)
  end

  -- Copy file to destination
  if not utils.copy_file(source_path, destination_path) then
    local error_msg = "Failed to copy file"
    M.log_operation("move", source_path, destination_path, false, error_msg)
    return false, error_msg
  end

  -- Add PARA tags
  local folder_name = vim.fn.fnamemodify(destination_folder, ":t")
  local para_type = nil

  -- Determine PARA type from path
  local para_folders = config_mod.get_para_folders()
  for type_name, type_path in pairs(para_folders) do
    if destination_folder:find("^" .. vim.pesc(type_path)) then
      para_type = type_name:sub(1, -2) -- Remove 's' (projects -> project)
      break
    end
  end

  if para_type then
    local new_tags = {
      para_type .. "/" .. folder_name,
    }
    M.update_tags(destination_path, new_tags)
  end

  -- Archive original
  local archive_path = M.archive_capture(source_path)

  -- Log successful operation
  M.log_operation("move", source_path, destination_path, true, nil)

  utils.log("INFO", "Successfully moved to: %s", destination_path)

  return true, destination_path
end

-- Function to archive a capture note without moving to destination
function M.archive_capture(capture_note)
  if not capture_note.id then
    capture_note.id = capture_note.filename .. "_" .. os.date("%Y%m%d%H%M%S")
    require("para-organize.utils").log(
      "WARN",
      "Capture note missing ID. Generated a new one: " .. capture_note.id
    )
  end

  local config = config_mod.get()
  local utils = require("para-organize.utils")
  local archive_path = config.paths.vault_dir
    .. "/archives/capture/raw_capture/"
    .. capture_note.id
    .. ".md"
  local dest_dir = archive_path:match("(.+)/")
  if vim.fn.isdirectory(dest_dir) == 0 then
    vim.fn.mkdir(dest_dir, "p")
  end

  os.rename(capture_note.path, archive_path)
  vim.notify("Capture note archived: " .. capture_note.id, vim.log.levels.INFO)
  return archive_path
end

-- Merge content into existing note
function M.merge_into_note(source_path, target_path)
  local utils = require("para-organize.utils")
  local config = config_mod.get()

  utils.log("INFO", "Merging %s into %s", source_path, target_path)

  -- Read source content
  local source_content = utils.read_file(source_path)
  if not source_content then
    local error_msg = "Failed to read source file"
    M.log_operation("merge", source_path, target_path, false, error_msg)
    return false, error_msg
  end

  -- Read target content
  local target_content = utils.read_file(target_path)
  if not target_content then
    local error_msg = "Failed to read target file"
    M.log_operation("merge", source_path, target_path, false, error_msg)
    return false, error_msg
  end

  -- Create backup of target
  if config.file_ops.create_backups then
    M.create_backup(target_path)
  end

  -- Extract source frontmatter and body
  local source_fm_str, source_body =
    utils.extract_frontmatter(source_content, config.patterns.frontmatter_delimiters)
  local source_fm = {}
  if source_fm_str then
    source_fm = utils.parse_yaml_simple(source_fm_str)
  end

  -- Extract target frontmatter and body
  local target_fm_str, target_body =
    utils.extract_frontmatter(target_content, config.patterns.frontmatter_delimiters)
  local target_fm = {}
  if target_fm_str then
    target_fm = utils.parse_yaml_simple(target_fm_str)
  end

  -- Merge frontmatter
  -- Merge tags
  local merged_tags = {}
  local tag_set = {}

  for _, tag in ipairs(target_fm.tags or {}) do
    local normalized = utils.normalize_tag(tag)
    if not tag_set[normalized] then
      tag_set[normalized] = true
      table.insert(merged_tags, tag)
    end
  end

  for _, tag in ipairs(source_fm.tags or {}) do
    local normalized = utils.normalize_tag(tag)
    if not tag_set[normalized] then
      tag_set[normalized] = true
      table.insert(merged_tags, tag)
    end
  end

  target_fm.tags = merged_tags

  -- Merge sources
  local merged_sources = {}
  local source_set = {}

  for _, source in ipairs(target_fm.sources or {}) do
    if not source_set[source] then
      source_set[source] = true
      table.insert(merged_sources, source)
    end
  end

  for _, source in ipairs(source_fm.sources or {}) do
    if not source_set[source] then
      source_set[source] = true
      table.insert(merged_sources, source)
    end
  end

  target_fm.sources = merged_sources

  -- Update last edited date
  target_fm.last_edited_date = os.date("%Y-%m-%d")

  -- Build merged content
  local separator = "\n\n---\n\n"
  local merge_header = string.format(
    "## Merged from %s on %s\n\n",
    vim.fn.fnamemodify(source_path, ":t"),
    os.date("%Y-%m-%d %H:%M")
  )

  local merged_body = target_body .. separator .. merge_header .. source_body

  -- Rebuild content
  local new_frontmatter = M.build_frontmatter(target_fm)
  local delimiters = config.patterns.frontmatter_delimiters
  local merged_content = delimiters[1]
    .. "\n"
    .. new_frontmatter
    .. "\n"
    .. delimiters[2]
    .. "\n"
    .. merged_body

  -- Write merged content
  local success = false
  if config.file_ops.atomic_writes then
    success = utils.write_file_atomic(target_path, merged_content)
  else
    local file = io.open(target_path, "w")
    if file then
      file:write(merged_content)
      file:close()
      success = true
    end
  end

  if success then
    -- Archive source file
    M.archive_capture(source_path)
    M.log_operation("merge", source_path, target_path, true, nil)
    utils.log("INFO", "Successfully merged into: %s", target_path)
    return true, target_path
  else
    local error_msg = "Failed to write merged content"
    M.log_operation("merge", source_path, target_path, false, error_msg)
    return false, error_msg
  end
end

-- Get recent operations
function M.get_recent_operations(limit)
  limit = limit or 10

  local recent = {}
  local start_idx = math.max(1, #operation_log - limit + 1)

  for i = start_idx, #operation_log do
    table.insert(recent, operation_log[i])
  end

  return recent
end

-- Get undo information for last operation
function M.get_undo_info()
  if #operation_log == 0 then
    return nil
  end

  local last_op = operation_log[#operation_log]

  if not last_op.success then
    return nil
  end

  return {
    type = last_op.type,
    source = last_op.source,
    destination = last_op.destination,
    timestamp = last_op.timestamp,
    undo_hint = string.format(
      "To undo: move %s back to %s",
      last_op.destination,
      vim.fn.fnamemodify(last_op.source, ":h")
    ),
  }
end

return M
