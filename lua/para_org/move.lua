-- lua/para_org/move.lua

-- This module handles all safe file operations, including moving and archiving notes.
-- It follows the principle of "never delete".

local Path = require('plenary.path')
local config = require('para_org.config').options

local M = {}

-- A basic frontmatter parser, similar to the one in the indexer.
-- In a refactor, this could be moved to a shared 'utils' module.
local function parse_frontmatter(content)
  local start_delim, end_delim = table.unpack(config.patterns.frontmatter_delimiters)
  local _, start_pos = content:find(start_delim, 1, true)
  local fm_end, end_pos = content:find(end_delim, start_pos + 1, true)
  if not fm_end then return nil, content end

  local fm_content = content:sub(start_pos + 1, end_pos - #end_delim - 1)
  local body_content = content:sub(end_pos + 1)
  return fm_content, body_content
end

-- Safely moves a file to the archive directory.
-- If a file with the same name exists, it appends a timestamp.
function M.archive_note(original_path)
  local archive_root = Path:new(config.root_dir, config.folders.Archives)
  local archive_dest_path = Path:new(archive_root, config.archive_capture_path)
  archive_dest_path:mkdir({ parents = true })

  local original_p = Path:new(original_path)
  local dest_file = Path:new(archive_dest_path, original_p.name)

  if dest_file:exists() then
    local timestamp = os.date('!%Y%m%dT%H%M%S')
    dest_file = Path:new(archive_dest_path, original_p.stem .. '_' .. timestamp .. original_p.suffix)
  end

  local ok, err = original_p:move(dest_file:absolute())
  if not ok then
    vim.notify('Failed to archive note: ' .. err, vim.log.levels.ERROR)
    return false
  end

  vim.notify('Archived ' .. original_p.name .. ' to ' .. dest_file:shorten(40), vim.log.levels.INFO)
  return true
end

-- Copies a note to a new destination, adds metadata, and archives the original.
function M.move_to_dest(original_path, para_dest_path)
  local original_p = Path:new(original_path)
  local dest_p = Path:new(config.root_dir, para_dest_path)
  dest_p:mkdir({ parents = true })

  local new_file_path = Path:new(dest_p, original_p.name)
  if new_file_path:exists() then
    vim.notify('File already exists at destination: ' .. new_file_path.name, vim.log.levels.ERROR)
    return false
  end

  -- 1. Copy the file
  local ok, err = original_p:copy(new_file_path:absolute())
  if not ok then
    vim.notify('Failed to copy file to destination: ' .. err, vim.log.levels.ERROR)
    return false
  end

  -- 2. TODO: Add tags to the new file's frontmatter

  -- 3. Archive the original file
  if M.archive_note(original_path) then
    -- 4. Record the successful move for the learning engine
    local _, frontmatter = parse_frontmatter(original_p:read())
    require('para_org.learn').record_move(frontmatter, para_dest_path)

    vim.notify('Successfully moved ' .. original_p.name .. ' to ' .. dest_p:shorten(40), vim.log.levels.INFO)
    return true
  end

  return false
end

return M
