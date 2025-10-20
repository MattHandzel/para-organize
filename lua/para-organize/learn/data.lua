-- para-organize/learn/data.lua
-- Persistence for learning system

local M = {}
local utils = require("para-organize.utils")

function M.load_data(learning_file)
  if not utils.path_exists(learning_file) then
    return {
      associations = {},
      patterns = {},
      statistics = { total_moves = 0, destinations = {}, last_updated = os.time() },
    }
  end
  local content = utils.read_file(learning_file)
  if content then
    local ok, data = pcall(vim.json.decode, content)
    if ok and type(data) == "table" then
      return data
    end
  end
  return {
    associations = {},
    patterns = {},
    statistics = { total_moves = 0, destinations = {}, last_updated = os.time() },
  }
end

function M.save_data(learning_file, learning_data)
  local learning_dir = vim.fn.fnamemodify(learning_file, ":h")
  if vim.fn.isdirectory(learning_dir) == 0 then vim.fn.mkdir(learning_dir, "p") end
  learning_data.statistics.last_updated = os.time()
  local content = vim.json.encode(learning_data)
  if utils.write_file_atomic(learning_file, content) then
    utils.log("DEBUG", "Saved learning data")
  else
    utils.log("ERROR", "Failed to save learning data")
  end
end

return M
