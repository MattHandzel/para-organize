-- para-organize/health.lua
-- Health checks for para-organize.nvim

local M = {}

-- Check if a module is available
local function check_module(name, optional)
  local ok, _ = pcall(require, name)
  if ok then
    return true, string.format("✓ %s found", name)
  else
    if optional then
      return true, string.format("○ %s not found (optional)", name)
    else
      return false, string.format("✗ %s not found (required)", name)
    end
  end
end

-- Check if a command exists
local function check_command(cmd)
  if vim.fn.executable(cmd) == 1 then
    return true, string.format("✓ %s found", cmd)
  else
    return false, string.format("✗ %s not found", cmd)
  end
end

-- Check directory permissions
local function check_directory(path, writable)
  path = vim.fn.expand(path)
  
  if vim.fn.isdirectory(path) == 0 then
    if writable then
      -- Try to create it
      local ok = pcall(vim.fn.mkdir, path, "p")
      if ok then
        return true, string.format("✓ %s created", path)
      else
        return false, string.format("✗ %s does not exist and cannot be created", path)
      end
    else
      return false, string.format("✗ %s does not exist", path)
    end
  end
  
  if writable then
    -- Check write permission
    local test_file = path .. "/.para_organize_test"
    local file = io.open(test_file, "w")
    if file then
      file:close()
      os.remove(test_file)
      return true, string.format("✓ %s is writable", path)
    else
      return false, string.format("✗ %s is not writable", path)
    end
  else
    return true, string.format("✓ %s exists", path)
  end
end

-- Main health check function
function M.check()
  local health = vim.health or require("health")
  
  health.start("para-organize.nvim")
  
  -- Check Neovim version
  local nvim_version = vim.version()
  if nvim_version.major == 0 and nvim_version.minor >= 9 then
    health.ok(string.format("Neovim version %d.%d.%d", 
      nvim_version.major, nvim_version.minor, nvim_version.patch))
  else
    health.error(string.format("Neovim version %d.%d.%d is too old. Requires >= 0.9.0",
      nvim_version.major, nvim_version.minor, nvim_version.patch))
  end
  
  -- Check Lua version
  if jit then
    health.ok(string.format("LuaJIT version %s", jit.version))
  else
    health.ok(string.format("Lua version %s", _VERSION))
  end
  
  -- Check required dependencies
  health.start("Required Dependencies")
  
  local deps = {
    { "plenary.nvim", "plenary", false },
    { "telescope.nvim", "telescope", false },
  }
  
  for _, dep in ipairs(deps) do
    local ok, msg = check_module(dep[2], dep[3])
    if ok then
      health.ok(msg)
    else
      health.error(msg .. " - Install with: " .. dep[1])
    end
  end
  
  -- Check optional dependencies
  health.start("Optional Dependencies")
  
  local opt_deps = {
    { "nui.nvim", "nui", true },
    { "which-key.nvim", "which-key", true },
    { "nvim-web-devicons", "nvim-web-devicons", true },
  }
  
  for _, dep in ipairs(opt_deps) do
    local ok, msg = check_module(dep[2], dep[3])
    if ok then
      health.ok(msg)
    else
      health.info(msg .. " - Install for better experience: " .. dep[1])
    end
  end
  
  -- Check system commands
  health.start("System Commands")
  
  local cmds = { "find", "grep" }
  for _, cmd in ipairs(cmds) do
    local ok, msg = check_command(cmd)
    if ok then
      health.ok(msg)
    else
      health.warn(msg .. " - Some features may not work")
    end
  end
  
  -- Check configuration
  health.start("Configuration")
  
  local config = require("para-organize.config").get()
  
  -- Check vault directory
  local vault_ok, vault_msg = check_directory(config.paths.vault_dir, false)
  if vault_ok then
    health.ok(vault_msg)
  else
    health.error(vault_msg .. " - Please set a valid vault_dir in config")
  end
  
  -- Check PARA folders
  if vault_ok then
    for folder_type, folder_name in pairs(config.paths.para_folders) do
      local folder_path = config.paths.vault_dir .. "/" .. folder_name
      local ok, msg = check_directory(folder_path, true)
      if ok then
        health.ok(string.format("PARA %s folder: %s", folder_type, msg))
      else
        health.warn(string.format("PARA %s folder: %s", folder_type, msg))
      end
    end
    
    -- Check capture folder
    local capture_path = config.paths.vault_dir .. "/" .. config.paths.capture_folder
    local ok, msg = check_directory(capture_path, true)
    if ok then
      health.ok("Capture folder: " .. msg)
    else
      health.warn("Capture folder: " .. msg)
    end
  end
  
  -- Check data directory
  local data_dir = vim.fn.stdpath("data") .. "/para-organize"
  local data_ok, data_msg = check_directory(data_dir, true)
  if data_ok then
    health.ok("Data directory: " .. data_msg)
  else
    health.error("Data directory: " .. data_msg)
  end
  
  -- Check log file if debug is enabled
  if config.debug.enabled then
    local log_dir = vim.fn.fnamemodify(config.debug.log_file, ":h")
    local log_ok, log_msg = check_directory(log_dir, true)
    if log_ok then
      health.ok("Log directory: " .. log_msg)
    else
      health.warn("Log directory: " .. log_msg)
    end
  end
  
  -- Check index backend
  health.start("Index Backend")
  
  if config.indexing.backend == "sqlite" then
    local sqlite_ok = check_module("sqlite", true)
    if sqlite_ok then
      health.ok("SQLite backend available")
    else
      health.warn("SQLite not available, falling back to JSON backend")
    end
  else
    health.ok("Using JSON backend")
  end
  
  -- Performance checks
  health.start("Performance")
  
  -- Count notes in vault
  if vault_ok then
    local note_count = 0
    local pattern = config.patterns.file_glob
    local find_cmd = string.format("find '%s' -type f -name '*.md' 2>/dev/null | wc -l",
      config.paths.vault_dir)
    local handle = io.popen(find_cmd)
    if handle then
      note_count = tonumber(handle:read("*a")) or 0
      handle:close()
    end
    
    if note_count > 0 then
      health.ok(string.format("Found %d markdown files in vault", note_count))
      
      if note_count > 5000 then
        health.warn("Large number of files may affect performance. Consider using SQLite backend.")
      end
    else
      health.info("No markdown files found in vault")
    end
  end
  
  -- Memory usage
  local memory = collectgarbage("count")
  health.ok(string.format("Lua memory usage: %.2f MB", memory / 1024))
  
  -- Summary
  health.start("Summary")
  health.info("Run :ParaOrganize to start organizing your notes!")
  health.info("See :help para-organize for documentation")
end

return M
