-- para-organize/ui/keymaps.lua
-- Keymap and autocmd setup for PARA organize UI

local M = {}

function M.setup_keymaps(ui_state, actions)
  local config = require("para-organize.config").get()
  local keymaps = config.keymaps.buffer

  -- Capture pane keymaps
  local capture_opts = { buffer = ui_state.capture_popup.bufnr, silent = true }
  vim.keymap.set("n", keymaps.accept, actions.accept_suggestion, capture_opts)
  vim.keymap.set("n", keymaps.cancel, actions.close, capture_opts)
  vim.keymap.set("n", keymaps.next, actions.next_capture, capture_opts)
  vim.keymap.set("n", keymaps.prev, actions.prev_capture, capture_opts)
  vim.keymap.set("n", keymaps.skip, actions.skip_capture, capture_opts)
  vim.keymap.set("n", keymaps.archive, actions.archive_capture, capture_opts)
  vim.keymap.set("n", keymaps.merge, actions.enter_merge_mode, capture_opts)
  vim.keymap.set("n", keymaps.search, actions.search_inline, capture_opts)
  vim.keymap.set("n", keymaps.help, actions.show_help, capture_opts)
  vim.keymap.set("n", "<A-j>", actions.next_suggestion, capture_opts)
  vim.keymap.set("n", "<A-k>", actions.prev_suggestion, capture_opts)
  vim.keymap.set("n", "<C-l>", function() vim.api.nvim_set_current_win(ui_state.organize_popup.winid) end, capture_opts)

  -- Organize pane keymaps
  local organize_opts = { buffer = ui_state.organize_popup.bufnr, silent = true }
  vim.keymap.set("n", keymaps.accept, actions.accept_suggestion, organize_opts)
  vim.keymap.set("n", keymaps.cancel, actions.close, organize_opts)
  vim.keymap.set("n", "<A-j>", actions.next_suggestion, organize_opts)
  vim.keymap.set("n", "<A-k>", actions.prev_suggestion, organize_opts)
  vim.keymap.set("n", "s", actions.change_sort_order, organize_opts)
  vim.keymap.set("n", "/", actions.search_inline, organize_opts)
  vim.keymap.set("n", "<CR>", actions.open_item, organize_opts)
  vim.keymap.set("n", "<BS>", actions.back_to_parent, organize_opts)
  vim.keymap.set("n", "<C-h>", function() vim.api.nvim_set_current_win(ui_state.capture_popup.winid) end, organize_opts)
  vim.keymap.set("n", keymaps.new_project, function() actions.create_new_folder("projects") end, capture_opts)
  vim.keymap.set("n", keymaps.new_area, function() actions.create_new_folder("areas") end, capture_opts)
  vim.keymap.set("n", keymaps.new_resource, function() actions.create_new_folder("resources") end, capture_opts)
end

function M.setup_autocmds(ui_state, close_fn)
  local group = vim.api.nvim_create_augroup("ParaOrganizeUI", { clear = true })
  vim.api.nvim_create_autocmd("WinClosed", {
    group = group,
    pattern = tostring(ui_state.capture_popup.winid) .. "," .. tostring(ui_state.organize_popup.winid),
    callback = close_fn,
  })
end

return M
