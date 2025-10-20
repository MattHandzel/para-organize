-- para-organize/utils/log.lua
-- Logging utilities for para-organize.nvim

local M = {}

local log_levels = {
  TRACE = 0,
  DEBUG = 1,
  INFO = 2,
  WARN = 3,
  ERROR = 4,
}

local current_log_level = log_levels.INFO
local log_file = nil

function M.init_logging(config)
  if config.debug and config.debug.enabled then
    current_log_level = log_levels[string.upper(config.debug.log_level)] or log_levels.INFO
    log_file = config.debug.log_file
    local log_dir = vim.fn.fnamemodify(log_file, ":h")
    if vim.fn.isdirectory(log_dir) == 0 then
      vim.fn.mkdir(log_dir, "p")
    end
  end
end

function M.log(level, msg, ...)
  if log_levels[level] < current_log_level then return end
  local formatted_msg = string.format(msg, ...)
  local timestamp = os.date("%Y-%m-%d %H:%M:%S")
  local log_line = string.format("[%s] [%s] %s", timestamp, level, formatted_msg)
  if log_file then
    local file = io.open(log_file, "a")
    if file then file:write(log_line .. "\n"); file:close() end
  end
  if log_levels[level] >= log_levels.WARN then
    local vim_level = vim.log.levels.INFO
    if level == "ERROR" then vim_level = vim.log.levels.ERROR
    elseif level == "WARN" then vim_level = vim.log.levels.WARN end
    vim.notify(formatted_msg, vim_level, { title = "para-organize" })
  end
end

return M
