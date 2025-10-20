-- para-organize/search.lua
-- Search functionality with Telescope integration

local M = {}

-- Dependencies
local telescope = require("telescope")
local pickers = require("telescope.pickers")
local finders = require("telescope.finders")
local conf = require("telescope.config").values
local actions = require("telescope.actions")
local action_state = require("telescope.actions.state")
local previewers = require("telescope.previewers")
local config_mod = require("para-organize.config")
local entry_display = require("telescope.pickers.entry_display")
local uv = vim.loop

-- Simple cache for subfolder listings keyed by root folder path
local folder_cache = {}
-- Fetch subfolders with caching based on directory mtime
local function get_subfolders_cached(root)
  local stat = uv.fs_stat(root)
  local mtime = stat and stat.mtime.sec or 0
  local cached = folder_cache[root]
  if cached and cached.mtime == mtime then
    return cached.subfolders
  end
  -- Re-scan
  local subfolders = {}
  local find_cmd = string.format("find '%s' -type d -maxdepth 2 2>/dev/null", root)
  local handle = io.popen(find_cmd)
  if handle then
    for line in handle:lines() do
      if line ~= root then
        table.insert(subfolders, line)
      end
    end
    handle:close()
  end
  folder_cache[root] = { mtime = mtime, subfolders = subfolders }
  return subfolders
end

-- Find captures matching filters
function M.find_captures(filters)
  local indexer = require("para-organize.indexer")
  local utils = require("para-organize.utils")
  
  -- Build search criteria
  local criteria = {
    para_type = "capture",
  }
  
  -- Parse filters
  if filters then
    -- Parse tags
    if filters.tags then
      criteria.tags = utils.split(filters.tags, ",")
    end
    
    -- Parse sources
    if filters.sources then
      criteria.sources = utils.split(filters.sources, ",")
    end
    
    -- Parse modalities
    if filters.modalities then
      criteria.modalities = utils.split(filters.modalities, ",")
    end
    
    -- Parse status
    if filters.status then
      criteria.status = filters.status
    end
    
    -- Parse date filters
    if filters.since then
      criteria.since = filters.since
    end
    
    if filters.until_date then
      criteria.until_date = filters.until_date
    end
  end
  
  -- Default to unprocessed captures
  if not filters or vim.tbl_isempty(filters) then
    criteria.status = "raw"
  end
  
  -- Search index
  return indexer.search(criteria)
end

-- Create entry display for notes
local function make_entry_display(entry)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  
  local displayer = entry_display.create({
    separator = " ",
    items = {
      { width = 4 },  -- Icon/Type
      { width = 30 }, -- Title/Alias
      { width = 20 }, -- Tags
      { remaining = true }, -- Path
    },
  })
  
  -- Get icon for PARA type
  local icon = ""
  if config.ui.icons.enabled then
    local icons = config.ui.icons
    if entry.para_type == "projects" then
      icon = icons.project
    elseif entry.para_type == "areas" then
      icon = icons.area
    elseif entry.para_type == "resources" then
      icon = icons.resource
    elseif entry.para_type == "archives" then
      icon = icons.archive
    else
      icon = icons.file
    end
  end
  
  -- Get display title
  local title = entry.title or entry.filename
  if entry.aliases and #entry.aliases > 0 then
    -- Use first alias that's not capture_id
    for _, alias in ipairs(entry.aliases) do
      if not alias:match("^capture_") then
        title = alias
        break
      end
    end
  end
  
  -- Format tags
  local tags_str = ""
  if entry.tags and #entry.tags > 0 then
    tags_str = table.concat(entry.tags, ", ")
    if #tags_str > 20 then
      tags_str = tags_str:sub(1, 17) .. "..."
    end
  end
  
  -- Get relative path
  local rel_path = utils.relative_path(config.paths.vault_dir, entry.path)
  
  return displayer({
    { icon, "TelescopeResultsSpecialComment" },
    { title, "TelescopeResultsIdentifier" },
    { tags_str, "TelescopeResultsComment" },
    { rel_path, "TelescopeResultsLineNr" },
  })
end

