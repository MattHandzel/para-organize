-- para-organize/utils.lua
-- Utility functions for para-organize.nvim

local M = {}

local log_mod = require("para-organize.utils.log")
local path_mod = require("para-organize.utils.path")
local string_mod = require("para-organize.utils.string")

-- Logging
M.init_logging = log_mod.init_logging
M.log = log_mod.log

-- Path utilities
M.normalize_path = path_mod.normalize_path
M.path_exists = path_mod.path_exists
M.is_directory = path_mod.is_directory
M.relative_path = path_mod.relative_path
M.glob_files = path_mod.glob_files

-- String utilities
M.trim = string_mod.trim
M.split = string_mod.split
M.normalize_tag = string_mod.normalize_tag
M.string_similarity = string_mod.string_similarity

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

-- [[
-- YAML parsing helpers
-- We try to use a real YAML parser (lyaml) if present for maximum correctness.
-- If lyaml is unavailable, we fall back to a reasonably complete Lua parser that
-- supports the YAML subset typically found in note front-matter.
-- ]]

-- public entry point
function M.parse_yaml(yaml_str)
  if not yaml_str or yaml_str == "" then return {} end

  -- First, attempt to use lyaml (if the user has it installed via luarocks).
  local ok, lyaml = pcall(require, "lyaml")
  if ok and lyaml and type(lyaml.load) == "function" then
    local success, result = pcall(function() return lyaml.load(yaml_str) end)
    if success and type(result) == "table" then
      return result
    end
  end

  -- Fall back to internal parser
  return M.parse_yaml_fallback(yaml_str)
end

-- Internal YAML subset parser (previously named parse_yaml_simple)
function M.parse_yaml_fallback(yaml_str)
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
    -- allow negative / decimal numbers
    local num = tonumber(val)
    if num then return num end
    -- Strip surrounding quotes
    if val:match("^[\"'].*[\"']$") then
      val = val:sub(2, -2)
    end
    return val
  end

  -- YAML constants
  local INDENT_STEP = 2

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
        -- Could be a nested map OR a list; create child table and push. Assume list/child items indented by INDENT_STEP.
        local tbl = {}
        parent[key] = tbl
        table.insert(stack, tbl)
        table.insert(indent_stack, indent + INDENT_STEP)
      elseif value == "[]" then
        parent[key] = {}
      elseif value:sub(1,1) == "[" and value:sub(-1) == "]" then
        -- Inline list e.g. [a, b, c]
        parent[key] = M.map(M.split(value:sub(2,-2), ","), to_scalar)
      elseif value:sub(1,1) == "{" and value:sub(-1) == "}" then
        -- Inline map e.g. {city: SF, country: US}
        local map_tbl = {}
        local inner = value:sub(2,-2)
        for pair in inner:gmatch("[^,]+") do
          local pk, pv = pair:match("^%s*([%w%-%_]+)%s*:%s*(.-)%s*$")
          if pk then
            map_tbl[pk] = to_scalar(pv)
          end
        end
        parent[key] = map_tbl
      else
        parent[key] = to_scalar(value)
      end
    end

    ::continue::
  end

  return root
end

-- Backwards-compatibility: keep old function name
M.parse_yaml_simple = M.parse_yaml

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
