-- tests/helpers.lua
local M = {}

local config = require("para-organize.config")

-- Creates a temporary file for testing purposes
-- opts: { content: string, name: string, dir: string }
function M.create_temp_file(opts)
  opts = opts or {}
  local content = opts.content or [[
---
timestamp: 2024-01-01T00:00:00Z
tags: [temp, helper]
---
# Temporary Test File
]]
  local dir = opts.dir or (config.get().paths.vault_dir .. "/capture/raw_capture")
  local name = opts.name or ("temp_test_" .. os.time() .. "_" .. math.random(1000) .. ".md")

  -- Ensure directory exists
  vim.fn.mkdir(dir, "p")

  local path = dir .. "/" .. name
  local file, err = io.open(path, "w")
  if not file then
    return nil, err
  end

  file:write(content)
  file:close()
  return path
end

-- Cleans up files in the test output directory
function M.clean_test_output()
  local test_output_dir = vim.env.PARA_ORGANIZE_TEST_DIR .. "/output"
  if vim.fn.isdirectory(test_output_dir) == 1 then
    os.execute("rm -rf " .. test_output_dir .. "/*")
  end
end

return M