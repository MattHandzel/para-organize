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

-- Load index from disk
function M.load_index()

  if not utils.path_exists(index_file) then
    index = {}
    return
  end

  local content = utils.read_file(index_file)
  if content then
    local ok, data = pcall(vim.json.decode, content)
    if ok and type(data) == "table" then
      index = data
      utils.log("DEBUG", "Loaded index with %d entries", vim.tbl_count(index))
    else
      utils.log("ERROR", "Failed to parse index file")
      index = {}
    end
  end
end

-- Save index to disk
function M.save_index()

  -- Ensure directory exists
  local index_dir = vim.fn.fnamemodify(index_file, ":h")
  if vim.fn.isdirectory(index_dir) == 0 then
    vim.fn.mkdir(index_dir, "p")
  end

  -- Save index
  local content = vim.json.encode(index)
  if utils.write_file_atomic(index_file, content) then
    utils.log("DEBUG", "Saved index with %d entries", vim.tbl_count(index))
  else
    utils.log("ERROR", "Failed to save index")
  end
end

-- Extract metadata from a note file
function M.extract_metadata(filepath)
  local config = require("para-organize.config").get()

  -- Read file content
  local content = utils.read_file(filepath)
  if not content then
    return nil
  end

  -- Check file size
  if #content > config.indexing.max_file_size then
    utils.log("WARN", "File too large, skipping: %s", filepath)
    return nil
  end

  local ok, result = pcall(function()
    -- Extract frontmatter
    local frontmatter_str, body =
      utils.extract_frontmatter(content, config.patterns.frontmatter_delimiters)

    utils.log("TRACE", "Frontmatter for %s: %s", filepath, frontmatter_str or "none")

    -- Parse frontmatter
    local frontmatter = {}
    if frontmatter_str then
      frontmatter = utils.parse_yaml_simple(frontmatter_str)
    end

    return { frontmatter = frontmatter, body = body }
  end)

  if not ok then
    utils.log("ERROR", "Failed to extract metadata for %s: %s", filepath, result)
    return nil
  end

  local frontmatter = result.frontmatter or {}
  local body = result.body

  utils.log("TRACE", "Parsed frontmatter for %s: %s", filepath, vim.inspect(frontmatter))

  -- Extract title from first header or filename
  local title = nil
  if body then
    title = body:match("^#%s+(.-)%s*\n")
  end
  if not title then
    title = vim.fn.fnamemodify(filepath, ":t:r")
  end

  -- Determine PARA type from path
  local para_type = M.get_para_type(filepath)

  -- Get file stats
  local stat = vim.loop.fs_stat(filepath)

  -- Build metadata
  local metadata = {
    path = filepath,
    filename = vim.fn.fnamemodify(filepath, ":t"),
    title = title,
    para_type = para_type,
    folder = vim.fn.fnamemodify(filepath, ":h:t"),

    -- From frontmatter
    timestamp = frontmatter.timestamp,
    id = frontmatter.id,
    aliases = frontmatter.aliases or {},
    capture_id = frontmatter.capture_id,
    tags = frontmatter.tags or {},
    sources = frontmatter.sources or {},
    modalities = frontmatter.modalities or {},
    context = frontmatter.context,
    location = frontmatter.location,
    metadata = frontmatter.metadata or {},
    processing_status = frontmatter.processing_status,
    created_date = frontmatter.created_date,
    last_edited_date = frontmatter.last_edited_date,

    -- File stats
    size = stat and stat.size or 0,
    modified = stat and stat.mtime.sec or 0,

    -- Indexing metadata
    indexed_at = os.time(),
  }

  -- Ensure list fields are tables even if YAML provided scalar strings
  local function ensure_list(value)
    if type(value) == "table" then
      return value
    elseif value == nil then
      return {}
    else
      return { value }
    end
  end

  metadata.tags = ensure_list(metadata.tags)
  metadata.aliases = ensure_list(metadata.aliases)
  metadata.sources = ensure_list(metadata.sources)
  metadata.modalities = ensure_list(metadata.modalities)

  -- Normalize tags
  if config.patterns.tag_normalization then
    local normalized_tags = {}
    for _, tag in ipairs(metadata.tags) do
      local normalized = utils.normalize_tag(tag)
      -- Apply normalization rules
      for pattern, replacement in pairs(config.patterns.tag_normalization) do
        if normalized == pattern then
          normalized = replacement
        end
      end
      table.insert(normalized_tags, normalized)
    end
    metadata.normalized_tags = normalized_tags
  end

  return metadata
