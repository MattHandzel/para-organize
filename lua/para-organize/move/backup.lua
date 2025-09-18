-- para-organize/move/backup.lua
-- Backup utilities for para-organize.nvim

local config_mod = require("para-organize.config")
local utils = require("para-organize.utils")
local M = {}

function M.create_backup(filepath)
  local config = config_mod.get()
  if not config.file_ops.create_backups then return true end
  local vault_dir = config.paths.vault_dir
  local backup_dir = vault_dir .. "/" .. config.file_ops.backup_dir
  if vim.fn.isdirectory(backup_dir) == 0 then vim.fn.mkdir(backup_dir, "p") end
  local filename = vim.fn.fnamemodify(filepath, ":t")
  local timestamp = os.date("%Y%m%d_%H%M%S")
  local backup_path = backup_dir .. "/" .. timestamp .. "_" .. filename
  if utils.copy_file(filepath, backup_path) then
    utils.log("DEBUG", "Created backup: %s", backup_path)
    return true, backup_path
  else
    utils.log("ERROR", "Failed to create backup for: %s", filepath)
    return false, nil
  end
end

return M
