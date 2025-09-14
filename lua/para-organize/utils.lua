-- para-organize/utils.lua
-- Utility functions for para-organize.nvim

local M = {}

-- Dependencies
local Path = require("plenary.path")
local Job = require("plenary.job")

-- Logging functionality
local log_levels = {
  TRACE = 0,
  DEBUG = 1,
  INFO = 2,
  WARN = 3,
  ERROR = 4,
}

local current_log_level = log_levels.INFO
local log_file = nil

-- Initialize logging
function M.init_logging(config)
  if config.debug and config.debug.enabled then
    current_log_level = log_levels[string.upper(config.debug.log_level)] or log_levels.INFO
    log_file = config.debug.log_file
    
    -- Ensure log directory exists
    local log_dir = vim.fn.fnamemodify(log_file, ":h")
    if vim.fn.isdirectory(log_dir) == 0 then
      vim.fn.mkdir(log_dir, "p")
    end
  end
end

-- Log a message
function M.log(level, msg, ...)
  if log_levels[level] < current_log_level then
    return
  end
  
  local formatted_msg = string.format(msg, ...)
  local timestamp = os.date("%Y-%m-%d %H:%M:%S")
  local log_line = string.format("[%s] [%s] %s", timestamp, level, formatted_msg)
  
  -- Write to log file if configured
  if log_file then
    local file = io.open(log_file, "a")
    if file then
      file:write(log_line .. "\n")
      file:close()
    end
  end
  
  -- Also notify in Neovim for important messages
  if log_levels[level] >= log_levels.WARN then
    local vim_level = vim.log.levels.INFO
    if level == "ERROR" then
      vim_level = vim.log.levels.ERROR
    elseif level == "WARN" then
      vim_level = vim.log.levels.WARN
    end
    vim.notify(formatted_msg, vim_level, { title = "para-organize" })
  end
end

-- Path utilities

-- Normalize a path
function M.normalize_path(path)
  path = vim.fn.expand(path)
  path = vim.fn.fnamemodify(path, ":p")
  -- Remove trailing slash except for root
  if #path > 1 and path:sub(-1) == "/" then
    path = path:sub(1, -2)
  end
  return path
end

-- Check if a path exists
function M.path_exists(path)
  return vim.fn.filereadable(path) == 1 or vim.fn.isdirectory(path) == 1
end

-- Check if a path is a directory
function M.is_directory(path)
  return vim.fn.isdirectory(path) == 1
end

