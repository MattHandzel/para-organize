-- para-organize/indexer/scan.lua
-- Directory scanning and reindexing for PARA organize indexer

local utils = require("para-organize.utils")
local scan = require("plenary.scandir")
local M = {}

function M.scan_directory(dir_path, ignore_patterns, index_file, index_file_callback)
  utils.log("INFO", "Scanning directory: %s", dir_path)
  scan.scan_dir_async(dir_path, {
    respect_gitignore = true,
    search_pattern = "*.md",
    silent = true,
    on_exit = function(files)
      local count = 0
      for _, filepath in ipairs(files) do
        local should_ignore = false
        for _, pattern in ipairs(ignore_patterns or {}) do
          if filepath:match(pattern) then
            should_ignore = true
            break
          end
        end
        if not should_ignore and index_file_callback then
          if index_file_callback(filepath) then
            count = count + 1
          end
        end
      end
      utils.log("INFO", "Indexed %d files from %s", count, dir_path)
    end,
  })
end

return M
