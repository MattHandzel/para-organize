-- para-organize/ui.lua
-- Two-pane UI for organizing notes

local M = {}

-- Dependencies
local Layout = require("nui.layout")
local Popup = require("nui.popup")
local Menu = require("nui.menu")
local Input = require("nui.input")
local Line = require("nui.line")
local Text = require("nui.text")

-- Module state
local ui_state = {
  layout = nil,
  capture_popup = nil,
  organize_popup = nil,
  suggestions_menu = nil,
  current_capture = nil,
  current_suggestions = nil,
  selected_suggestion_index = 1,
  merge_mode = false,
  merge_target = nil,
  session = nil,
}

-- Create the two-pane layout
function M.create_layout()
  local config = require("para-organize.config").get()
  local ui_config = config.ui
  
  -- Create capture pane (left)
  ui_state.capture_popup = Popup({
    enter = false,
    focusable = true,
    border = {
      style = ui_config.float_opts.border,
      text = {
        top = " Capture ",
        top_align = "center",
      },
    },
    win_options = {
      winblend = 0,
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
    buf_options = {
      modifiable = true,
      swapfile = false,
      filetype = "markdown",
    },
  })
  
  -- Create organize pane (right)
  ui_state.organize_popup = Popup({
    enter = false,
    focusable = true,
    border = {
      style = ui_config.float_opts.border,
      text = {
        top = " Organize ",
        top_align = "center",
      },
    },
    win_options = {
      winblend = 0,
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
  })
  
  -- Create layout based on config
  if ui_config.layout == "float" then
    -- Floating layout
    local width = math.floor(vim.o.columns * ui_config.float_opts.width)
    local height = math.floor(vim.o.lines * ui_config.float_opts.height)
    
    ui_state.layout = Layout(
      {
        position = ui_config.float_opts.position or "50%",
        size = {
          width = width,
          height = height,
        },
      },
      Layout.Box({
        Layout.Box(ui_state.capture_popup, { size = "50%" }),
        Layout.Box(ui_state.organize_popup, { size = "50%" }),
      }, { dir = "row" })
    )
  else
    -- Split layout
    ui_state.layout = Layout(
      {
        position = "0%",
        size = "100%",
      },
      Layout.Box({
        Layout.Box(ui_state.capture_popup, { size = "50%" }),
        Layout.Box(ui_state.organize_popup, { size = "50%" }),
      }, { dir = ui_config.split_opts.direction == "vertical" and "row" or "col" })
    )
  end
end

-- Open the UI
function M.open(session)
  local utils = require("para-organize.utils")
  
  ui_state.session = session
  
  -- Create layout if not exists
  if not ui_state.layout then
    M.create_layout()
  end
  
  -- Mount and show layout
  ui_state.layout:mount()
  
  -- Setup keymaps
  M.setup_keymaps()
  
  -- Setup autocmds
  M.setup_autocmds()
  
  utils.log("DEBUG", "UI opened")
end

-- Close the UI
function M.close()
  local utils = require("para-organize.utils")
  
  if ui_state.layout then
    ui_state.layout:unmount()
  end
  
  -- Reset state
  ui_state.current_capture = nil
  ui_state.current_suggestions = nil
  ui_state.selected_suggestion_index = 1
  ui_state.merge_mode = false
  ui_state.merge_target = nil
  
  utils.log("DEBUG", "UI closed")
end

-- Load a capture into the UI
function M.load_capture(capture, suggestions)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  
  ui_state.current_capture = capture
  ui_state.current_suggestions = suggestions
  ui_state.selected_suggestion_index = 1
  
  -- Load capture content into left pane
  M.render_capture(capture)
  
  -- Load suggestions into right pane
  M.render_suggestions(suggestions)
  
  -- Focus capture pane
  vim.api.nvim_set_current_win(ui_state.capture_popup.winid)
end

-- Render capture in left pane
function M.render_capture(capture)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  local ui_config = config.ui
  
  -- Read capture file content
  local content = utils.read_file(capture.path)
  if not content then
    vim.api.nvim_buf_set_lines(ui_state.capture_popup.bufnr, 0, -1, false, {
      "Error: Could not read capture file"
    })
    return
  end
  
  -- Parse frontmatter
  local frontmatter_str, body = utils.extract_frontmatter(content,
    config.patterns.frontmatter_delimiters)
  
  local lines = {}
  
  -- Add metadata header
  table.insert(lines, "")
  
  -- Show timestamp
  if ui_config.display.show_timestamps and capture.timestamp then
    local formatted_time = utils.format_datetime(capture.timestamp,
      ui_config.display.timestamp_format)
    table.insert(lines, "📅 " .. formatted_time)
  end
  
  -- Show aliases (excluding capture_id)
  if capture.aliases and #capture.aliases > 0 then
    local display_aliases = {}
    for _, alias in ipairs(capture.aliases) do
      if not (ui_config.display.hide_capture_id and alias:match("^capture_")) then
        table.insert(display_aliases, alias)
      end
    end
    
    if #display_aliases > 0 then
      table.insert(lines, "📝 " .. table.concat(display_aliases, ", "))
    end
  end
  
  -- Show tags
  if capture.tags and #capture.tags > 0 then
    local tag_str = ""
    for _, tag in ipairs(capture.tags) do
      tag_str = tag_str .. " #" .. tag
    end
    table.insert(lines, "🏷️" .. tag_str)
  end
  
  -- Show sources
  if capture.sources and #capture.sources > 0 and 
     not ui_config.display.hide_location then
    table.insert(lines, "📍 " .. table.concat(capture.sources, ", "))
  end
  
  -- Add separator
  table.insert(lines, "")
  table.insert(lines, "---")
  table.insert(lines, "")
  
  -- Add body content
  for line in body:gmatch("[^\n]*") do
    table.insert(lines, line)
  end
  
  -- Set buffer content
  vim.api.nvim_buf_set_lines(ui_state.capture_popup.bufnr, 0, -1, false, lines)
  
  -- Set buffer as modifiable
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "readonly", false)
end

-- Render suggestions in right pane
function M.render_suggestions(suggestions)
  local config = require("para-organize.config").get()
  local ui_config = config.ui
  
  if ui_state.merge_mode then
    M.render_merge_view()
    return
  end
  
  local lines = {}
  
  -- Header
  table.insert(lines, " Suggested Destinations:")
  table.insert(lines, "")
  
  -- Render each suggestion
  for i, suggestion in ipairs(suggestions) do
    local line = ""
    
    -- Selection indicator
    if i == ui_state.selected_suggestion_index then
      line = "▶ "
    else
      line = "  "
    end
    
    -- Type icon
    if ui_config.icons.enabled then
      local icon = ""
      if suggestion.type == "projects" then
        icon = ui_config.icons.project
      elseif suggestion.type == "areas" then
        icon = ui_config.icons.area
      elseif suggestion.type == "resources" then
        icon = ui_config.icons.resource
      elseif suggestion.type == "archives" then
        icon = ui_config.icons.archive
      end
      line = line .. icon .. " "
    end
    
    -- Type letter
    local type_letter = suggestion.type:sub(1, 1):upper()
    line = line .. "[" .. type_letter .. "] "
    
    -- Folder name
    line = line .. suggestion.name
    
    -- Score (if enabled)
    if ui_config.display.show_scores then
      local score_str = string.format(" (%.2f)", suggestion.score)
      line = line .. score_str
    end
    
    table.insert(lines, line)
    
    -- Reasons (indented)
    if suggestion.reasons and #suggestion.reasons > 0 then
      for _, reason in ipairs(suggestion.reasons) do
        table.insert(lines, "      • " .. reason)
      end
    end
    
    table.insert(lines, "")
  end
  
  -- Add help text
  table.insert(lines, "")
  table.insert(lines, "─────────────────────────")
  table.insert(lines, " Commands:")
  table.insert(lines, "  <CR>    Accept suggestion")
  table.insert(lines, "  /       Search for folder")
  table.insert(lines, "  m       Merge mode")
  table.insert(lines, "  a       Archive now")
  table.insert(lines, "  s       Skip")
  table.insert(lines, "  <Tab>   Next capture")
  table.insert(lines, "  ?       Help")
  
  -- Set buffer content
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, lines)
  
  -- Highlight selected line
  M.highlight_selection()
