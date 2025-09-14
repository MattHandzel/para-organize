-- para-organize/ui.lua
-- Two-pane UI for organizing notes

local M = {}

-- Dependencies
local layout_mod = require("para-organize.ui.layout")
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
  selected_suggestion_index = 1,
  merge_mode = false,
  merge_target = nil,
  session = nil,
  current_sort = 1,
  -- Track navigation state
  current_directory = nil, -- Current directory being viewed
  directory_stack = {}, -- Navigation history for back button
}

local render_mod = require("para-organize.ui.render")

-- Create the two-pane layout
function M.create_layout()
  local config = require("para-organize.config").get()
  local ui_config = config.ui
  local layout_objs = layout_mod.create_layout(ui_config)
  ui_state.layout = layout_objs.layout
  ui_state.capture_popup = layout_objs.capture_popup
  ui_state.organize_popup = layout_objs.organize_popup
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
  render_mod.render_capture(ui_state, capture)

  -- Load suggestions into right pane
  render_mod.render_suggestions(ui_state, suggestions)

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
      "Error: Could not read capture file",
    })
    return
  end

  -- Parse frontmatter
  local frontmatter_str, body =
    utils.extract_frontmatter(content, config.patterns.frontmatter_delimiters)

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

  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "modifiable", true)
  vim.api.nvim_buf_set_lines(ui_state.capture_popup.bufnr, 0, -1, false, lines)
  -- Keep buffer modifiable to allow normal cursor movement and editing
  vim.api.nvim_buf_set_option(ui_state.capture_popup.bufnr, "filetype", "markdown")
end

