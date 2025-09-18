-- para-organize/config/validate.lua
-- Configuration validation for PARA organize

local M = {}

function M.validate(config)
  assert(type(config) == "table", "Config must be a table")
  assert(type(config.ui) == "table", "Config.ui must be a table")
  assert(type(config.paths) == "table", "Config.paths must be a table")
  assert(type(config.indexing) == "table", "Config.indexing must be a table")
  assert(type(config.keymaps) == "table", "Config.keymaps must be a table")
  assert(type(config.patterns) == "table", "Config.patterns must be a table")
  -- Add more detailed checks as needed
  return true
end

return M
