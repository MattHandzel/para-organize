-- para-organize/utils/string.lua
-- String utilities for para-organize.nvim

local M = {}

function M.trim(s)
  return s:match("^%s*(.-)%s*$")
end

function M.split(str, delimiter)
  delimiter = delimiter or ","
  local result = {}
  local pattern = string.format("([^%s]+)", delimiter)
  for match in string.gmatch(str, pattern) do
    table.insert(result, M.trim(match))
  end
  return result
end

function M.normalize_tag(tag)
  tag = tag:lower()
  tag = tag:gsub("%s+", "-")
  tag = tag:gsub("[^%w%-]", "-")
  tag = tag:gsub("%-+", "-")
  tag = tag:gsub("^%-+", "")
  tag = tag:gsub("%-+$", "")
  return tag
end

function M.string_similarity(s1, s2)
  local len1, len2 = #s1, #s2
  local matrix = {}
  for i = 0, len1 do matrix[i] = { [0] = i } end
  for j = 0, len2 do matrix[0][j] = j end
  for i = 1, len1 do
    for j = 1, len2 do
      local cost = s1:sub(i, i) == s2:sub(j, j) and 0 or 1
      matrix[i][j] = math.min(
        matrix[i-1][j] + 1,
        matrix[i][j-1] + 1,
        matrix[i-1][j-1] + cost
      )
    end
  end
  return matrix[len1][len2]
end

return M