end

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

-- Scan directory for notes
function M.scan_directory(dir_path, on_complete)
  local config = require("para-organize.config").get()

  utils.log("INFO", "Scanning directory: %s", dir_path)

  -- Build ignore patterns
  local ignore_patterns = {}
  for _, pattern in ipairs(config.indexing.ignore_patterns) do
    table.insert(ignore_patterns, pattern)
  end

  -- Scan for markdown files
  scan.scan_dir_async(dir_path, {
    respect_gitignore = true,
    search_pattern = config.patterns.file_glob,
    silent = true,
    on_exit = function(files)
      local count = 0
      for _, filepath in ipairs(files) do
        -- Check if should ignore
        local should_ignore = false
        for _, pattern in ipairs(ignore_patterns) do
          if filepath:match(pattern) then
            should_ignore = true
            break
          end
        end

        if not should_ignore then
          if M.index_file(filepath) then
            count = count + 1
          end
        end
      end

      utils.log("INFO", "Indexed %d files from %s", count, dir_path)

      if on_complete then
        on_complete(count)
      end
    end,
  })
end

-- Full reindex of all notes
function M.full_reindex(on_complete)
  if is_indexing then
      utils.log("WARN", "Indexing already in progress")
    return
  end

  is_indexing = true

  local config = require("para-organize.config").get()

  utils.log("INFO", "Starting full reindex")

  -- Clear existing index
  index = {}

  local start_time = vim.loop.now()
  local total_count = 0
  local dirs_to_scan = {}

  -- Add vault directory
  table.insert(dirs_to_scan, config.paths.vault_dir)

  local dirs_completed = 0
  local function check_complete()
    dirs_completed = dirs_completed + 1
    if dirs_completed >= #dirs_to_scan then
      -- Save index
      M.save_index()

      -- Calculate duration
      local duration = (vim.loop.now() - start_time) / 1000

      is_indexing = false

      if on_complete then
        on_complete({
          total = total_count,
          duration = duration,
        })
      end
    end
  end

  -- Scan each directory
  for _, dir in ipairs(dirs_to_scan) do
    M.scan_directory(dir, function(count)
      total_count = total_count + count
      check_complete()
    end)
  end
end

