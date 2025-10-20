-- plugin/para-organize.lua
-- Plugin initialization for para-organize.nvim

-- Only load once
if vim.g.loaded_para_organize then
  return
end
vim.g.loaded_para_organize = true

-- Check Neovim version
if vim.fn.has("nvim-0.9.0") == 0 then
  vim.api.nvim_err_writeln("para-organize.nvim requires Neovim >= 0.9.0")
  return
end

-- Health check command
vim.api.nvim_create_user_command("ParaOrganizeHealth", function()
  vim.cmd("checkhealth para-organize")
end, { desc = "Check para-organize.nvim health" })

-- Note: Main setup and commands are created when user calls require("para-organize").setup()
-- This follows the lazy-loading best practice pattern