-- Open search picker for notes
function M.open_search_picker(query)
  local config = require("para-organize.config").get()
  local indexer = require("para-organize.indexer")
  
  -- Get all notes or search by query
  local criteria = {}
  if query and query ~= "" then
    criteria.query = query
  end
  
  local results = indexer.search(criteria)
  
  -- Create picker
  local picker_config = config.telescope
  local theme_opts = {}
  
  if picker_config.theme then
    theme_opts = require("telescope.themes")["get_" .. picker_config.theme]()
  end
  
  pickers.new(theme_opts, {
    prompt_title = "Search Notes",
    finder = finders.new_table({
      results = results,
      entry_maker = function(entry)
        return {
          value = entry,
          display = make_entry_display,
          ordinal = (entry.title or "") .. " " .. 
                   table.concat(entry.tags or {}, " ") .. " " ..
                   table.concat(entry.aliases or {}, " "),
          path = entry.path,
        }
      end,
    }),
    sorter = conf.generic_sorter(theme_opts),
    previewer = picker_config.previewer and conf.file_previewer(theme_opts) or nil,
    attach_mappings = function(prompt_bufnr, map)
      -- Default action: open file
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection then
          vim.cmd("edit " .. selection.path)
        end
      end)
      
      -- Multi-select for batch operations
      if picker_config.multi_select then
        map("i", "<Tab>", actions.toggle_selection + actions.move_selection_next)
        map("n", "<Tab>", actions.toggle_selection + actions.move_selection_next)
      end
      
      return true
    end,
  }):find()
end

-- Create PARA folder picker
function M.open_folder_picker(on_select)
  local config = config_mod.get()
  local indexer = require("para-organize.indexer")
  local utils = require("para-organize.utils")
  
  -- Get all PARA folders
  local folders = {}
  
  for folder_type, folder_path in pairs(config_mod.get_para_folders()) do
    -- Get subfolders (cached)
    for _, line in ipairs(get_subfolders_cached(folder_path)) do
      local folder_name = vim.fn.fnamemodify(line, ":t")
      local note_count = #indexer.get_folder_notes(line)
      table.insert(folders, {
        path = line,
        name = folder_name,
        type = folder_type,
        count = note_count,
      })
    end
  end
  
  -- Sort folders by type then name
  table.sort(folders, function(a, b)
    if a.type ~= b.type then
      local type_order = { projects = 1, areas = 2, resources = 3, archives = 4 }
      return (type_order[a.type] or 5) < (type_order[b.type] or 5)
    end
    return a.name < b.name
  end)
  
  -- Create picker
  local picker_config = config.telescope
  local theme_opts = {}
  
  if picker_config.theme then
    theme_opts = require("telescope.themes")["get_" .. picker_config.theme]()
  end
  
  local folder_displayer = entry_display.create({
    separator = " ",
    items = {
      { width = 4 },  -- Icon
      { width = 12 }, -- Type
      { width = 30 }, -- Name
      { width = 10 }, -- Count
    },
  })
  
  pickers.new(theme_opts, {
    prompt_title = "Select Destination Folder",
    finder = finders.new_table({
      results = folders,
      entry_maker = function(entry)
        return {
          value = entry,
          display = function(e)
            local icon = ""
            if config.ui.icons.enabled then
              local icons = config.ui.icons
              if e.value.type == "projects" then
                icon = icons.project
              elseif e.value.type == "areas" then
                icon = icons.area
              elseif e.value.type == "resources" then
                icon = icons.resource
              elseif e.value.type == "archives" then
                icon = icons.archive
              else
                icon = icons.folder
              end
            end
            
            local type_str = e.value.type:sub(1, 1):upper() .. e.value.type:sub(2)
            local count_str = string.format("(%d)", e.value.count)
            
            return folder_displayer({
              { icon, "TelescopeResultsSpecialComment" },
              { type_str, "TelescopeResultsFunction" },
              { e.value.name, "TelescopeResultsIdentifier" },
              { count_str, "TelescopeResultsComment" },
            })
          end,
          ordinal = entry.type .. " " .. entry.name,
          path = entry.path,
        }
      end,
    }),
    sorter = conf.generic_sorter(theme_opts),
    previewer = false,
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection and on_select then
          on_select(selection.value)
        end
      end)
      
      return true
    end,
  }):find()
