-- para-organize/utils/yaml.lua
-- YAML parsing utilities for para-organize.nvim

local M = {}

-- Very simple YAML parser for frontmatter (not full YAML)
function M.parse_yaml_simple(yaml_str)
  local result = {}
  for line in yaml_str:gmatch("[^
]+") do
    local k, v = line:match("^([%w_]+):%s*(.*)$")
    if k then
      if v:find(",") then
        local items = {}
        for item in v:gmatch("[^,]+") do
          table.insert(items, vim.trim(item))
        end
        result[k] = items
      else
        result[k] = vim.trim(v)
      end
    end
  end
  return result
end

return M
