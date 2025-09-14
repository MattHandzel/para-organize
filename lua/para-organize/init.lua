-- para-organize/init.lua
-- Main entry point for para-organize.nvim

local M = {}

-- Plugin version
M.version = "0.1.0"

-- Module state
local initialized = false
local session = nil

-- Lazy-loaded modules
local modules = {}

-- Lazy load a module
local function load_module(name)
  if not modules[name] then
    modules[name] = require("para-organize." .. name)
  end
  return modules[name]
end

-- Setup function
function M.setup(user_config)
  -- Load and setup configuration
  local config = load_module("config")
  config.setup(user_config)
  
  -- Initialize utils with config
  local utils = load_module("utils")
  utils.init_logging(config.get())
  
  -- Setup user commands
  M._setup_commands()
  
  -- Setup autocommands
  M._setup_autocmds()
  
  -- Setup <Plug> mappings
  M._setup_plug_mappings()
  
  initialized = true
  
  utils.log("INFO", "para-organize.nvim v%s initialized", M.version)
end

-- Setup user commands
function M._setup_commands()
  -- Main command with subcommands
  vim.api.nvim_create_user_command("ParaOrganize", function(opts)
    local args = vim.split(opts.args, "%s+")
    local subcommand = args[1] or "start"
    
    -- Remove subcommand from args
    table.remove(args, 1)
    
    -- Parse remaining arguments as filters
    local filters = {}
    for _, arg in ipairs(args) do
      local key, value = arg:match("([^=]+)=(.+)")
      if key and value then
        filters[key] = value
      end
    end
    
    -- Execute subcommand
    if subcommand == "start" then
      M.start(filters)
    elseif subcommand == "stop" then
      M.stop()
    elseif subcommand == "next" then
      M.next()
    elseif subcommand == "prev" or subcommand == "previous" then
      M.prev()
    elseif subcommand == "skip" then
      M.skip()
    elseif subcommand == "move" then
      M.move(args[1])
    elseif subcommand == "merge" then
      M.merge()
    elseif subcommand == "archive" then
      M.archive()
    elseif subcommand == "reindex" then
      M.reindex()
    elseif subcommand == "search" then
      M.search(table.concat(args, " "))
    elseif subcommand == "help" then
      M.help()
    elseif subcommand == "new-project" then
      M.new_folder("projects", args[1])
    elseif subcommand == "new-area" then
      M.new_folder("areas", args[1])
    elseif subcommand == "new-resource" then
      M.new_folder("resources", args[1])
    else
      vim.notify("Unknown subcommand: " .. subcommand, vim.log.levels.ERROR)
    end
  end, {
    nargs = "*",
    complete = function(arg_lead, cmd_line, cursor_pos)
      local subcommands = {
        "start", "stop", "next", "prev", "skip",
        "move", "merge", "archive", "reindex",
        "search", "help", "new-project", "new-area", "new-resource"
      }
      
      -- If we're still typing the subcommand
      local args = vim.split(cmd_line:sub(1, cursor_pos), "%s+")
      if #args <= 2 then
        return vim.tbl_filter(function(cmd)
          return cmd:find("^" .. arg_lead)
        end, subcommands)
      end
      
      -- Provide filter suggestions for start command
      local subcommand = args[2]
      if subcommand == "start" and arg_lead:find("^[^=]*$") then
        local filter_keys = {
          "tags=", "sources=", "modalities=",
          "since=", "until=", "status="
        }
        return vim.tbl_filter(function(key)
          return key:find("^" .. arg_lead)
        end, filter_keys)
      end
      
      return {}
    end,
    desc = "Organize notes using the PARA method"
  })
end

-- Setup autocommands
function M._setup_autocmds()
  local config = load_module("config").get()
  
  -- Auto-reindex on save if configured
  if config.indexing.auto_reindex then
    local utils = load_module("utils")
    local debounced_reindex = utils.debounce(function(bufnr)
      local filepath = vim.api.nvim_buf_get_name(bufnr)
      if filepath and filepath ~= "" then
        local indexer = load_module("indexer")
        indexer.update_file(filepath)
      end
    end, config.indexing.incremental_debounce)
    
    vim.api.nvim_create_autocmd("BufWritePost", {
      group = vim.api.nvim_create_augroup("ParaOrganizeReindex", { clear = true }),
      pattern = "*.md",
      callback = function(args)
        debounced_reindex(args.buf)
      end,
      desc = "Update index on file save"
    })
  end