end

-- Open notes picker for a specific folder
function M.open_folder_notes_picker(folder_path, on_select)
  local config = require("para-organize.config").get()
  local indexer = require("para-organize.indexer")
  local utils = require("para-organize.utils")
  
  -- Get notes in folder
  local notes = indexer.get_folder_notes(folder_path)
  
  if #notes == 0 then
    vim.notify("No notes in folder", vim.log.levels.INFO)
    return
  end
  
  -- Create picker
  local picker_config = config.telescope
  local theme_opts = {}
  
  if picker_config.theme then
    theme_opts = require("telescope.themes")["get_" .. picker_config.theme]()
  end
  
  pickers.new(theme_opts, {
    prompt_title = "Notes in " .. vim.fn.fnamemodify(folder_path, ":t"),
    finder = finders.new_table({
      results = notes,
      entry_maker = function(entry)
        return {
          value = entry,
          display = make_entry_display,
          ordinal = (entry.title or "") .. " " .. 
                   table.concat(entry.tags or {}, " "),
          path = entry.path,
        }
      end,
    }),
    sorter = conf.generic_sorter(theme_opts),
    previewer = picker_config.previewer and conf.file_previewer(theme_opts) or nil,
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection and on_select then
          on_select(selection.value)
        end
      end)
      
      return true
    end,
  }):find()
end

-- Create saved searches picker
function M.open_saved_searches()
  local config = require("para-organize.config").get()
  
  -- Define common saved searches
  local saved_searches = {
    { name = "Unprocessed Captures", filters = { status = "raw" } },
    { name = "Today's Notes", filters = { since = os.date("%Y-%m-%d") } },
    { name = "This Week", filters = { 
      since = os.date("%Y-%m-%d", os.time() - 7 * 24 * 60 * 60) 
    }},
    { name = "With Audio", filters = { modalities = "audio" } },
    { name = "Meeting Notes", filters = { tags = "meeting" } },
    { name = "No Tags", filters = { tags = "" } },
    { name = "Projects", filters = { para_type = "projects" } },
    { name = "Areas", filters = { para_type = "areas" } },
    { name = "Resources", filters = { para_type = "resources" } },
  }
  
  -- Create picker
  local theme_opts = require("telescope.themes").get_dropdown()
  
  pickers.new(theme_opts, {
    prompt_title = "Saved Searches",
    finder = finders.new_table({
      results = saved_searches,
      entry_maker = function(entry)
        return {
          value = entry,
          display = entry.name,
          ordinal = entry.name,
        }
      end,
    }),
    sorter = conf.generic_sorter(theme_opts),
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection then
          -- Start organize session with filters
          local para = require("para-organize")
          para.start(selection.value.filters)
        end
      end)
      
      return true
    end,
  }):find()
end

-- Quick search with live results
function M.live_search()
  local config = require("para-organize.config").get()
  local indexer = require("para-organize.indexer")
  
  -- Create picker with dynamic finder
  local picker_config = config.telescope
  local theme_opts = {}
  
  if picker_config.theme then
    theme_opts = require("telescope.themes")["get_" .. picker_config.theme]()
  end
  
  pickers.new(theme_opts, {
    prompt_title = "Live Search",
    finder = finders.new_dynamic({
      fn = function(prompt)
        if prompt == "" then
          return indexer.search({})
        end
        
        -- Search with query
        return indexer.search({ query = prompt })
      end,
      entry_maker = function(entry)
        return {
          value = entry,
          display = make_entry_display,
          ordinal = (entry.title or "") .. " " .. 
                   table.concat(entry.tags or {}, " "),
          path = entry.path,
        }
      end,
    }),
    sorter = conf.generic_sorter(theme_opts),
    previewer = picker_config.previewer and conf.file_previewer(theme_opts) or nil,
    attach_mappings = function(prompt_bufnr, map)
      actions.select_default:replace(function()
        actions.close(prompt_bufnr)
        local selection = action_state.get_selected_entry()
        if selection then
          vim.cmd("edit " .. selection.path)
        end
      end)
      
      return true
    end,
  }):find()
end

return M
