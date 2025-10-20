-- para-organize/move/frontmatter.lua
-- Frontmatter/tag update utilities for para-organize.nvim

local config_mod = require("para-organize.config")
local utils = require("para-organize.utils")
local M = {}

function M.update_tags(filepath, new_tags)
  local config = config_mod.get()
  local content = utils.read_file(filepath)
  if not content then return false, "Failed to read file" end
  local frontmatter_str, body = utils.extract_frontmatter(content, config.patterns.frontmatter_delimiters)
  local frontmatter = {}
  if frontmatter_str then frontmatter = utils.parse_yaml_simple(frontmatter_str) end
  local existing_tags = frontmatter.tags or {}
  local tag_set = {}
  for _, tag in ipairs(existing_tags) do tag_set[utils.normalize_tag(tag)] = tag end
  for _, tag in ipairs(new_tags) do
    local normalized = utils.normalize_tag(tag)
    if not tag_set[normalized] then tag_set[normalized] = tag end
  end
  local final_tags = {}
  for _, tag in pairs(tag_set) do table.insert(final_tags, tag) end
  table.sort(final_tags)
  frontmatter.tags = final_tags
  frontmatter.last_edited_date = os.date("%Y-%m-%d")
  local new_frontmatter = M.build_frontmatter(frontmatter)
  local delimiters = config.patterns.frontmatter_delimiters
  local new_content = delimiters[1].."\n"..new_frontmatter.."\n"..delimiters[2].."\n"..body
  if config.file_ops.atomic_writes then
    return utils.write_file_atomic(filepath, new_content)
  else
    local file = io.open(filepath, "w")
    if file then file:write(new_content); file:close(); return true end
    return false
  end
end

function M.build_frontmatter(data)
  local lines = {}
  local field_order = { "timestamp", "id", "aliases", "capture_id", "tags", "sources", "modalities", "context", "location", "metadata", "processing_status", "created_date", "last_edited_date" }
  for _, field in ipairs(field_order) do
    local value = data[field]
    if value ~= nil then
      if type(value) == "table" then
        if #value > 0 then
          table.insert(lines, field .. ":")
          for _, item in ipairs(value) do table.insert(lines, "  - " .. tostring(item)) end
        elseif next(value) then
          table.insert(lines, field .. ":")
          for k, v in pairs(value) do table.insert(lines, "  " .. tostring(k) .. ": " .. tostring(v)) end
        end
      else
        table.insert(lines, field .. ": " .. tostring(value))
      end
    end
  end
  return table.concat(lines, "\n")
end

return M
