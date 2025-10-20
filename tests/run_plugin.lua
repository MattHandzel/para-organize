-- tests/run_plugin.lua
-- This script is executed by Neovim in headless mode to test the plugin.

-- Set up a safe environment to run the test
local success, err = pcall(function()
  -- It's important to load plenary first as other modules depend on it
  require('plenary')
  
  -- Now, require the main plugin file
  local para_organize = require('para-organize')
  
  -- Start the plugin
  para_organize.start()
  
  -- If the plugin has a UI, we might need to simulate some interactions
  -- For now, we'll just check if it starts without errors.
  
  -- Close Neovim after the test
  vim.cmd('q!')
end)

-- If there was an error, print it and exit with a non-zero code
if not success then
  print('Error running plugin test: ' .. err)
  vim.cmd('cq')
end
