-- para-organize/utils/path.lua
-- Path utilities for para-organize.nvim

local Path = require("plenary.path")
local M = {}

function M.normalize_path(path)
  path = vim.fn.expand(path)
  path = vim.fn.fnamemodify(path, ":p")
  if #path > 1 and path:sub(-1) == "/" then path = path:sub(1, -2) end
  return path
end

function M.path_exists(path)
  return vim.fn.filereadable(path) == 1 or vim.fn.isdirectory(path) == 1
end

function M.is_directory(path)
  return vim.fn.isdirectory(path) == 1
end

function M.relative_path(base, target)
  local base_path = Path:new(base):absolute()
  local target_path = Path:new(target):absolute()
  local base_parts = vim.split(base_path, "/", { plain = true })
  local target_parts = vim.split(target_path, "/", { plain = true })
  local common_len = 0
  for i = 1, math.min(#base_parts, #target_parts) do
    if base_parts[i] == target_parts[i] then common_len = i else break end
  end
  local rel_parts = {}
  for i = common_len + 1, #base_parts do table.insert(rel_parts, "..") end
  for i = common_len + 1, #target_parts do table.insert(rel_parts, target_parts[i]) end
  if #rel_parts == 0 then return "." end
  return table.concat(rel_parts, "/")
end

function M.glob_files(pattern, base_dir)
  base_dir = base_dir or "."
  local cmd = string.format("find %s -type f -name '%s' 2>/dev/null", base_dir, pattern)
  local handle = io.popen(cmd)
  local files = {}
  if handle then for line in handle:lines() do table.insert(files, line) end; handle:close() end
  return files
end

return M
