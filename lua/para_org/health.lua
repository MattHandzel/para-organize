-- lua/para_org/health.lua

-- This module provides health checks for the plugin.

local config = require('para_org.config').options

local M = {}

function M.check()
  vim.health.start('para-organize.nvim')

  -- Check for Plenary
  local plenary_ok, _ = pcall(require, 'plenary.path')
  if plenary_ok then
    vim.health.ok('plenary.nvim is installed.')
  else
    vim.health.error('plenary.nvim is not installed. This is a required dependency.')
  end

  -- Check for Telescope
  local telescope_ok, _ = pcall(require, 'telescope')
  if telescope_ok then
    vim.health.ok('telescope.nvim is installed.')
  else
    vim.health.error('telescope.nvim is not installed. This is a required dependency.')
  end

  -- Check for Nui
  local nui_ok, _ = pcall(require, 'nui.layout')
  if nui_ok then
    vim.health.ok('nui.nvim is installed.')
  else
    vim.health.error('nui.nvim is not installed. This is a required dependency.')
  end

  -- Check if root_dir exists
  local root = require('plenary.path'):new(config.root_dir)
  if root:exists() and root:is_dir() then
    vim.health.ok('Configured root_dir exists: ' .. config.root_dir)
  else
    vim.health.error('Configured root_dir does not exist or is not a directory: ' .. config.root_dir)
  end
end

return M
