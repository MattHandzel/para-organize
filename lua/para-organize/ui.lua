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
local indexer = require("para-organize.indexer")
local suggest = require("para-organize.suggest")
local move = require("para-organize.move")
local utils = require("para-organize.utils")

-- Module state
local ui_state = {
  layout = nil,
  capture_popup = nil,
  organize_popup = nil,
  suggestions_menu = nil,
  current_capture = nil,
  current_suggestions = nil,

  merge_mode = false,
  merge_target = nil,
  session = nil,
  current_sort = 1,
  sort_modes = {"Alphabetical", "Last Modified", "Intelligent Suggestions"},
}

-- Helper function to get the letter for a PARA type
local function get_type_letter(type)
  if type == "projects" then
    return "P"
  elseif type == "areas" then
    return "A"
  elseif type == "resources" then
    return "R"
  elseif type == "archives" then
    return ""
  else
    return "?"
  end
end

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
    
    local position = ui_config.float_opts.position or "50%"
    if position == "center" then
      position = { row = "50%", col = "50%" }
    elseif type(position) == "string" then
      position = { row = position, col = position }
    end
    
    ui_state.layout = Layout(
      {
        position = position,
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
  
  -- Load capture content into left pane
  M.render_capture(capture)
  
  -- Load suggestions into right pane
  M.render_suggestions(suggestions)
  
  -- Focus organize pane
  vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
  vim.api.nvim_win_set_cursor(ui_state.organize_popup.winid, { 1, 0 })
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
  
  local metadata = capture.metadata or {}
  local tags = table.concat(metadata.tags or {}, ", ")
  local sources = table.concat(metadata.sources or {}, ", ")
  local aliases = table.concat(metadata.aliases or {}, ", ")
  local timestamp = os.date("%B %d, %H:%M", metadata.timestamp)
  local notes_count = #indexer.get_capture_notes()
  
  local lines = {
    "# Capture Note",
    "",
    "**Notes to Organize:** " .. notes_count,
    "**Timestamp:** " .. timestamp,
    "**Aliases:** " .. aliases,
    "**Tags:** " .. tags,
    "**Sources:** " .. sources,
    "",
  }
  
  vim.list_extend(lines, vim.split(body, "\n"))
  
  vim.api.nvim_buf_set_lines(ui_state.capture_popup.bufnr, 0, -1, false, lines)
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "filetype", "markdown")
end

-- Render suggestions in right pane
function M.render_suggestions(suggestions)
  local config = require("para-organize.config").get()
  local ui_config = config.ui

  -- Header
  local header = Line()
  header:append(Text("Suggestions", "Title"))
  header:append(Text(string.format(" (Sort: %s)", ui_state.sort_modes[ui_state.current_sort]), "Comment"))

  local lines = {}
  for _, suggestion in ipairs(suggestions) do
    local line = Line()

    line:append(Text("  ")) -- Indent
    line:append(Text("[", "Comment"))
    line:append(Text(get_type_letter(suggestion.type), "ParaOrganizerUIType" .. suggestion.type:sub(1, 1):upper() .. suggestion.type:sub(2)))
    line:append(Text("] ", "Comment"))
    line:append(Text(suggestion.name))

    if suggestion.match_count and suggestion.match_count > 0 then
      line:append(Text(string.format(" (%d)", suggestion.match_count), "Comment"))
    end

    table.insert(lines, line)
  end

  ui_state.organize_popup:set_lines(header, lines)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", false)
  vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
end

-- Setup keymaps
function M.setup_keymaps()
  local config = require("para-organize.config").get()
  local keymaps = config.keymaps.buffer

  -- Capture pane keymaps
  local capture_opts = { buffer = ui_state.capture_popup.bufnr, silent = true }

  vim.keymap.set('n', keymaps.quit, M.close, capture_opts)
  vim.keymap.set('n', keymaps.organize, function()
    M.organize_item()
  end, capture_opts)
  vim.keymap.set('n', keymaps.merge, M.enter_merge_mode, capture_opts)
  vim.keymap.set('n', keymaps.search, M.search_folders, capture_opts)
  vim.keymap.set('n', keymaps.help, M.show_help, capture_opts)

  -- Organize pane keymaps
  local organize_opts = { buffer = ui_state.organize_popup.bufnr, silent = true, noremap = true }

  vim.keymap.set('n', keymaps.cancel, M.close, organize_opts)
  vim.keymap.set('n', 'j', 'j', organize_opts)
  vim.keymap.set('n', 'k', 'k', organize_opts)
  vim.keymap.set('n', 'gg', 'gg', organize_opts)
  vim.keymap.set('n', 'G', 'G', organize_opts)
  vim.keymap.set('n', '<C-d>', '<C-d>', organize_opts)
  vim.keymap.set('n', '<C-u>', '<C-u>', organize_opts)
  vim.keymap.set('n', '<CR>', M.organize_item, organize_opts)
  vim.keymap.set('n', 's', M.change_sort_order, organize_opts)
  vim.keymap.set('n', '/', M.search_folders, organize_opts)
  vim.keymap.set('n', '<BS>', M.back_to_parent, organize_opts)

  -- Quick folder creation
  vim.keymap.set('n', keymaps.new_project, function()
    M.create_new_folder("projects")
  end, organize_opts)
  vim.keymap.set('n', keymaps.new_area, function()
    M.create_new_folder("areas")
  end, organize_opts)
  vim.keymap.set('n', keymaps.new_resource, function()
    M.create_new_folder("resources")
  end, organize_opts)
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

-- Action functions
function M.organize_item()
  if not ui_state.current_suggestions or not ui_state.current_capture then
    return
  end

  -- Determine the selected suggestion from the cursor position
  local cursor_line = vim.api.nvim_win_get_cursor(ui_state.organize_popup.winid)[1]
  local header_offset = 2 -- For "Suggestions" title and the following line
  local suggestion_index = cursor_line - header_offset

  if suggestion_index <= 0 or suggestion_index > #ui_state.current_suggestions then
    utils.log("WARN", "No suggestion selected or invalid line.")
    return
  end

  local suggestion = ui_state.current_suggestions[suggestion_index]
  if not suggestion then
    return
  end

  local destination_path
  if suggestion.type == "new_file" then
    destination_path = suggestion.path
  else
    destination_path = suggestion.full_path
  end

  local session = require("para-organize.session")
  session.organize_capture(ui_state.current_capture, destination_path, ui_state.merge_mode)
end

function M.enter_merge_mode()
  ui_state.merge_mode = not ui_state.merge_mode
  local status = ui_state.merge_mode and "enabled" or "disabled"
  vim.notify("Merge mode " .. status, vim.log.levels.INFO, { title = "Para Organizer" })
end

function M.change_sort_order()
  ui_state.current_sort = (ui_state.current_sort % #ui_state.sort_modes) + 1
  local new_suggestions = suggest.get_suggestions(ui_state.current_capture)
  M.load_capture(ui_state.current_capture, new_suggestions)
end

function M.search_folders()
  -- To be implemented: use Telescope or other picker
  utils.log("INFO", "Search folders feature not yet implemented.")
end

function M.show_help()
  local Popup = require("nui.popup")

  local help_popup = Popup({
    position = "50%",
    size = {
      width = "80%",
      height = "80%",
    },
    enter = true,
    focusable = true,
    border = {
      style = "single",
      text = {
        top = " Help ",
        top_align = "center",
      },
    },
    win_options = {
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
  })

  help_popup:mount()

  local keymaps = require("para-organize.config").get().keymaps.buffer
  local help_content = {
    "Para Organizer Help",
    "===================",
    "",
    "Capture Pane Keymaps:",
    string.format("  %-20s %s", keymaps.quit, "Close the UI"),
    string.format("  %-20s %s", keymaps.organize, "Organize the capture"),
    string.format("  %-20s %s", keymaps.merge, "Toggle merge mode"),
    string.format("  %-20s %s", keymaps.search, "Search folders"),
    string.format("  %-20s %s", keymaps.help, "Show this help"),
    "",
    "Organize Pane Keymaps:",
    string.format("  %-20s %s", keymaps.cancel, "Close the UI"),
    string.format("  %-20s %s", "j/k, gg, G", "Navigate suggestions"),
    string.format("  %-20s %s", "s", "Change sort order"),
    string.format("  %-20s %s", "/", "Search folders"),
    string.format("  %-20s %s", "<CR>", "Organize to selected item"),
    string.format("  %-20s %s", "<BS>", "Go to parent folder"),
    string.format("  %-20s %s", keymaps.new_project, "Create new project"),
    string.format("  %-20s %s", keymaps.new_area, "Create new area"),
    string.format("  %-20s %s", keymaps.new_resource, "Create new resource"),
  }

  vim.api.nvim_buf_set_lines(help_popup.bufnr, 0, -1, false, help_content)

  vim.keymap.set('n', 'q', function()
    help_popup:unmount()
  end, { buffer = help_popup.bufnr, silent = true })
end

function M.back_to_parent()
  -- To be implemented
  utils.log("INFO", "Back to parent feature not yet implemented.")
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
      style = "single",
      text = {
        top = " Create New " .. folder_type:sub(1, 1):upper() .. folder_type:sub(2), -- Capitalize
        top_align = "center",
      },
    },
    win_options = {
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
  },
  {
    prompt = "> ",
    on_submit = function(value)
      if value and value ~= "" then
        para.create_new_item(folder_type, value)
        -- Refresh suggestions
        local new_suggestions = suggest.get_suggestions(ui_state.current_capture)
        M.load_capture(ui_state.current_capture, new_suggestions)
      end
    end,
  })

  input:mount()
end

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
  if  ui_state.current_suggestions = suggestions then
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

-- Change sort order in right pane
function M.change_sort_order()
  ui_state.current_sort = ui_state.current_sort + 1
  if ui_state.current_sort > 3 then ui_state.current_sort = 1 end
  
  local sort_label = {
    "Alphabetical",
    "Last Modified",
    "Intelligent Suggestions"
  }
  
  local dirs = indexer.get_para_directories()
  if ui_state.current_sort == 1 then
    table.sort(dirs, function(a, b) return a.name < b.name end)
  elseif ui_state.current_sort == 2 then
    table.sort(dirs, function(a, b) return a.modified > b.modified end)
  else
    dirs = suggest.get_suggestions(ui_state.current_capture)
  end
  
  local content = {
    "# Organization Pane",
    "",
    "**Sort Order:** " .. sort_label[ui_state.current_sort] .. " (Press 's' to change sort)",
    "Press '/' to search, 'Enter' to open folder or merge note",
    ""
  }
  for _, dir in ipairs(dirs) do
    local type_letter = get_type_letter(dir.type)
    table.insert(content, string.format("[%s] %s", type_letter, dir.name))
  end
  
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
end

-- Open directory or file in right pane
function M.open_item()
  local line = vim.api.nvim_win_get_cursor(ui_state.organize_popup.winid)[1]
  local content = vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, line - 1, line, false)[1]
  if content:match("^%[.%] ") then
    local name = content:match("^%[.%] (.+)")
    local dir = indexer.get_directory_by_name(name)
    if dir then
      local sub_items = indexer.get_sub_items(dir.path)
      local content = {
        "# " .. name,
        "",
        "Press 'Enter' on a note to merge, 'Backspace' to go back",
        ""
      }
      for _, item in ipairs(sub_items) do
        if item.type == "directory" then
          table.insert(content, "[D] " .. item.name)
        else
          table.insert(content, "[F] " .. (item.alias or item.name))
        end
      end
      vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
      vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", false)
    end
  elseif content:match("^%[F%] ") then
    local name = content:match("^%[F%] (.+)")
    local file = indexer.get_file_by_alias_or_name(name)
    if file then
      vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
      local content = {
        "# Merging to: " .. name,
        "",
        "Edit this note to merge content from the capture note.",
        "Close the buffer to complete merging.",
        ""
      }
      local file_content = utils.read_file(file.path) or ""
      vim.list_extend(content, vim.split(file_content, "\n"))
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
      vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
      vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "filetype", "markdown")
      vim.api.nvim_win_set_buf(ui_state.organize_popup.winid, ui_state.organize_popup.bufnr)
      vim.api.nvim_buf_attach(ui_state.organize_popup.bufnr, false, {
        on_detach = function()
          local edited_content = table.concat(vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, 0, -1, false), "\n")
          utils.write_file(file.path, edited_content)
          move.archive_capture(ui_state.current_capture)
          load_next_capture()
        end
      })
    end
  end
end

-- Back to parent directory
function M.back_to_parent()
  M.render_suggestions(ui_state.current_suggestions)
end

-- Public accessors for testing
function M.is_open()
  return ui_state.layout ~= nil
end

function M.get_organize_bufnr()
  return ui_state.organize_popup and ui_state.organize_popup.bufnr
end

function M.get_organize_winid()
  return ui_state.organize_popup and ui_state.organize_popup.winid
end

return M
