-- lua/para_org/search.lua

-- This module integrates with telescope.nvim to provide custom pickers
-- for searching and selecting notes.

local pickers = require('telescope.pickers')
local finders = require('telescope.finders')
local conf = require('telescope.config').values
local actions = require('telescope.actions')
local action_state = require('telescope.actions.state')

local indexer = require('para_org.indexer')
local config = require('para_org.config').options

local M = {}

-- A custom Telescope picker to list captured notes from the index.
function M.list_captures(opts)
  opts = opts or {}

  local notes = indexer.load_index()
  if not notes or #notes == 0 then
    vim.notify('No notes found in index. Run :PARAOrganize reindex', vim.log.levels.WARN, { title = 'PARAOrganize' })
    return
  end

  pickers.new(opts, {
    prompt_title = 'PARA Captures',
    finder = finders.new_table {
      results = notes,
      entry_maker = function(entry)
        -- entry is one of the tables from our index array
        return {
          value = entry, -- The full note object
          display = entry.path:gsub(config.root_dir .. '/', ''), -- Show relative path
          ordinal = entry.path, -- For sorting
          path = entry.path,
        }
      end,
    },
    sorter = conf.generic_sorter(opts),
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection then
                    -- For single selection, start a session with just that one note
          require('para_org.ui').start_session({ selection })
        end
      end)
      return true
    end,
  }):find()
end


-- A custom Telescope picker to list PARA folders (Projects, Areas, Resources).
function M.find_para_folders(opts)
  opts = opts or {}

  local destinations = require('para_org.suggest').get_all_destinations()

  pickers.new(opts, {
    prompt_title = 'Find PARA Folder',
    finder = finders.new_table {
      results = destinations,
      entry_maker = function(entry)
        return {
          value = entry.path, -- The full path to the folder
          display = string.format('[%s] %s', entry.type:sub(1,1), Path:new(entry.path):shorten(40)),
          ordinal = entry.path,
        }
      end,
    },
    sorter = conf.generic_sorter(opts),
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection then
          -- Pass the selected folder path to the UI to display its contents
          require('para_org.ui').show_folder_contents(selection.value)
        end
      end)
      return true
    end,
  }):find()
end

return M