-- Search index by criteria
function M.search(criteria)
  local config = require("para-organize.config")
  local results = {}

  -- Diagnostic logging
  utils.log("TRACE", "Search criteria: %s", vim.inspect(criteria))
  utils.log("TRACE", "Total entries in index: %d", vim.tbl_count(index))
  utils.log("TRACE", "Vault directory: %s", config.get_vault_dir())
  utils.log("TRACE", "Capture folder: %s", config.get_capture_folder())

  for filepath, metadata in pairs(index) do
    local match = true

    -- Check para_type
    if criteria.para_type then
      if metadata.para_type ~= criteria.para_type then
        match = false
      end
    end

    -- Check tags
    if match and criteria.tags then
      local has_tag = false
      for _, required_tag in ipairs(criteria.tags) do
        for _, note_tag in ipairs(metadata.tags or {}) do
          if
            note_tag == required_tag
            or (
              metadata.normalized_tags
              and vim.tbl_contains(metadata.normalized_tags, required_tag)
            )
          then
            has_tag = true
            break
          end
        end
        if has_tag then
          break
        end
      end
      if not has_tag then
        match = false
      end
    end

    -- Check sources
    if match and criteria.sources then
      local has_source = false
      for _, required_source in ipairs(criteria.sources) do
        if vim.tbl_contains(metadata.sources or {}, required_source) then
          has_source = true
          break
        end
      end
      if not has_source then
        match = false
      end
    end

    -- Check modalities
    if match and criteria.modalities then
      local has_modality = false
      for _, required_modality in ipairs(criteria.modalities) do
        if vim.tbl_contains(metadata.modalities or {}, required_modality) then
          has_modality = true
          break
        end
      end
      if not has_modality then
        match = false
      end
    end

    -- Check processing status
    if match and criteria.status then
      if metadata.processing_status ~= criteria.status then
        match = false
      end
    end

    -- Check date range
    if match and criteria.since then
      local note_date = metadata.created_date or metadata.timestamp
      if note_date then
        local note_time = utils.parse_iso_datetime(note_date)
        local since_time = utils.parse_iso_datetime(criteria.since)
        if note_time and since_time then
          if os.time(note_time) < os.time(since_time) then
            match = false
          end
        end
      end
    end

    if match and criteria.until_date then
      local note_date = metadata.created_date or metadata.timestamp
      if note_date then
        local note_time = utils.parse_iso_datetime(note_date)
        local until_time = utils.parse_iso_datetime(criteria.until_date)
        if note_time and until_time then
          if os.time(note_time) > os.time(until_time) then
            match = false
          end
        end
      end
    end

    -- Check text search
    if match and criteria.query then
      local query_lower = criteria.query:lower()
      local text_match = false

      -- Search in title
      if metadata.title and metadata.title:lower():find(query_lower, 1, true) then
        text_match = true
      end

      -- Search in aliases
      if not text_match then
        for _, alias in ipairs(metadata.aliases or {}) do
          if alias:lower():find(query_lower, 1, true) then
            text_match = true
            break
          end
        end
      end

      -- Search in context
      if
        not text_match
        and metadata.context
        and metadata.context:lower():find(query_lower, 1, true)
      then
        text_match = true
      end

      if not text_match then
        match = false
      end
    end

    if match then
      table.insert(results, metadata)
    end
  end

  -- Sort results by modified time (newest first)
  table.sort(results, function(a, b)
    return (a.modified or 0) > (b.modified or 0)
  end)

  return results
end

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

-- Clear the index
function M.clear()
  index = {}
  M.save_index()
end

-- Function to get all PARA directories
function M.get_para_directories()
  local config = require("para-organize.config").get()
  local dirs = {}
  for _, type in ipairs({"projects", "areas", "resources", "archives"}) do
    local path = config.paths.vault_dir .. "/" .. type
    local handle = io.popen("find " .. path .. " -maxdepth 1 -type d")
    if handle then
      for line in handle:lines() do
        if line ~= path then
          local name = line:match(".*/(.+)")
          local modified = vim.fn.getftime(line)
          table.insert(dirs, {name = name, path = line, type = type, modified = modified})
        end
      end
      handle:close()
    end
  end
  return dirs
end

-- Function to get sub-items in a directory
function M.get_sub_items(dir_path)
  local items = {}
  local handle = io.popen("find " .. dir_path .. " -maxdepth 1")
  if handle then
    for line in handle:lines() do
      if line ~= dir_path then
        local name = line:match(".*/(.+)")
        local is_dir = vim.fn.isdirectory(line) == 1
        local item_type = is_dir and "directory" or "file"
        local alias = item_type == "file" and extract_alias(line) or nil
        table.insert(items, {name = name, path = line, type = item_type, alias = alias})
      end
    end
    handle:close()
  end
  table.sort(items, function(a, b) return a.name < b.name end)
  return items
end

-- Function to get directory by name
function M.get_directory_by_name(name)
  local dirs = M.get_para_directories()
  for _, dir in ipairs(dirs) do
    if dir.name == name then return dir end
  end
  return nil
end

-- Function to get file by alias or name
function M.get_file_by_alias_or_name(name)
  local dirs = M.get_para_directories()
  for _, dir in ipairs(dirs) do
    local items = M.get_sub_items(dir.path)
    for _, item in ipairs(items) do
      if item.type == "file" and (item.alias == name or item.name == name) then
        return item
      end
    end
  end
  return nil
end

-- Function to extract alias from file content (simplified for example)
function extract_alias(file_path)
  local content = utils.read_file(file_path) or ""
      local frontmatter_str, _ = utils.extract_frontmatter(content)
    if not frontmatter_str then return nil end

    local frontmatter = utils.parse_yaml_simple(frontmatter_str)
  if frontmatter.aliases and #frontmatter.aliases > 0 then
    return frontmatter.aliases[1]
  end
  return nil
end

-- Initialize on module load
M.init()

return M
