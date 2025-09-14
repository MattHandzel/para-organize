-- minimal_init.lua
-- Minimal Neovim configuration for testing para-organize.nvim

-- Ensure tests can find the plugin and dependencies
local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':h')
local plugin_root = vim.fn.fnamemodify(test_dir, ':h')

-- Add plugin root to rtp
vim.opt.rtp:prepend(plugin_root)

-- Add tests directory to package path so we can require test helpers
package.path = package.path .. ';' .. test_dir .. '/?.lua'

-- Ensure plenary is in path
local status_ok, _ = pcall(require, "plenary")
if not status_ok then
  -- Try to find plenary in common locations
  local plenary_paths = {
    vim.fn.stdpath("data") .. "/lazy/plenary.nvim",
    vim.fn.stdpath("data") .. "/site/pack/packer/start/plenary.nvim",
    plugin_root .. "/deps/plenary.nvim",
    plugin_root .. "/../plenary.nvim",
  }
  
  for _, path in ipairs(plenary_paths) do
    if vim.fn.isdirectory(path) == 1 then
      vim.opt.rtp:prepend(path)
      break
    end
  end
end

-- Ensure telescope is in path
local telescope_ok, _ = pcall(require, "telescope")
if not telescope_ok then
  local telescope_paths = {
    vim.fn.stdpath("data") .. "/lazy/telescope.nvim",
    vim.fn.stdpath("data") .. "/site/pack/packer/start/telescope.nvim",
    plugin_root .. "/deps/telescope.nvim",
    plugin_root .. "/../telescope.nvim",
  }
  
  for _, path in ipairs(telescope_paths) do
    if vim.fn.isdirectory(path) == 1 then
      vim.opt.rtp:prepend(path)
      break
    end
  end
end

-- Ensure nui is in path
local nui_ok, _ = pcall(require, "nui")
if not nui_ok then
  local nui_paths = {
    vim.fn.stdpath("data") .. "/lazy/nui.nvim",
    vim.fn.stdpath("data") .. "/site/pack/packer/start/nui.nvim",
    plugin_root .. "/deps/nui.nvim",
    plugin_root .. "/../nui.nvim",
  }
  
  for _, path in ipairs(nui_paths) do
    if vim.fn.isdirectory(path) == 1 then
      vim.opt.rtp:prepend(path)
      break
    end
  end
end

-- Set up test environment variables
vim.env.PARA_ORGANIZE_TEST_MODE = "1"
vim.env.PARA_ORGANIZE_TEST_DIR = test_dir

-- Create test data directories if they don't exist
local test_data_dir = test_dir .. "/output"
if vim.fn.isdirectory(test_data_dir) == 0 then
  vim.fn.mkdir(test_data_dir, "p")
end

-- Define test vault directory to use the real notes
local test_vault_dir = test_dir .. "/test_vault/notes"

-- Ensure the test vault exists
if vim.fn.isdirectory(test_vault_dir) == 0 then
  error("Test vault not found at: " .. test_vault_dir .. ". Please run 'make setup-tests' or copy notes manually.")
end

-- Basic Neovim configuration for tests
vim.o.swapfile = false
vim.o.backup = false
vim.o.writebackup = false
vim.o.undofile = false
vim.o.termguicolors = true

-- Setup plugin with test configuration
local para = require("para-organize")
para.setup({
  paths = {
    vault_dir = test_vault_dir,
    capture_folder = "capture/raw_capture",
  },
  debug = {
    enabled = true,
    log_level = "debug",
    log_file = test_data_dir .. "/para-organize-test.log",
  },
})

-- Preload plenary test utilities
require("plenary.busted")

-- Echo test environment is ready
print("Test environment initialized")