-- Get the relative path from base to target
function M.relative_path(base, target)
  local base_path = Path:new(base):absolute()
  local target_path = Path:new(target):absolute()
  
  -- Find common prefix
  local base_parts = vim.split(base_path, "/", { plain = true })
  local target_parts = vim.split(target_path, "/", { plain = true })
  
  local common_len = 0
  for i = 1, math.min(#base_parts, #target_parts) do
    if base_parts[i] == target_parts[i] then
      common_len = i
    else
      break
    end
  end
  
  -- Build relative path
  local rel_parts = {}
  
  -- Add ".." for each remaining base part
  for i = common_len + 1, #base_parts do
    table.insert(rel_parts, "..")
  end
  
  -- Add remaining target parts
  for i = common_len + 1, #target_parts do
    table.insert(rel_parts, target_parts[i])
  end
  
  if #rel_parts == 0 then
    return "."
  end
  
  return table.concat(rel_parts, "/")
end

-- Get all files matching a pattern
function M.glob_files(pattern, base_dir)
  base_dir = base_dir or "."
  local cmd = string.format("find %s -type f -name '%s' 2>/dev/null", base_dir, pattern)
  local handle = io.popen(cmd)
  local files = {}
  
  if handle then
    for line in handle:lines() do
      table.insert(files, line)
    end
    handle:close()
  end
  
  return files
end

-- String utilities

-- Trim whitespace from string
function M.trim(s)
  return s:match("^%s*(.-)%s*$")
end

-- Split string by delimiter
function M.split(str, delimiter)
  delimiter = delimiter or ","
  local result = {}
  local pattern = string.format("([^%s]+)", delimiter)
  
  for match in string.gmatch(str, pattern) do
    table.insert(result, M.trim(match))
  end
  
  return result
end

-- Normalize tags (lowercase, replace spaces with hyphens)
function M.normalize_tag(tag)
  tag = tag:lower()
  tag = tag:gsub("%s+", "-")
  tag = tag:gsub("[^%w%-]", "-") -- Replace special chars with hyphens
  -- Remove consecutive hyphens
  tag = tag:gsub("%-+", "-")
  -- Remove leading/trailing hyphens
  tag = tag:gsub("^%-+", "")
  tag = tag:gsub("%-+$", "")
  return tag
end

-- Calculate string similarity (simple Levenshtein distance)
function M.string_similarity(s1, s2)
  local len1, len2 = #s1, #s2
  local matrix = {}
  
  -- Initialize matrix
  for i = 0, len1 do
    matrix[i] = { [0] = i }
  end
  for j = 0, len2 do
    matrix[0][j] = j
  end
  
  -- Calculate distances
  for i = 1, len1 do
    for j = 1, len2 do
      local cost = s1:sub(i, i) == s2:sub(j, j) and 0 or 1
      matrix[i][j] = math.min(
        matrix[i-1][j] + 1,      -- deletion
        matrix[i][j-1] + 1,      -- insertion
        matrix[i-1][j-1] + cost  -- substitution
      )
    end
  end
  
  -- Return normalized similarity (0 to 1)
  local distance = matrix[len1][len2]
  local max_len = math.max(len1, len2)
  if max_len == 0 then return 1.0 end
  return 1.0 - (distance / max_len)
end

-- Date/Time utilities

-- Parse ISO 8601 datetime
function M.parse_iso_datetime(datetime_str)
  if not datetime_str then return nil end
  
  -- Match ISO 8601 format: YYYY-MM-DDTHH:MM:SS[Z|+HH:MM|-HH:MM]
  local year, month, day, hour, min, sec = 
    datetime_str:match("(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
  
  if year then
    return {
      year = tonumber(year),
      month = tonumber(month),
      day = tonumber(day),
      hour = tonumber(hour),
      min = tonumber(min),
      sec = tonumber(sec),
    }
  end
  
  return nil
end

-- Format datetime for display
function M.format_datetime(datetime, format_str)
  format_str = format_str or "%Y-%m-%d %H:%M:%S"
  
  if type(datetime) == "string" then
    datetime = M.parse_iso_datetime(datetime)
  end
  
  if datetime then
    local time = os.time(datetime)
    return os.date(format_str, time)
  end
  
  return ""
end

-- Get relative time string (e.g., "2 hours ago")
function M.relative_time(datetime)
  if type(datetime) == "string" then
    datetime = M.parse_iso_datetime(datetime)
  end
  
  if not datetime then return "" end
  
  local now = os.time()
  local time_then = os.time(datetime)
  local diff = now - time_then
  
  if diff < 60 then
    return "just now"
  elseif diff < 3600 then
    local mins = math.floor(diff / 60)
    return string.format("%d minute%s ago", mins, mins == 1 and "" or "s")
  elseif diff < 86400 then
    local hours = math.floor(diff / 3600)
    return string.format("%d hour%s ago", hours, hours == 1 and "" or "s")
  elseif diff < 604800 then
    local days = math.floor(diff / 86400)
    return string.format("%d day%s ago", days, days == 1 and "" or "s")
  else
    return M.format_datetime(datetime, "%b %d, %Y")
  end
end

-- File operations

-- Read file contents
function M.read_file(path)
  local file = io.open(path, "r")
  if not file then return nil end
  
  local content = file:read("*all")
  file:close()
  return content
end

-- Write file contents atomically
function M.write_file_atomic(path, content)
  -- Write to temporary file first
  local temp_path = path .. ".tmp" .. os.time()
  local file = io.open(temp_path, "w")
  if not file then
    M.log("ERROR", "Failed to write to temporary file: %s", temp_path)
    return false
  end
  
  file:write(content)
  file:close()
  
  -- Rename temporary file to final path
  local ok = os.rename(temp_path, path)
  if not ok then
    M.log("ERROR", "Failed to rename %s to %s", temp_path, path)
    os.remove(temp_path)
    return false
  end
  
  return true
end

-- Copy file
function M.copy_file(source, dest)
  local source_content = M.read_file(source)
  if not source_content then
    M.log("ERROR", "Failed to read source file: %s", source)
    return false
  end
  
  -- Ensure destination directory exists
  local dest_dir = vim.fn.fnamemodify(dest, ":h")
  if vim.fn.isdirectory(dest_dir) == 0 then
    vim.fn.mkdir(dest_dir, "p")
  end
  
  return M.write_file_atomic(dest, source_content)
end

-- Move file (copy then delete)
function M.move_file(source, dest)
  if M.copy_file(source, dest) then
    os.remove(source)
    return true
  end
  return false
end

-- YAML frontmatter parsing

-- Extract frontmatter from content (robust implementation)
-- Returns: frontmatter string (without delimiters) or nil, and the remaining body
function M.extract_frontmatter(content, delimiters)
  delimiters = delimiters or { "---", "---" }
  local start_delim = delimiters[1]
  local end_delim = delimiters[2] or start_delim

  -- Early-exit when the note does not start with the start delimiter
  if content:sub(1, #start_delim) ~= start_delim then
    return nil, content
  end

  -- Split into lines so we can cope with different newline conventions (\n / \r\n)
  local lines = vim.split(content, "\n", { plain = true })
  if #lines == 0 or M.trim(lines[1]) ~= start_delim then
    return nil, content
  end

  local frontmatter_lines = {}
  local end_idx = nil
  for i = 2, #lines do
    local l = M.trim(lines[i])
    if l == end_delim then
      end_idx = i
      break
    end
    frontmatter_lines[#frontmatter_lines + 1] = lines[i]
  end

  if not end_idx then
    -- No closing delimiter – treat the whole document as body
    return nil, content
  end

  local frontmatter = table.concat(frontmatter_lines, "\n")
  local body_lines = {}
  for j = end_idx + 1, #lines do
    body_lines[#body_lines + 1] = lines[j]
  end
  local body = table.concat(body_lines, "\n")
  return frontmatter, body
end

-- Parse YAML frontmatter (still *simple* but now supports nested maps and lists)
-- NOTE: This is **NOT** a full YAML parser – it supports the subset of YAML we
-- need for note front-matter (scalars, nested maps, and lists indicated by "-").
function M.parse_yaml_simple(yaml_str)
  if not yaml_str or yaml_str == "" then
    return {}
  end

  local root = {}
  local stack = { root }          -- value stack corresponding to indentation levels
  local indent_stack = { 0 }      -- indentation widths (number of leading spaces)

  -- Helper to coerce scalars
  local function to_scalar(val)
    val = M.trim(val)
    if val == "" then
      return ""
    end
    if val == "true" then return true end
    if val == "false" then return false end
    local num = tonumber(val)
    if num then return num end
    -- Strip surrounding quotes
    if val:match("^[\"'].*[\"']$") then
      val = val:sub(2, -2)
    end
    return val
  end

  -- Process each line
  for raw_line in yaml_str:gmatch("[^\n]+") do
    -- Ignore comments & blank lines
    if raw_line:match("^%s*$") or raw_line:match("^%s*#") then
      goto continue
    end

    local indent = raw_line:match("^(%s*)") or ""
    indent = #indent
    local line = M.trim(raw_line)

    -- Calculate current parent based on indentation
    while indent < indent_stack[#indent_stack] do
      table.remove(stack)
      table.remove(indent_stack)
    end
    local parent = stack[#stack]

    -- List item? e.g. "- foo"
    local list_item = line:match("^%-[%s]*(.+)$")
    if list_item then
      if type(parent) ~= "table" then parent = {} end
      table.insert(parent, to_scalar(list_item))
      goto continue
    end

    -- Key / value pair (value may be empty – indicating nested map or list)
    local key, value = line:match("^([%w%-%_]+)%s*:%s*(.*)$")
    if key then
      if value == "" then
        -- Nested map – prepare new table and push on stack
        local tbl = {}
        parent[key] = tbl
        table.insert(stack, tbl)
        table.insert(indent_stack, indent + 1) -- anything > current indent
      elseif value == "[]" then
        parent[key] = {}
      elseif value:sub(1,1) == "[" and value:sub(-1) == "]" then
        parent[key] = M.map(M.split(value:sub(2,-2), ","), to_scalar)
      else
        parent[key] = to_scalar(value)
      end
    end

    ::continue::
  end

  return root
end

-- Table utilities

-- Deep copy a table
function M.deep_copy(orig)
  local copy
  if type(orig) == "table" then
    copy = {}
    for k, v in next, orig, nil do
      copy[M.deep_copy(k)] = M.deep_copy(v)
    end
    setmetatable(copy, M.deep_copy(getmetatable(orig)))
  else
    copy = orig
  end
  return copy
end

-- Merge tables (shallow)
function M.merge_tables(...)
  local result = {}
  for _, t in ipairs({...}) do
    if type(t) == "table" then
      for k, v in pairs(t) do
        result[k] = v
      end
    end
  end
  return result
end

-- Filter a list
function M.filter(list, predicate)
  local result = {}
  for _, item in ipairs(list) do
    if predicate(item) then
      table.insert(result, item)
    end
  end
  return result
end

-- Map over a list
function M.map(list, func)
  local result = {}
  for _, item in ipairs(list) do
    table.insert(result, func(item))
  end
  return result
end

-- Async utilities

-- Debounce a function
function M.debounce(func, delay)
  local timer = nil
  return function(...)
    local args = {...}
    if timer then
      timer:stop()
    end
    timer = vim.loop.new_timer()
    timer:start(delay, 0, vim.schedule_wrap(function()
      func(unpack(args))
      timer:close()
    end))
  end
end

-- Throttle a function
function M.throttle(func, delay)
  local last_call = 0
  local timer = nil
  
  return function(...)
    local now = vim.loop.now()
    local args = {...}
    
    if now - last_call >= delay then
      last_call = now
      func(unpack(args))
    elseif not timer then
      timer = vim.loop.new_timer()
      timer:start(delay - (now - last_call), 0, vim.schedule_wrap(function()
        last_call = vim.loop.now()
        func(unpack(args))
        timer:close()
        timer = nil
      end))
    end
  end
end

-- Run async job using plenary
function M.run_async(cmd, args, on_success, on_error)
  Job:new({
    command = cmd,
    args = args,
    on_exit = function(job, return_val)
      if return_val == 0 then
        if on_success then
          vim.schedule(function()
            on_success(job:result())
          end)
        end
      else
        if on_error then
          vim.schedule(function()
            on_error(job:stderr_result())
          end)
        end
      end
    end,
  }):start()
end

-- Validation utilities

-- Validate that required keys exist in table
function M.validate_keys(tbl, required_keys)
  for _, key in ipairs(required_keys) do
    if tbl[key] == nil then
      return false, string.format("Missing required key: %s", key)
    end
  end
  return true, nil
end

-- Validate file path
function M.validate_path(path, must_exist)
  if not path or path == "" then
    return false, "Path is empty"
  end
  
  if must_exist and not M.path_exists(path) then
    return false, string.format("Path does not exist: %s", path)
  end
  
  return true, nil
end

return M
