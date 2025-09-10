-- lua/para_org/utils.lua

local M = {}

--- A basic YAML frontmatter parser.
-- It extracts key-value pairs from the frontmatter block.
-- NOTE: This is not a full YAML parser and has limitations.
--
-- @param content The file content to parse.
-- @return A table with the parsed frontmatter, and a string with the rest of the content.
function M.parse_frontmatter(content)
  local config = require('para_org.config').options
  local start_delim, end_delim = unpack(config.patterns.frontmatter_delimiters)
  local _, start_pos = content:find(start_delim, 1, true)
  if not start_pos then
    return {}, content
  end

  local fm_end, end_pos = content:find(end_delim, start_pos + 1, true)
  if not fm_end then
    return {}, content
  end

  local yaml_str = content:sub(start_pos + 1, end_pos - #end_delim - 1)

  local metadata = {}
  for line in yaml_str:gmatch('[^\n]+') do
    local key, value = line:match('^([^:]+):%s*(.*)$')
    if key and value then
      key = key:gsub('^%s*', ''):gsub('%s*$', '')
      value = value:gsub('^%s*', ''):gsub('%s*$', '')
      -- This doesn't handle complex types, just strings.
      metadata[key] = value
    end
  end
  return metadata, content:sub(end_pos + 1)
end

return M
