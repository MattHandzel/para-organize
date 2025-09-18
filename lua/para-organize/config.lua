-- para-organize/config.lua
-- Configuration management for para-organize.nvim

local M = {}

local defaults_mod = require("para-organize.config.defaults")
local validate_mod = require("para-organize.config.validate")
local parse_mod = require("para-organize.config.parse")

local config = {}

-- Setup configuration
function M.setup(user_config)
  config = parse_mod.parse(user_config)

  -- Expand paths
  config.paths.vault_dir = vim.fn.expand(config.paths.vault_dir)
  config.file_ops.log_file = vim.fn.expand(config.file_ops.log_file)
  config.debug.log_file = vim.fn.expand(config.debug.log_file)

  -- Create necessary directories
  local data_dir = vim.fn.stdpath("data") .. "/para-organize"
  if vim.fn.isdirectory(data_dir) == 0 then
    vim.fn.mkdir(data_dir, "p")
  end

  return config
end

-- Get current configuration
function M.get()
  if vim.tbl_isempty(config) then
    M.setup({})
  end
  return config
end

-- Get specific configuration value
function M.get_value(path)
  local conf = M.get()
  local keys = vim.split(path, ".", { plain = true })

  local value = conf
  for _, key in ipairs(keys) do
    if type(value) == "table" then
      value = value[key]
    else
      return nil
    end
  end

  return value
end

-- Update configuration value
function M.set_value(path, value)
  local keys = vim.split(path, ".", { plain = true })
  local conf = M.get()

  local target = conf
  for i = 1, #keys - 1 do
    local key = keys[i]
    if type(target[key]) ~= "table" then
      target[key] = {}
    end
    target = target[key]
  end

  target[keys[#keys]] = value
end

-- Get vault directory
function M.get_vault_dir()
  return M.get_value("paths.vault_dir")
end

-- Get capture folder path
function M.get_capture_folder()
  local vault = M.get_vault_dir()
  local capture = M.get_value("paths.capture_folder")
  return vault .. "/" .. capture
end

-- Get PARA folder path
function M.get_para_folder(folder_type)
  local vault = M.get_vault_dir()
  local folder = M.get_value("paths.para_folders." .. folder_type)
  if folder then
    return vault .. "/" .. folder
  end
  return nil
end

-- Get all PARA folders
function M.get_para_folders()
  local vault = M.get_vault_dir()
  local folders = M.get_value("paths.para_folders")
  local result = {}

  for key, folder in pairs(folders) do
    result[key] = vault .. "/" .. folder
  end

  return result
end

-- Get archive path for a capture
function M.get_archive_path(filename)
  if not filename then
    require("para-organize.utils").log("ERROR", "get_archive_path called with nil filename")
    return nil
  end

  local vault = M.get_vault_dir()
  local archives = M.get_value("paths.para_folders.archives")
  local archive_capture = M.get_value("paths.archive_capture_path")

  local archive_dir = vault .. "/" .. archives .. "/" .. archive_capture

  -- Create archive directory if it doesn't exist
  if vim.fn.isdirectory(archive_dir) == 0 then
    vim.fn.mkdir(archive_dir, "p")
  end

  -- Handle filename collision with timestamp
  local base_name = filename
  local extension = ""

  local dot_pos = filename:find("%.[^%.]+$")
  if dot_pos then
    base_name = filename:sub(1, dot_pos - 1)
    extension = filename:sub(dot_pos)
  end

  local final_path = archive_dir .. "/" .. filename
  if vim.fn.filereadable(final_path) == 1 then
    local timestamp = os.date("%Y%m%d_%H%M%S")
    final_path = archive_dir .. "/" .. base_name .. "_" .. timestamp .. extension
  end

  return final_path
end

-- Check if debug mode is enabled
function M.is_debug()
  return M.get_value("debug.enabled") == true
end

-- Get log level
function M.get_log_level()
  local levels = {
    trace = vim.log.levels.TRACE,
    debug = vim.log.levels.DEBUG,
    info = vim.log.levels.INFO,
    warn = vim.log.levels.WARN,
    error = vim.log.levels.ERROR,
  }

  local level = M.get_value("debug.log_level") or "info"
  return levels[level] or vim.log.levels.INFO
end

-- Print debug info about current configuration
function M.debug_info()
  local utils = require("para-organize.utils")
  local conf = M.get()

  local capture_path = M.get_capture_folder()

  local info = {
    vault_dir = vim.fn.expand(conf.paths.vault_dir),
    capture_folder = conf.paths.capture_folder,
    capture_path = capture_path,
    capture_exists = vim.fn.isdirectory(capture_path) == 1,
    para_folders = conf.paths.para_folders,
    file_glob = conf.patterns.file_glob,
    debug_enabled = M.is_debug(),
    debug_level = M.get_value("debug.log_level"),
  }

  utils.log("INFO", "========== Configuration Debug Info ==========")
  utils.log("INFO", "Vault directory: %s", info.vault_dir)
  utils.log("INFO", "Capture folder (relative): %s", info.capture_folder)
  utils.log("INFO", "Capture path (full): %s", info.capture_path)
  utils.log("INFO", "Capture path exists: %s", tostring(info.capture_exists))
  utils.log("INFO", "File glob pattern: %s", info.file_glob)
  utils.log("INFO", "Debug enabled: %s", tostring(info.debug_enabled))
  utils.log("INFO", "Debug level: %s", tostring(info.debug_level))

  -- Check if directories exist and count files
  local dirs_to_check = {
    { name = "vault_dir", path = vim.fn.expand(conf.paths.vault_dir) },
    { name = "capture_path", path = capture_path },
  }

  utils.log("INFO", "\nDirectory Analysis:")
  for _, dir_info in ipairs(dirs_to_check) do
    local path = dir_info.path
    local exists = vim.fn.isdirectory(path) == 1
    local file_count = 0
    local md_files = {}

    if exists then
      -- Count markdown files
      local handle = io.popen('find "' .. path .. '" -name "*.md" 2>/dev/null')
      if handle then
        for line in handle:lines() do
          file_count = file_count + 1
          if file_count <= 5 then
            table.insert(md_files, line)
          end
        end
        handle:close()
      end
    end

    utils.log("INFO", "%s: %s", dir_info.name, path)
    utils.log("INFO", "  Exists: %s, .md files: %d", tostring(exists), file_count)
    if #md_files > 0 then
      utils.log("INFO", "  Sample files:")
      for _, file in ipairs(md_files) do
        utils.log("INFO", "    - %s", file)
      end
    end
  end

  utils.log("INFO", "===============================================")

  return info
end

return M
