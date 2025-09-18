-- para-organize/move/log.lua
-- Operation logging utilities for para-organize.nvim

local M = {}
local operation_log = {}
local log_file = nil

function M.init(config)
  log_file = config.file_ops.log_file
  local log_dir = vim.fn.fnamemodify(log_file, ":h")
  if vim.fn.isdirectory(log_dir) == 0 then vim.fn.mkdir(log_dir, "p") end
end

function M.log_operation(operation_type, source, destination, success, error_msg)
  local entry = {
    timestamp = os.time(),
    type = operation_type,
    source = source,
    destination = destination,
    success = success,
    error = error_msg,
  }
  table.insert(operation_log, entry)
  if log_file then
    local log_line = string.format(
      "[%s] %s: %s -> %s [%s]%s\n",
      os.date("%Y-%m-%d %H:%M:%S", entry.timestamp),
      operation_type,
      source,
      destination,
      success and "SUCCESS" or "FAILED",
      error_msg and (" Error: " .. error_msg) or ""
    )
    local file = io.open(log_file, "a")
    if file then file:write(log_line); file:close() end
  end
  return entry
end

function M.get_undo_info()
  if #operation_log == 0 then return nil end
  return operation_log[#operation_log]
end

return M
