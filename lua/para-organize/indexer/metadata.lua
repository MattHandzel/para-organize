-- para-organize/indexer/metadata.lua
-- Metadata extraction and helpers for PARA organize indexer

local utils = require("para-organize.utils")
local M = {}

-- Helper to ensure a value is a list
local function ensure_list(value)
  if type(value) == "table" then
    return value
  elseif value == nil then
    return {}
  else
    return { value }
  end
end

-- Extract metadata from a note file
function M.extract_metadata(filepath)
  local config = require("para-organize.config").get()
  local content = utils.read_file(filepath)
  if not content then return nil end
  if #content > config.indexing.max_file_size then
    utils.log("WARN", "File too large, skipping: %s", filepath)
    return nil
  end
  local ok, result = pcall(function()
    local frontmatter_str, body = utils.extract_frontmatter(content, config.patterns.frontmatter_delimiters)
    utils.log("TRACE", "Frontmatter for %s: %s", filepath, frontmatter_str or "none")
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
  local title = nil
  if body then
    title = body:match("^#%s+(.-)%s*\n")
  end
  if not title then
    title = vim.fn.fnamemodify(filepath, ":t:r")
  end
  local get_para_type = M.get_para_type or function() return nil end
  local para_type = get_para_type(filepath)
  local stat = vim.loop.fs_stat(filepath)
  local metadata = {
    path = filepath,
    filename = vim.fn.fnamemodify(filepath, ":t"),
    title = title,
    para_type = para_type,
    folder = vim.fn.fnamemodify(filepath, ":h:t"),
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
    size = stat and stat.size or 0,
    modified = stat and stat.mtime and stat.mtime.sec or 0,
    indexed_at = os.time(),
  }
  metadata.tags = ensure_list(metadata.tags)
  metadata.aliases = ensure_list(metadata.aliases)
  metadata.sources = ensure_list(metadata.sources)
  metadata.modalities = ensure_list(metadata.modalities)
  if config.patterns.tag_normalization then
    local normalized_tags = {}
    for _, tag in ipairs(metadata.tags) do
      local normalized = utils.normalize_tag(tag)
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

M.ensure_list = ensure_list
return M
