-- lua/para_org/ui.lua

-- This module will handle the two-pane user interface for organizing notes.

local Layout = require('nui.layout')
local Split = require('nui.split')
local Popup = require('nui.popup')
local Menu = require('nui.menu')
local config = require('para_org.config').options

local M = {}

-- To hold the state of the current UI components
local state = {
  layout = nil,
  active_note = nil,
}

-- Stops and cleans up the organization UI.
function M.stop()
  if state.layout and state.layout:is_mounted() then
    state.layout:unmount()
    state.layout = nil
    state.active_note = nil -- Clear the active note
    vim.notify('PARAOrganize session stopped.', vim.log.levels.INFO, { title = 'PARAOrganize' })
  end
end

-- Creates the main two-pane organization view for a given note.
function M.open_organization_view(note)
  if state.layout and state.layout:is_mounted() then
    M.stop()
  end

  state.active_note = note

  -- Left Pane: The editable capture note
  local capture_popup = Popup({
    enter = true,
    border = {
      style = config.ui.border_style,
      text = { top = ' Capture Note ', top_align = 'left' },
    },
    win_options = { winblend = 0 },
  })

  -- Right Pane: Suggestions Menu
  local suggestions = require('para_org.suggest').get_suggestions(note)
  local menu_items = {}
  for _, sug in ipairs(suggestions) do
    table.insert(menu_items, Menu.item(string.format('[%s] %s', sug.type, sug.display), { value = sug }))
  end

  local suggestion_menu = Menu({
    border = {
      style = config.ui.border_style,
      text = { top = ' Suggestions ', top_align = 'left' },
    },
    lines = menu_items,
    win_options = { winblend = 0 },
    line_padding = 1,
  }, {
    on_submit = function(item)
      local suggestion = item.value
      local move = require('para_org.move')
      if suggestion.path == 'archive' then
        if move.archive_note(state.active_note.path) then
          M.stop()
        end
      else
        if move.move_to_dest(state.active_note.path, suggestion.path) then
          M.stop()
        end
      end
    end,
  })

  -- Main Layout: A 50/50 vertical split
  state.layout = Layout({
    position = '50%',
    size = { width = '90%', height = '90%' },
  }, Split({
    dir = 'col',
    Split({ size = '50%' }, capture_popup),
    Split({ size = '50%', enter = true }, suggestion_menu),
  }))

  -- Mount the layout first
  state.layout:mount()

  -- Load content and set keymaps *after* mounting
  local note_content = require('plenary.path'):new(note.path):read()
  vim.api.nvim_buf_set_lines(capture_popup.bufnr, 0, -1, false, vim.split(note_content, '\n'))
  vim.api.nvim_buf_set_option(capture_popup.bufnr, 'filetype', 'markdown')
  vim.api.nvim_buf_set_option(capture_popup.bufnr, 'modifiable', true)

  vim.keymap.set('n', 'q', M.stop, { buffer = capture_popup.bufnr, nowait = true, silent = true, desc = 'Close Organizer' })
    vim.keymap.set('n', 'q', M.stop, { buffer = suggestion_menu.bufnr, nowait = true, silent = true, desc = 'Close Organizer' })
  vim.keymap.set('n', '/', function() require('para_org.search').find_para_folders() end, { buffer = suggestion_menu.bufnr, nowait = true, silent = true, desc = 'Find PARA Folder' })
end

-- The main entry point to start an organizing session.
function M.start(...)
  require('para_org.search').list_captures()
end

-- Returns the path of the note currently being organized.
-- Shows a list of notes within a selected folder in the right-hand pane.
function M.show_folder_contents(folder_path)
  local Path = require('plenary.path')
  local folder = Path:new(folder_path)
  local notes_in_folder = {}

  for path, type in folder:iter() do
    if type == 'file' and path.name:match('.md$') then
      table.insert(notes_in_folder, Menu.item(path.name, { value = path:absolute() }))
    end
  end

  if #notes_in_folder == 0 then
    vim.notify('No notes found in ' .. folder.name, vim.log.levels.INFO)
    return
  end

  local folder_menu = Menu({
    border = {
      style = config.ui.border_style,
      text = { top = ' Notes in ' .. folder.name .. ' ', top_align = 'left' },
    },
    lines = notes_in_folder,
    win_options = { winblend = 0 },
  }, {
    on_submit = function(item)
      -- TODO: Implement the merge flow - open this note in the right pane.
      vim.notify('Selected for merge: ' .. item.value)
      M.open_for_merge(item.value)
    end,
  })

  -- We need to replace the right-hand side of the split layout.
  state.layout.nodes[2]:update(folder_menu)
  state.layout:update()

  vim.keymap.set('n', 'q', M.stop, { buffer = folder_menu.bufnr, nowait = true, silent = true, desc = 'Close Organizer' })
end

-- Opens a target note in the right-hand pane for merging.
function M.open_for_merge(target_note_path)
  local target_content = require('plenary.path'):new(target_note_path):read()
  local merge_popup = Popup({
    enter = true,
    border = {
      style = config.ui.border_style,
      text = { top = ' Merge Target ', top_align = 'left' },
    },
    win_options = { winblend = 0 },
  })

  state.layout.nodes[2]:update(merge_popup)
  state.layout:update()

  vim.api.nvim_buf_set_lines(merge_popup.bufnr, 0, -1, false, vim.split(target_content, '\n'))
  vim.api.nvim_buf_set_option(merge_popup.bufnr, 'modifiable', true)
  vim.api.nvim_buf_set_option(merge_popup.bufnr, 'filetype', 'markdown')

  vim.keymap.set('n', 'q', M.stop, { buffer = merge_popup.bufnr, nowait = true, silent = true, desc = 'Close Organizer' })
  vim.notify('Manual merge: Edit left pane, then close window to archive original.', vim.log.levels.INFO)
end

function M.get_active_note_path()
  if state.active_note and state.active_note.path then
    return state.active_note.path
  end
  vim.notify('No active note in the organizer.', vim.log.levels.WARN)
  return nil
end

return M