end

-- Setup <Plug> mappings
function M._setup_plug_mappings()
  -- Global <Plug> mappings
  vim.keymap.set('n', '<Plug>(ParaOrganizeStart)', function() M.start() end,
    { silent = true, desc = "Start para-organize session" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeStop)', function() M.stop() end,
    { silent = true, desc = "Stop para-organize session" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeReindex)', function() M.reindex() end,
    { silent = true, desc = "Reindex notes" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeSearch)', function() M.search() end,
    { silent = true, desc = "Search notes" })
  
  -- Session-specific <Plug> mappings (will be active in UI buffers)
  vim.keymap.set('n', '<Plug>(ParaOrganizeAccept)', function() M.accept() end,
    { silent = true, desc = "Accept suggestion" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeMerge)', function() M.merge() end,
    { silent = true, desc = "Merge with existing note" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeArchive)', function() M.archive() end,
    { silent = true, desc = "Archive current capture" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeNext)', function() M.next() end,
    { silent = true, desc = "Next capture" })
  vim.keymap.set('n', '<Plug>(ParaOrganizePrev)', function() M.prev() end,
    { silent = true, desc = "Previous capture" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeSkip)', function() M.skip() end,
    { silent = true, desc = "Skip current capture" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeNewProject)', function() M.new_folder("projects") end,
    { silent = true, desc = "Create new project" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeNewArea)', function() M.new_folder("areas") end,
    { silent = true, desc = "Create new area" })
  vim.keymap.set('n', '<Plug>(ParaOrganizeNewResource)', function() M.new_folder("resources") end,
    { silent = true, desc = "Create new resource" })
end

