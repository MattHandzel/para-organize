-- para-organize/suggest/folders.lua
-- Folder utilities for suggestion engine

local M = {}

function M.get_subfolders(folder_path)
  local utils = require("para-organize.utils")
  local subfolders = {}
  if not utils.is_directory(folder_path) then return subfolders end
  local cmd = string.format("find '%s' -mindepth 1 -maxdepth 1 -type d 2>/dev/null", folder_path)
  local handle = io.popen(cmd)
  if handle then
    for line in handle:lines() do
      local name = vim.fn.fnamemodify(line, ":t")
      table.insert(subfolders, {
        path = line,
        name = name,
        normalized_name = utils.normalize_tag(name),
      })
    end
    handle:close()
  end
  return subfolders
end

return M