-- Render suggestions in right pane
function M.render_suggestions(suggestions)
  local config = require("para-organize.config").get()
  local ui_config = config.ui

  if ui_state.merge_mode then
    M.render_merge_view()
    return
  end

  -- Reset directory state when going back to main suggestions view
  if ui_state.current_directory then
    vim.notify(
      "Resetting directory state, was in: " .. ui_state.current_directory.name,
      vim.log.levels.DEBUG
    )
  end
  ui_state.current_directory = nil
  ui_state.directory_stack = {}
  vim.notify("Showing main suggestions view", vim.log.levels.DEBUG)

  local dirs = indexer.get_para_directories()
  local content = {
    "# Organization Pane",
    "",
end

-- Render merge view (delegated)
function M.render_merge_view()
  render_mod.render_merge_view(ui_state)
end

-- Highlight selected suggestion (delegated)
function M.highlight_selection()
  render_mod.highlight_selection(ui_state)
end

-- Render directories
function M.render_directories(dirs)
  render_mod.render_directories(ui_state, dirs)
end

-- Setup keymaps
function M.setup_keymaps()
  local config = require("para-organize.config").get()
  local keymaps = config.keymaps.buffer

  -- Capture pane keymaps
  local capture_opts = { buffer = ui_state.capture_popup.bufnr, silent = true }

  vim.keymap.set("n", keymaps.accept, M.accept_suggestion, capture_opts)
  vim.keymap.set("n", keymaps.cancel, M.close, capture_opts)
  vim.keymap.set("n", keymaps.next, M.next_capture, capture_opts)
  vim.keymap.set("n", keymaps.prev, M.prev_capture, capture_opts)
  vim.keymap.set("n", keymaps.skip, M.skip_capture, capture_opts)
  vim.keymap.set("n", keymaps.archive, M.archive_capture, capture_opts)
  vim.keymap.set("n", keymaps.merge, M.enter_merge_mode, capture_opts)
  vim.keymap.set("n", keymaps.search, M.search_inline, capture_opts)
  vim.keymap.set("n", keymaps.help, M.show_help, capture_opts)
  -- Use Alt+j/k for suggestion navigation instead of overriding j/k
  vim.keymap.set("n", "<A-j>", M.next_suggestion, capture_opts)
  vim.keymap.set("n", "<A-k>", M.prev_suggestion, capture_opts)
  -- Add pane switching
  vim.keymap.set("n", "<C-l>", function()
    vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
  end, capture_opts)

  -- Organize pane keymaps
  local organize_opts = { buffer = ui_state.organize_popup.bufnr, silent = true }

  vim.keymap.set("n", keymaps.accept, M.accept_suggestion, organize_opts)
  vim.keymap.set("n", keymaps.cancel, M.close, organize_opts)
  -- Use Alt+j/k for suggestion navigation instead of overriding j/k
  vim.keymap.set("n", "<A-j>", M.next_suggestion, organize_opts)
  vim.keymap.set("n", "<A-k>", M.prev_suggestion, organize_opts)
  vim.keymap.set("n", "s", M.change_sort_order, organize_opts)
  vim.keymap.set("n", "/", M.search_inline, organize_opts)
  -- Map Enter to open_item for all modes
  vim.keymap.set("n", "<CR>", function()
    vim.notify(
      "Enter key pressed, merge_mode = " .. tostring(ui_state.merge_mode),
      vim.log.levels.INFO
    )
    M.open_item() -- Always call open_item, it will handle different states
  end, organize_opts)
  vim.keymap.set("n", "<BS>", M.back_to_parent, organize_opts)
  -- Add pane switching
  vim.keymap.set("n", "<C-h>", function()
    vim.api.nvim_set_current_win(ui_state.capture_popup.winid)
  end, organize_opts)

  -- Quick folder creation
  vim.keymap.set("n", keymaps.new_project, function()
    M.create_new_folder("projects")
  end, capture_opts)

  vim.keymap.set("n", keymaps.new_area, function()
    M.create_new_folder("areas")
  end, capture_opts)

  vim.keymap.set("n", keymaps.new_resource, function()
    M.create_new_folder("resources")
  end, capture_opts)
end

-- Setup autocmds
function M.setup_autocmds()
  local group = vim.api.nvim_create_augroup("ParaOrganizeUI", { clear = true })

  -- Clean up on window close
  vim.api.nvim_create_autocmd("WinClosed", {
    group = group,
    pattern = tostring(ui_state.capture_popup.winid) .. "," .. tostring(
      ui_state.organize_popup.winid
    ),
    callback = function()
      M.close()
    end,
  })
end

-- Navigation functions
function M.next_suggestion()
  if
    ui_state.current_suggestions
    and ui_state.selected_suggestion_index < #ui_state.current_suggestions
  then
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
  if
    not ui_state.current_suggestions
    or not ui_state.current_suggestions[ui_state.selected_suggestion_index]
  then
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

  -- Show visual indicator that we're entering merge mode
  vim.notify("Entering merge mode - select a destination folder", vim.log.levels.INFO)
  ui_state.capture_popup.border:set_text("top", " Capture (Merge Mode) ")
  ui_state.organize_popup.border:set_text("top", " Select Destination ")

  -- Debug info
  vim.notify("DEBUG: merge_mode is being set to true", vim.log.levels.DEBUG)
  ui_state.merge_mode = true

  -- Open folder picker to select destination
  search.open_folder_picker(function(folder)
    if not folder then
      -- User cancelled folder selection
      ui_state.merge_mode = false
      ui_state.capture_popup.border:set_text("top", " Capture ")
      ui_state.organize_popup.border:set_text("top", " Organize ")
      vim.notify("Merge mode cancelled", vim.log.levels.INFO)
      return
    end

    -- Show visual indicator for selecting a note
    ui_state.organize_popup.border:set_text("top", " Select Note to Merge ")
    vim.notify("Select a note to merge into", vim.log.levels.INFO)

    -- Open notes picker for selected folder
    search.open_folder_notes_picker(folder.path, function(note)
      if not note then
        -- User cancelled note selection
        ui_state.merge_mode = false
        ui_state.capture_popup.border:set_text("top", " Capture ")
        ui_state.organize_popup.border:set_text("top", " Organize ")
        vim.notify("Merge mode cancelled", vim.log.levels.INFO)
        return
      end

      ui_state.merge_target = note
      M.render_merge_view()
    end)
  end)
end

-- Search directly in right pane (inline search)
function M.search_inline()
  local config = require("para-organize.config").get()
  local Input = require("nui.input")
  local indexer = require("para-organize.indexer")

  -- Create small input field at the top of right pane
  local input = Input({
    position = {
      row = 1,
      col = math.floor(vim.o.columns / 2) + 5,
    },
    size = {
      width = 30,
    },
    border = {
      style = "rounded",
      text = {
        top = " Search ",
        top_align = "center",
      },
    },
    win_options = {
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
    },
  }, {
    prompt = "🔍 ",
    default_value = "",
    on_submit = function(value)
      if value and value ~= "" then
        local results = {}
        local search_header = ""

        -- Check if we're in a directory - search within that directory
        if ui_state.current_directory then
          vim.notify(
            "Searching within directory: " .. ui_state.current_directory.name,
            vim.log.levels.INFO
          )
          local sub_items = indexer.get_sub_items(ui_state.current_directory.path)

          for _, item in ipairs(sub_items) do
            if item.name:lower():find(value:lower()) then
              table.insert(results, item)
            end
          end

          search_header = "# Search Results in '"
            .. ui_state.current_directory.name
            .. "' for: "
            .. value
        else
          -- Otherwise search all top-level directories
          vim.notify("Searching all directories", vim.log.levels.INFO)
          local folders = indexer.get_para_directories()

          for _, dir in ipairs(folders) do
            if dir.name:lower():find(value:lower()) then
              table.insert(results, dir)
            end
          end

          search_header = "# Search Results for: " .. value
        end

        -- Show search results in right pane
        local content = {
          search_header,
          "",
          "Press 'Enter' to open folder or file, 'Backspace' to go back",
          "",
        }

        if #results == 0 then
          table.insert(content, "No results found.")
        else
          for _, item in ipairs(results) do
            local type_letter = nil
            if item.type == "directory" then
              type_letter = "D"
            elseif item.type == "file" then
              type_letter = "F"
            else
              -- For top-level PARA folders
              type_letter = get_type_letter(item.type)
            end
            table.insert(content, string.format("[%s] %s", type_letter, item.name))
          end
        end

        vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
        vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
        vim.api.nvim_buf_set_option(ui_state.organize_popup.bufnr, "modifiable", true)
      end
    end,
  })

  input:mount()
  vim.api.nvim_set_current_win(input.winid)
  vim.cmd("startinsert")

  -- Close input when focus lost
  input:on("BufLeave", function()
    input:unmount()
  end)
end

-- Use Telescope for search (traditional approach)
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
    "   j/k         Normal cursor movement",
    "   <Alt-j/k>   Move selection up/down",
    "   <Tab>       Next capture",
    "   <S-Tab>     Previous capture",
    "   Up/Down     Move cursor (all normal Vim navigation works)",
    "   <C-h>/<C-l>  Switch between left/right panes",
    "",
    " Actions:",
    "   <CR>        Open folder, select file for merge, or accept suggestion",
    "   m           Enter merge mode (via picker)",
    "   a           Archive immediately",
    "   s           Skip current capture",
    "   /           Search in right pane (context-aware, searches current directory)",
    "   <BS>        Go back to parent directory",
    "",
    " Merge Operations:",
    "   <leader>mc  Complete merge (save changes)",
    "   <leader>mx  Cancel merge",
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

  vim.keymap.set("n", "<Esc>", function()
    help_popup:unmount()
  end, { buffer = help_popup.bufnr })

  vim.keymap.set("n", "q", function()
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

-- Change sort order in right pane
function M.change_sort_order()
  ui_state.current_sort = ui_state.current_sort + 1
  if ui_state.current_sort > 3 then
    ui_state.current_sort = 1
  end

  local sort_label = {
    "Alphabetical",
    "Last Modified",
    "Intelligent Suggestions",
  }

  local dirs = indexer.get_para_directories()
  if ui_state.current_sort == 1 then
    table.sort(dirs, function(a, b)
      return a.name < b.name
    end)
  elseif ui_state.current_sort == 2 then
    table.sort(dirs, function(a, b)
      return a.modified > b.modified
    end)
  else
    dirs = suggest.get_suggestions(ui_state.current_capture)
  end

  local content = {
    "# Organization Pane",
    "",
    "**Sort Order:** " .. sort_label[ui_state.current_sort] .. " (Press 's' to change sort)",
    "Press '/' to search, 'Enter' to open folder or merge note",
    "",
  }
  M.render_directories(dirs)
  vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
end

-- Open directory or file in right pane
function M.open_item()
  -- Output debug info
  vim.notify("DEBUG: open_item function called", vim.log.levels.DEBUG)

  -- Get the current line under cursor
  local line = vim.api.nvim_win_get_cursor(ui_state.organize_popup.winid)[1]
  local content =
    vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, line - 1, line, false)[1]

  if not content then
    vim.notify("No content found at current line", vim.log.levels.WARN)
    return
  end

  vim.notify("Line content: " .. content, vim.log.levels.DEBUG)

  -- Check if this is a directory entry (P/A/R folder or D for directory)
  if
    content:match("^%[P%] ")
    or content:match("^%[A%] ")
    or content:match("^%[R%] ")
    or content:match("^%[D%] ")
  then
    local name = content:match("^%[.%] (.+)")
    vim.notify("Opening directory: " .. name, vim.log.levels.INFO)
    local dir = indexer.get_directory_by_name(name)
    if dir then
      -- Store the previous directory in the stack for back navigation
      if ui_state.current_directory then
        table.insert(ui_state.directory_stack, ui_state.current_directory)
      end

      -- Set current directory
      ui_state.current_directory = dir
      vim.notify("Current directory set to: " .. dir.path, vim.log.levels.DEBUG)

      local sub_items = indexer.get_sub_items(dir.path)
      local content = {
        "# " .. name,
        "",
        "Press 'Enter' on a note to merge, 'Backspace' to go back",
        "",
      }
      M.render_directories(sub_items)
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
      -- Keep buffer modifiable for cursor movement
      vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
    end
  -- Check if this is a file entry
  elseif content:match("^%[F%] ") then
    local name = content:match("^%[F%] (.+)")
    vim.notify("Selected file for merge: " .. name, vim.log.levels.INFO)
    local file = indexer.get_file_by_alias_or_name(name)
    if file then
      vim.notify("Starting merge with file: " .. file.name, vim.log.levels.INFO)
      -- Set up merge operation
      ui_state.merge_mode = true
      ui_state.merge_target = file

      -- Call render_merge_view to show the merge interface
      -- This will handle reading files and rendering the merge view
      M.render_merge_view()

      -- Add keybindings for merge actions
      local opts = { buffer = ui_state.organize_popup.bufnr, silent = true }
      vim.keymap.set("n", "<C-s>", function()
        -- Complete merge
        local utils = require("para-organize.utils") -- Get utils inside this scope
        local move = require("para-organize.move") -- Get move inside this scope
        local edited_content = table.concat(
          vim.api.nvim_buf_get_lines(ui_state.organize_popup.bufnr, 0, -1, false),
          "\n"
        )
        if utils.write_file_atomic(file.path, edited_content) then
          vim.notify("Successfully merged content to " .. file.name, vim.log.levels.INFO)
          -- Pass the entire capture object, not just the path
          move.archive_capture(ui_state.current_capture)
          M.render_suggestions(ui_state.current_suggestions)
          ui_state.merge_mode = false
          ui_state.merge_target = nil
        end
      end, opts)

      vim.keymap.set("n", "<leader>mx", function()
        -- Cancel merge
        ui_state.merge_mode = false
        ui_state.merge_target = nil
        M.render_suggestions(ui_state.current_suggestions)
        vim.notify("Merge canceled", vim.log.levels.INFO)
      end, opts)

      -- Focus the organize pane
      vim.api.nvim_set_current_win(ui_state.organize_popup.winid)
    end
  end
end

-- Back to parent directory
function M.back_to_parent()
  vim.notify("Going back from directory", vim.log.levels.DEBUG)

  -- Initialize directory stack if it doesn't exist
  if ui_state.directory_stack == nil then
    ui_state.directory_stack = {}
  end

  -- If we're in a directory, go back to the previous one
  if #ui_state.directory_stack > 0 then
    -- Pop the previous directory
    ui_state.current_directory = table.remove(ui_state.directory_stack)

    -- If we're back at the root level, display main suggestions
    if #ui_state.directory_stack == 0 and ui_state.current_directory == nil then
      vim.notify("Returning to root level", vim.log.levels.DEBUG)
      M.render_suggestions(ui_state.current_suggestions)
      return
    end

    -- Otherwise show the contents of the previous directory
    if ui_state.current_directory then
      vim.notify(
        "Going back to directory: " .. ui_state.current_directory.path,
        vim.log.levels.DEBUG
      )
      local sub_items = indexer.get_sub_items(ui_state.current_directory.path)
      local content = {
        "# " .. ui_state.current_directory.name,
        "",
        "Press 'Enter' on a note to merge, 'Backspace' to go back",
        "",
      }

      M.render_directories(sub_items)
      vim.api.nvim_buf_set_lines(ui_state.organize_popup.bufnr, 0, -1, false, content)
      return
    end
  else
    -- If we're already at the root level, just show the main suggestions
    ui_state.current_directory = nil
    M.render_suggestions(ui_state.current_suggestions)
  end
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
