-- para-organize/utils/frontmatter.lua
-- Frontmatter extraction utilities for para-organize.nvim

local M = {}

function M.extract_frontmatter(content, delimiters)
  delimiters = delimiters or { "---", "---" }
  local start_delim = delimiters[1]
  local end_delim = delimiters[2] or start_delim
  local start_pat = "^%s*" .. vim.pesc(start_delim) .. "%s*\n"
  local end_pat = "\n%s*" .. vim.pesc(end_delim) .. "%s*\n"
  local start_idx, _ = content:find(start_pat)
  if not start_idx then return nil, content end
  local frontmatter_start = select(2, content:find(start_pat)) or 0
  local end_idx = content:find(end_pat, frontmatter_start + 1)
  if not end_idx then return nil, content end
  local frontmatter = content:sub(frontmatter_start + 1, end_idx - 1)
  local body = content:sub(end_idx + #end_delim + 2)
  return frontmatter, body
end

return M
