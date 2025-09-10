-- lua/para_org/indexer.lua

-- This module is responsible for scanning the PARA folder structure,
-- parsing note frontmatter, and maintaining a searchable index.

local Path = require('plenary.path')
local async = require('plenary.async')
local config = require('para_org.config').options
local utils = require('para_org.utils')

local M = {}

-- Gets the path for the index file.
local function get_index_file_path()
  return Path:new(vim.fn.stdpath('data'), 'para_org_index.json')
end

-- Asynchronously builds the index by scanning all notes.
function M.reindex()
  async.run(function()
    vim.notify('Starting PARA reindex...', vim.log.levels.INFO, { title = 'PARA-Organize' })

    local root = Path:new(config.root_dir)
    if not root:exists() or not root:is_dir() then
      vim.notify('Root directory not found: ' .. config.root_dir, vim.log.levels.ERROR)
      return
    end

    local index = {}
    local scanner = require('plenary.scandir').scan_dir(root:absolute(), {
      hidden = true,
      respect_gitignore = true,
      glob = '**/' .. config.patterns.file_glob,
    })

    for _, file_path in ipairs(scanner) do
      local path = Path:new(file_path)
      local content, err = path:read()
      if content and not err then
        local frontmatter, _ = utils.parse_frontmatter(content)
        table.insert(index, {
          path = path:absolute(),
          frontmatter = frontmatter,
          -- TODO: Add more indexed fields as per spec (tags, sources, etc.)
        })
      end
    end

    local index_file = get_index_file_path()
    index_file:write(vim.fn.json_encode(index), 'w')

    vim.notify('PARA reindex complete. Found ' .. #index .. ' notes.', vim.log.levels.INFO, { title = 'PARA-Organize' })
  end)
end

-- Loads the index from the file.
function M.load_index()
  local index_file = get_index_file_path()
  if not index_file:exists() then
    return {}
  end
  local content, err = index_file:read()
  if err or not content then
    return {}
  end
  return vim.fn.json_decode(content)
end

return M
