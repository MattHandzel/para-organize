-- plugin/para_org.lua

-- This file is automatically loaded by Neovim and is the place to set up
-- commands, autocommands, and keymaps.

local function get_command_completion(arglead, cmdline, cursorpos)
  local para_org = require('para_org')
  if para_org and para_org.get_completions then
    return para_org.get_completions(arglead, cmdline, cursorpos)
  end
  return {}
end

vim.api.nvim_create_user_command(
  'PARAOrganize',
  function(opts) 
    -- The main logic will be handled in the init.lua module to support lazy loading.
    -- We pass the full opts table to the handler.
    require('para_org').execute(opts)
  end,
  {
    nargs = '*', -- Allow any number of arguments
    complete = get_command_completion, -- Custom completion function
    desc = 'Organize PARA notes. Use <Tab> for subcommands.',
  }
)
