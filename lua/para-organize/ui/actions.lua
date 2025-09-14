-- para-organize/ui/actions.lua
-- Navigation and UI actions for PARA organize UI

local M = {}

function M.open_item(ui_state, render_mod, indexer)
  vim.notify("DEBUG: open_item function called", vim.log.levels.DEBUG)
  local line = vim.api.nvim_win_get_cursor(ui_state.organize_popup.winid)[1]
  local content = vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, line - 1, line, false)[1]
  if not content then
    vim.notify("No content found at current line", vim.log.levels.WARN)
    return
  end
  vim.notify("Line content: " .. content, vim.log.levels.DEBUG)
  if content:match("^%[P%] ") or content:match("^%[A%] ") or content:match("^%[R%] ") or content:match("^%[D%] ") then
    local name = content:match("^%[.%] (.+)")
    vim.notify("Opening directory: " .. name, vim.log.levels.INFO)
    local dir = indexer.get_directory_by_name(name)
    if dir then
      if ui_state.current_directory then
        table.insert(ui_state.directory_stack, ui_state.current_directory)
      end
      ui_state.current_directory = dir
      vim.notify("Current directory set to: " .. dir.path, vim.log.levels.DEBUG)
      local sub_items = indexer.get_sub_items(dir.path)
      local content_tbl = {"# " .. name, "", "Press 'Enter' on a note to merge, 'Backspace' to go back", ""}
      render_mod.render_directories(ui_state, sub_items)
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content_tbl)
      vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
    end
  elseif content:match("^%[F%] ") then
    local name = content:match("^%[F%] (.+)")
    vim.notify("Selected file for merge: " .. name, vim.log.levels.INFO)
    local file = indexer.get_file_by_alias_or_name(name)
    if file then
      vim.notify("Starting merge with file: " .. file.name, vim.log.levels.INFO)
      ui_state.merge_mode = true
      ui_state.merge_target = file
      render_mod.render_merge_view(ui_state)
      local opts = { buffer = ui_state.organize_popup.bufnr, silent = true }
      vim.keymap.set("n", "<C-s>", function()
        local utils = require("para-organize.utils")
        local move = require("para-organize.move")
        local edited_content = table.concat(vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, 0, -1, false), "\n")
        if utils.write_file_atomic(file.path, edited_content) then
          vim.notify("Successfully merged content to " .. file.name, vim.log.levels.INFO)
          move.archive_capture(ui_state.current_capture)
          render_mod.render_suggestions(ui_state, ui_state.current_suggestions)
          ui_state.merge_mode = false
          ui_state.merge_target = nil
        end
      end, opts)
      vim.keymap.set("n", "<leader>mx", function()
        ui_state.merge_mode = false
        ui_state.merge_target = nil
        render_mod.render_suggestions(ui_state, ui_state.current_suggestions)
        vim.notify("Merge canceled", vim.log.levels.INFO)
      end, opts)
      vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
    end
  end
end

function M.back_to_parent(ui_state, render_mod, indexer)
  vim.notify("Going back from directory", vim.log.levels.DEBUG)
  if ui_state.directory_stack == nil then ui_state.directory_stack = {} end
  if #ui_state.directory_stack > 0 then
    ui_state.current_directory = table.remove(ui_state.directory_stack)
    if #ui_state.directory_stack == 0 and ui_state.current_directory == nil then
      vim.notify("Returning to root level", vim.log.levels.DEBUG)
      render_mod.render_suggestions(ui_state, ui_state.current_suggestions)
      return
    end
    if ui_state.current_directory then
      vim.notify("Going back to directory: " .. ui_state.current_directory.path, vim.log.levels.DEBUG)
      local sub_items = indexer.get_sub_items(ui_state.current_directory.path)
      local content_tbl = {"# " .. ui_state.current_directory.name, "", "Press 'Enter' on a note to merge, 'Backspace' to go back", ""}
      render_mod.render_directories(ui_state, sub_items)
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content_tbl)
      return
    end
  else
    ui_state.current_directory = nil
    render_mod.render_suggestions(ui_state, ui_state.current_suggestions)
  end
end

return M