-- Start organizing session
function M.start(filters)
  if not initialized then
    vim.notify("para-organize not initialized. Run require('para-organize').setup() first",
      vim.log.levels.ERROR)
    return
  end
  
  -- Load UI module
  local ui = load_module("ui")
  local search = load_module("search")
  local utils = load_module("utils")
  
  -- Get captures to process
  local captures = search.find_captures(filters)
  
  if #captures == 0 then
    vim.notify("No captures found matching filters", vim.log.levels.INFO)
    return
  end
  
  -- Create session
  session = {
    captures = captures,
    current_index = 1,
    processed = {},
    filters = filters,
  }
  
  utils.log("INFO", "Started session with %d captures", #captures)
  
  -- Open UI with first capture
  ui.open(session)
  M._load_capture(1)
end

-- Stop organizing session
function M.stop()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  local ui = load_module("ui")
  local utils = load_module("utils")
  
  -- Close UI
  ui.close()
  
  -- Log session summary
  local processed_count = vim.tbl_count(session.processed)
  utils.log("INFO", "Session ended. Processed %d/%d captures",
    processed_count, #session.captures)
  
  -- Clear session
  session = nil
end

-- Navigate to next capture
function M.next()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  if session.current_index < #session.captures then
    session.current_index = session.current_index + 1
    M._load_capture(session.current_index)
  else
    vim.notify("No more captures", vim.log.levels.INFO)
  end
end

-- Navigate to previous capture
function M.prev()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  if session.current_index > 1 then
    session.current_index = session.current_index - 1
    M._load_capture(session.current_index)
  else
    vim.notify("Already at first capture", vim.log.levels.INFO)
  end
end

-- Skip current capture
function M.skip()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  local utils = load_module("utils")
  utils.log("INFO", "Skipped capture: %s", session.captures[session.current_index].path)
  
  M.next()
end

-- Accept current suggestion
function M.accept()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  local ui = load_module("ui")
  local selected = ui.get_selected_suggestion()
  
  if selected then
    M.move(selected.path)
  end
end

-- Move current capture to destination
function M.move(destination)
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  if not destination then
    vim.notify("No destination specified", vim.log.levels.ERROR)
    return
  end
  
  local move = load_module("move")
  local learn = load_module("learn")
  local utils = load_module("utils")
  
  local capture = session.captures[session.current_index]
  
  -- Perform move operation
  local success, new_path = move.move_to_destination(capture.path, destination)
  
  if success then
    -- Record learning data
    learn.record_move(capture, destination)
    
    -- Mark as processed
    session.processed[capture.path] = {
      destination = destination,
      new_path = new_path,
      timestamp = os.time()
    }
    
    utils.log("INFO", "Moved %s to %s", capture.path, destination)
    vim.notify(string.format("Moved to %s", destination), vim.log.levels.INFO)
    
    -- Move to next capture
    M.next()
  else
    vim.notify("Failed to move capture", vim.log.levels.ERROR)
  end
end

-- Enter merge mode
function M.merge()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  local ui = load_module("ui")
  ui.enter_merge_mode()
end

-- Archive current capture
function M.archive()
  if not session then
    vim.notify("No active session", vim.log.levels.WARN)
    return
  end
  
  local move = load_module("move")
  local utils = load_module("utils")
  
  local capture = session.captures[session.current_index]
  
  -- Archive the capture
  local success, archive_path = move.archive_capture(capture.path)
  
  if success then
    -- Mark as processed
    session.processed[capture.path] = {
      destination = "archive",
      new_path = archive_path,
      timestamp = os.time()
    }
    
    utils.log("INFO", "Archived %s to %s", capture.path, archive_path)
    vim.notify("Archived capture", vim.log.levels.INFO)
    
    -- Move to next capture
    M.next()
  else
    vim.notify("Failed to archive capture", vim.log.levels.ERROR)
  end
end

-- Reindex notes
function M.reindex()
  local indexer = load_module("indexer")
  local utils = load_module("utils")
  
  utils.log("INFO", "Starting full reindex")
  vim.notify("Reindexing notes...", vim.log.levels.INFO)
  
  indexer.full_reindex(function(stats)
    utils.log("INFO", "Reindex complete: %d files indexed", stats.total)
    vim.notify(string.format("Indexed %d notes in %.2fs",
      stats.total, stats.duration), vim.log.levels.INFO)
  end)
end

-- Search notes
function M.search(query)
  local search = load_module("search")
  search.open_search_picker(query)
end

-- Show help
function M.help()
  local ui = load_module("ui")
  ui.show_help()
end

-- Create new folder
function M.new_folder(folder_type, name)
  if not name then
    -- Prompt for name
    vim.ui.input({
      prompt = string.format("New %s name: ", folder_type:sub(1, -2)),
    }, function(input)
      if input and input ~= "" then
        M.new_folder(folder_type, input)
      end
    end)
    return
  end
  
  local config = load_module("config")
  local utils = load_module("utils")
  
  local para_folder = config.get_para_folder(folder_type)
  if not para_folder then
    vim.notify("Invalid folder type: " .. folder_type, vim.log.levels.ERROR)
    return
  end
  
  local new_path = para_folder .. "/" .. name
  
  -- Check if already exists
  if utils.path_exists(new_path) then
    vim.notify("Folder already exists: " .. name, vim.log.levels.WARN)
    return
  end
  
  -- Create folder
  vim.fn.mkdir(new_path, "p")
  utils.log("INFO", "Created new %s: %s", folder_type, new_path)
  vim.notify(string.format("Created %s: %s", folder_type:sub(1, -2), name),
    vim.log.levels.INFO)
  
  -- If in session and auto_move is enabled, move current capture
  if session and config.get_value("ui.auto_move_to_new_folder") then
    M.move(new_path)
  else
    -- Refresh UI to show new folder
    if session then
      local ui = load_module("ui")
      ui.refresh_suggestions()
    end
  end
end

-- Load a capture into the UI
function M._load_capture(index)
  if not session or index < 1 or index > #session.captures then
    return
  end
  
  local ui = load_module("ui")
  local suggest = load_module("suggest")
  
  local capture = session.captures[index]
  
  -- Generate suggestions
  local suggestions = suggest.generate_suggestions(capture)
  
  -- Update UI
  ui.load_capture(capture, suggestions)
  
  -- Update status
  ui.set_status(string.format("Capture %d/%d", index, #session.captures))
end

-- Get current session
function M.get_session()
  return session
end

-- Check if initialized
function M.is_initialized()
  return initialized
end

return M
