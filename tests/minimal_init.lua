-- tests/minimal_init.lua
-- A minimal init.lua for running tests.

-- Set up the package path to include our dependencies
local deps_path = vim.fn.expand('<sfile>:p:h') .. '/site/pack/deps/start'
vim.opt.packpath:prepend(deps_path)

-- Add the plugin's root directory to the runtime path
vim.opt.runtimepath:prepend(vim.fn.expand('<sfile>:p:h:h'))

-- Load the plugin
require('para_org').setup()