end

-- Render merge view
function M.render_merge_view()
  if not ui_state.merge_target then
    return
  end
  
  local utils = require("para-organize.utils")
  
  -- Read target file content
  local content = utils.read_file(ui_state.merge_target.path)
  if not content then
    vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, {
      "Error: Could not read target file"
    })
    return
  end
  
  -- Show target file content
  local lines = {}
  table.insert(lines, " Merge Target: " .. ui_state.merge_target.title)
  table.insert(lines, " Path: " .. ui_state.merge_target.path)
  table.insert(lines, "")
  table.insert(lines, "─────────────────────────")
  table.insert(lines, "")
  
  for line in content:gmatch("[^\n]*") do
    table.insert(lines, line)
  end
  
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, lines)
  
  -- Make buffer modifiable for editing
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
end

-- Highlight selected suggestion
function M.highlight_selection()
  local ns_id = vim.api.nvim_create_namespace("para_organize_selection")
  
  -- Clear existing highlights
  vim.api.nvim_buf_clear_namespace(ui_state.organize_popup.bufnr, ns_id, 0, -1)
  
  -- Calculate line number (accounting for header)
  local line_num = 2 -- Header offset
  for i = 1, ui_state.selected_suggestion_index - 1 do
    line_num = line_num + 1 -- Suggestion line
    if ui_state.current_suggestions[i].reasons then
      line_num = line_num + #ui_state.current_suggestions[i].reasons
    end
    line_num = line_num + 1 -- Empty line
  end
  
  -- Apply highlight
  vim.api.nvim_buf_add_highlight(
    ui_state.organize_popup.bufnr,
    ns_id,
    "Visual",
    line_num,
    0,
    -1
  )
