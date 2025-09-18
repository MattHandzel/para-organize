-- para-organize/indexer/persistence.lua
-- Index persistence logic for PARA organize indexer

local utils = require("para-organize.utils")
local M = {}

-- Load index from disk
function M.load_index(index_file)
  if not utils.path_exists(index_file) then
    return {}
  end
  local content = utils.read_file(index_file)
  if content then
    local ok, data = pcall(vim.json.decode, content)
    if ok and type(data) == "table" then
      utils.log("DEBUG", "Loaded index with %d entries", vim.tbl_count(data))
      return data
    else
      utils.log("ERROR", "Failed to parse index file")
      return {}
    end
  end
  return {}
end

-- Save index to disk
function M.save_index(index, index_file)
  local index_dir = vim.fn.fnamemodify(index_file, ":h")
  if vim.fn.isdirectory(index_dir) == 0 then
    vim.fn.mkdir(index_dir, "p")
  end
  local content = vim.json.encode(index)
  if utils.write_file_atomic(index_file, content) then
    utils.log("DEBUG", "Saved index with %d entries", vim.tbl_count(index))
    return true
  else
    utils.log("ERROR", "Failed to save index")
    return false
  end
end

-- Clear the index
function M.clear_index(index_file)
  utils.write_file_atomic(index_file, vim.json.encode({}))
end

return M
