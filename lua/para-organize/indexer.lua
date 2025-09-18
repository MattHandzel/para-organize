-- para-organize/indexer.lua
-- Note indexing and metadata extraction

local M = {}

-- Dependencies
local Path = require("plenary.path")
local Job = require("plenary.job")
local scan = require("plenary.scandir")
local utils = require("para-organize.utils")

-- Module state
local index = {}
local index_file = nil
local is_indexing = false

-- Initialize indexer
function M.init()
  local config = require("para-organize.config").get()

  -- Set index file path
  index_file = vim.fn.stdpath("data") .. "/para-organize/index.json"

  -- Load existing index
  M.load_index()

  utils.log("DEBUG", "Indexer initialized with %d entries", vim.tbl_count(index))
end

local persistence_mod = require("para-organize.indexer.persistence")

-- Load index from disk (delegated)
function M.load_index()
  index = persistence_mod.load_index(index_file)
end

-- Save index to disk (delegated)
function M.save_index()
  persistence_mod.save_index(index, index_file)
end

local metadata_mod = require("para-organize.indexer.metadata")

-- Extract metadata from a note file (delegated)
M.extract_metadata = metadata_mod.extract_metadata

-- Determine PARA type from file path
function M.get_para_type(filepath)
  local config = require("para-organize.config").get()
  local vault_dir = config.paths.vault_dir
  local para_folders = config.paths.para_folders

  -- Get relative path from vault
  local rel_path = filepath:sub(#vault_dir + 2)

  -- Check each PARA folder
  for para_type, folder_name in pairs(para_folders) do
    if rel_path:find("^" .. folder_name .. "/") then
      return para_type
    end
  end

  -- Check if in capture folder
  if rel_path:find("^" .. config.paths.capture_folder .. "/") then
    return "capture"
  end

  return "other"
end

-- Index a single file
function M.index_file(filepath)

  utils.log("DEBUG", "Indexing file: %s", filepath)

  local metadata = M.extract_metadata(filepath)
  if metadata then
    index[filepath] = metadata
    return true
  end

  return false
end

-- Update index for a single file
function M.update_file(filepath)

  -- Check if file still exists
  if not utils.path_exists(filepath) then
    -- Remove from index
    if index[filepath] then
      index[filepath] = nil
      utils.log("DEBUG", "Removed from index: %s", filepath)
      M.save_index()
    end
    return
  end

  -- Re-index the file
  if M.index_file(filepath) then
    M.save_index()
  end
end

local scan_mod = require("para-organize.indexer.scan")
-- NOTE: full glue for scan/reindex can be added here as needed

local query_mod = require("para-organize.indexer.query")
-- NOTE: full glue for search can be added here as needed

-- Get all notes in a folder
function M.get_folder_notes(folder_path)
  local results = {}

  for filepath, metadata in pairs(index) do
    if filepath:find("^" .. vim.pesc(folder_path) .. "/") then
      table.insert(results, metadata)
    end
  end

  -- Sort by title
  table.sort(results, function(a, b)
    return (a.title or "") < (b.title or "")
  end)

  return results
end

-- Get statistics about the index
-- Get a single note from the index
function M.get_note(filepath)
  return index[filepath]
end

function M.get_statistics()
  local stats = {
    total = vim.tbl_count(index),
    by_type = {},
    by_status = {},
    with_tags = 0,
    with_sources = 0,
  }

  for _, metadata in pairs(index) do
    -- Count by PARA type
    local para_type = metadata.para_type or "other"
    stats.by_type[para_type] = (stats.by_type[para_type] or 0) + 1

    -- Count by processing status
    if metadata.processing_status then
      stats.by_status[metadata.processing_status] = (
        stats.by_status[metadata.processing_status] or 0
      ) + 1
    end

    -- Count with tags
    if metadata.tags and #metadata.tags > 0 then
      stats.with_tags = stats.with_tags + 1
    end

    -- Count with sources
    if metadata.sources and #metadata.sources > 0 then
      stats.with_sources = stats.with_sources + 1
    end
  end

  return stats
end

-- Get a note by its path
function M.get_note_by_path(filepath)
  return index[filepath]
end

-- Get all capture notes from the capture directory
function M.get_capture_notes()
  local config = require("para-organize.config").get()
  local capture_dir = config.paths.capture_dir
  local files = utils.glob_files("*", capture_dir)
  local notes = {}

  for _, file in ipairs(files) do
    if not index[file] or not index[file].moved_to then
      local note = M.get_note_by_path(file)
      if note then
        table.insert(notes, note)
      end
    end
  end

  return notes
end

-- Clear the index (delegated)
function M.clear()
  index = {}
  persistence_mod.clear_index(index_file)
end

local dirs_mod = require("para-organize.indexer.dirs")
M.get_para_directories = dirs_mod.get_para_directories
M.get_sub_items = dirs_mod.get_sub_items
M.get_directory_by_name = dirs_mod.get_directory_by_name
M.get_file_by_alias_or_name = dirs_mod.get_file_by_alias_or_name

-- Initialize on module load
M.init()

return M