end

-- Setup keymaps
function M.setup_keymaps()
  local config = require("para-organize.config").get()
  local keymaps = config.keymaps.buffer
  
  -- Capture pane keymaps
  local capture_opts = { buffer = ui_state.capture_popup.bufnr, silent = true }
  
  vim.keymap.set('n', keymaps.accept, M.accept_suggestion, capture_opts)
  vim.keymap.set('n', keymaps.cancel, M.close, capture_opts)
  vim.keymap.set('n', keymaps.next, M.next_capture, capture_opts)
  vim.keymap.set('n', keymaps.prev, M.prev_capture, capture_opts)
  vim.keymap.set('n', keymaps.skip, M.skip_capture, capture_opts)
  vim.keymap.set('n', keymaps.archive, M.archive_capture, capture_opts)
  vim.keymap.set('n', keymaps.merge, M.enter_merge_mode, capture_opts)
  vim.keymap.set('n', keymaps.search, M.search_folders, capture_opts)
  vim.keymap.set('n', keymaps.help, M.show_help, capture_opts)
  vim.keymap.set('n', 'j', M.next_suggestion, capture_opts)
  vim.keymap.set('n', 'k', M.prev_suggestion, capture_opts)
  
  -- Organize pane keymaps  
  local organize_opts = { buffer = ui_state.organize_popup.bufnr, silent = true }
  
  vim.keymap.set('n', keymaps.accept, M.accept_suggestion, organize_opts)
  vim.keymap.set('n', keymaps.cancel, M.close, organize_opts)
  vim.keymap.set('n', 'j', M.next_suggestion, organize_opts)
  vim.keymap.set('n', 'k', M.prev_suggestion, organize_opts)
  
  -- Quick folder creation
  vim.keymap.set('n', keymaps.new_project, function()
    M.create_new_folder("projects")
  end, capture_opts)
  
  vim.keymap.set('n', keymaps.new_area, function()
    M.create_new_folder("areas")
  end, capture_opts)
  
  vim.keymap.set('n', keymaps.new_resource, function()
    M.create_new_folder("resources")
  end, capture_opts)
end

-- Setup autocmds
function M.setup_autocmds()
  local group = vim.api.nvim_create_augroup("ParaOrganizeUI", { clear = true })
  
  -- Clean up on window close
  vim.api.nvim_create_autocmd("WinClosed", {
    group = group,
    pattern = tostring(ui_state.capture_popup.winid) .. "," .. 
              tostring(ui_state.organize_popup.winid),
    callback = function()
      M.close()
    end,
  })
end

-- Navigation functions
function M.next_suggestion()
  if ui_state.current_suggestions and 
     ui_state.selected_suggestion_index < #ui_state.current_suggestions then
    ui_state.selected_suggestion_index = ui_state.selected_suggestion_index + 1
    M.render_suggestions(ui_state.current_suggestions)
  end
end

