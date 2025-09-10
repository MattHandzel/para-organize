-- lua/para_org/init.lua

-- Main plugin module for para_org
-- This module is the entry point for all plugin functionality, dispatching
-- to other modules as needed.

local M = {}

local config = require('para_org.config')

-- A table to hold the implementation of subcommands.
-- We will lazy-load the modules to improve startup time.
local commands = {
  start = function(...) require('para_org.ui').start(...) end,
  stop = function(...) require('para_org.ui').stop(...) end,
  next = function(...) print('PARAOrganize next: Not yet implemented') end,
  prev = function(...) print('PARAOrganize prev: Not yet implemented') end,
  skip = function(...) print('PARAOrganize skip: Not yet implemented') end,
  reindex = function(...) require('para_org.indexer').reindex(...) end,
  move = function(dest) require('para_org.move').move_to_dest(require('para_org.ui').get_active_note_path(), dest) end,
  merge = function(target_note_path) require('para_org.ui').open_for_merge(target_note_path) end,
  archive = function(...) require('para_org.move').archive_note(require('para_org.ui').get_active_note_path()) end,
  help = function(...) print('PARAOrganize help: Not yet implemented') end,
}

-- Main execution function called by the user command.
-- It parses the arguments and calls the appropriate subcommand handler.
function M.execute(opts)
  local subcommand = opts.fargs[1]
  if not subcommand then
    -- Default action if no subcommand is given, e.g., start a session.
    subcommand = 'start'
  end

  local handler = commands[subcommand]
  if handler then
    -- Pass remaining arguments to the handler
    handler(unpack(opts.fargs, 2))
  else
    vim.notify('Unknown PARAOrganize subcommand: ' .. tostring(subcommand), vim.log.levels.ERROR)
  end
end

-- Function to provide command completions.
function M.get_completions(arglead, cmdline, cursorpos)
  -- Basic completion for subcommands
  local completions = {}
  for name, _ in pairs(commands) do
    if name:find('^' .. vim.pesc(arglead)) then
      table.insert(completions, name)
    end
  end
  return completions
end

-- Public setup function for user configuration.
function M.setup(user_config)
  config.setup(user_config)

  -- Expose <Plug> mappings
  vim.keymap.set('n', '<Plug>(para_organize_start)', function() M.execute({ fargs = { 'start' } }) end, { noremap = true, silent = true, desc = 'Start PARA organization' })
  vim.keymap.set('n', '<Plug>(para_organize_search)', function() require('para_org.search').find_para_folders() end, { noremap = true, silent = true, desc = 'Search PARA folders' })
  -- NOTE: accept, merge, archive are context-sensitive and handled in the UI
end

return M
