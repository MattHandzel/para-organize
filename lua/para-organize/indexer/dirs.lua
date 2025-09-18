-- para-organize/indexer/dirs.lua
-- Directory and file helpers for PARA organize indexer

local utils = require("para-organize.utils")
local M = {}

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

function M.get_sub_items(dir_path)
  local items = {}
  local handle = io.popen("find " .. dir_path .. " -maxdepth 1")
  if handle then
    for line in handle:lines() do
      if line ~= dir_path then
        local name = line:match(".*/(.+)")
        local is_dir = vim.fn.isdirectory(line) == 1
        local item_type = is_dir and "directory" or "file"
        local alias = item_type == "file" and M.extract_alias(line) or nil
        table.insert(items, {name = name, path = line, type = item_type, alias = alias})
      end
    end
    handle:close()
  end
  table.sort(items, function(a, b) return a.name < b.name end)
  return items
end

function M.get_directory_by_name(name)
  local dirs = M.get_para_directories()
  for _, dir in ipairs(dirs) do
    if dir.name == name then return dir end
  end
  return nil
end

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

function M.extract_alias(file_path)
  local content = utils.read_file(file_path) or ""
  local frontmatter_str, _ = utils.extract_frontmatter(content)
  if not frontmatter_str then return nil end
  local frontmatter = utils.parse_yaml_simple(frontmatter_str)
  if frontmatter.aliases and #frontmatter.aliases > 0 then
    return frontmatter.aliases[1]
  end
  return nil
end

return M