function M.prev_suggestion()
  if ui_state.selected_suggestion_index > 1 then
    ui_state.selected_suggestion_index = ui_state.selected_suggestion_index - 1
    M.render_suggestions(ui_state.current_suggestions)
  end
end

-- Action functions
function M.accept_suggestion()
  if not ui_state.current_suggestions or 
     not ui_state.current_suggestions[ui_state.selected_suggestion_index] then
    return
  end
  
  local suggestion = ui_state.current_suggestions[ui_state.selected_suggestion_index]
  local para = require("para-organize")
  
  para.move(suggestion.path)
end

function M.next_capture()
  local para = require("para-organize")
  para.next()
end

function M.prev_capture()
  local para = require("para-organize")
  para.prev()
end

function M.skip_capture()
  local para = require("para-organize")
  para.skip()
end

function M.archive_capture()
  local para = require("para-organize")
  para.archive()
end

function M.enter_merge_mode()
  local search = require("para-organize.search")
  
  ui_state.merge_mode = true
  
  -- Open folder picker to select destination
  search.open_folder_picker(function(folder)
    -- Open notes picker for selected folder
    search.open_folder_notes_picker(folder.path, function(note)
      ui_state.merge_target = note
      M.render_merge_view()
    end)
  end)
end

function M.search_folders()
  local search = require("para-organize.search")
  
  search.open_folder_picker(function(folder)
    local para = require("para-organize")
    para.move(folder.path)
  end)
end

function M.create_new_folder(folder_type)
  local Input = require("nui.input")
  local para = require("para-organize")
  
  local input = Input({
    position = "50%",
    size = {
      width = 40,
    },
    border = {
      style = "rounded",
      text = {
        top = " New " .. folder_type:sub(1, -2) .. " Name ",
        top_align = "center",
      },
    },
    win_options = {
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
  }, {
    prompt = "> ",
    default_value = "",
    on_submit = function(value)
      if value and value ~= "" then
        para.new_folder(folder_type, value)
      end
    end,
  })
  
  input:mount()
  input:on("BufLeave", function()
    input:unmount()
  end)
end

-- Show help overlay
function M.show_help()
  local Popup = require("nui.popup")
  
  local help_popup = Popup({
    position = "50%",
    size = {
      width = 60,
      height = 20,
    },
    enter = true,
    focusable = true,
    border = {
      style = "rounded",
      text = {
        top = " Help ",
        top_align = "center",
      },
    },
  })
  
  local help_text = {
    " PARA Organize - Keyboard Shortcuts",
    "",
    " Navigation:",
    "   j/k         Move selection up/down",
    "   <Tab>       Next capture",
    "   <S-Tab>     Previous capture",
    "",
    " Actions:",
    "   <CR>        Accept selected suggestion",
    "   m           Enter merge mode",
    "   a           Archive immediately",
    "   s           Skip current capture",
    "   /           Search for destination",
    "",
    " Create New:",
    "   <leader>np  New project folder",
    "   <leader>na  New area folder",
    "   <leader>nr  New resource folder",
    "",
    " Other:",
    "   ?           Show this help",
    "   <Esc>       Close UI",
    "",
    " Press any key to close this help...",
  }
  
  help_popup:mount()
  vim.api.nvim_buf_set_lines(help_popup.bufnr, 0, -1, false, help_text)
  
  vim.keymap.set('n', '<Esc>', function()
    help_popup:unmount()
  end, { buffer = help_popup.bufnr })
  
  vim.keymap.set('n', 'q', function()
    help_popup:unmount()
  end, { buffer = help_popup.bufnr })
end

-- Get selected suggestion
function M.get_selected_suggestion()
  if ui_state.current_suggestions and ui_state.selected_suggestion_index then
    return ui_state.current_suggestions[ui_state.selected_suggestion_index]
  end
  return nil
end

-- Refresh suggestions
function M.refresh_suggestions()
  if ui_state.current_capture then
    local suggest = require("para-organize.suggest")
    local suggestions = suggest.generate_suggestions(ui_state.current_capture)
    ui_state.current_suggestions = suggestions
    M.render_suggestions(suggestions)
  end
end

-- Set status message
function M.set_status(message)
  if ui_state.capture_popup then
    ui_state.capture_popup.border:set_text("top", " Capture - " .. message .. " ")
  end
end

return M
