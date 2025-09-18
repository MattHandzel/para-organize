-- para-organize/config/parse.lua
-- Configuration parsing and merging for PARA organize

local defaults_mod = require("para-organize.config.defaults")
local validate_mod = require("para-organize.config.validate")
local M = {}

function M.parse(user_config)
  local config = vim.tbl_deep_extend("force", {}, defaults_mod.defaults, user_config or {})
  validate_mod.validate(config)
  return config
end

return M
