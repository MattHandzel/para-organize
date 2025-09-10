-- lua/para_org/suggest.lua

-- This module contains the logic for generating and scoring
-- destination suggestions for a given note.

local Path = require('plenary.path')
local config = require('para_org.config').options

local M = {}

-- Scans the PARA directories to find all possible destinations.
-- Scans the PARA directories to find all possible destinations.
function M.get_all_destinations()
  local destinations = {}
  local root = Path:new(config.root_dir)

  for para_type, folder_name in pairs(config.folders) do
    if para_type ~= 'Archives' then
      local para_path = Path:new(root, folder_name)
      if para_path:exists() and para_path:is_dir() then
        -- Add the top-level folder itself
        table.insert(destinations, { type = para_type, path = para_path:absolute() })
        -- Add subfolders
        for path, type in para_path:iter() do
          if type == 'directory' then
            table.insert(destinations, { type = para_type, path = path:absolute() })
          end
        end
      end
    end
  end

  return destinations
end

-- Generates and scores suggestions for a given note.
function M.get_suggestions(note)
  local learn = require('para_org.learn')
  local learned_data = learn.load_data()
  local suggestions = {}
  local destinations = get_all_destinations()
  local note_tags = note.frontmatter.tags or ''
  local note_aliases = note.frontmatter.aliases or ''

  for _, dest in ipairs(destinations) do
    local dest_path = Path:new(dest.path)
    local dest_name_lower = dest_path.name:lower()
    local score = 0

    -- Score based on tag matching folder name
    if note_tags:lower():find(dest_name_lower, 1, true) then
      score = score + config.suggestions.weights.tag_folder_match
    end

    -- Score based on alias matching folder name
    if note_aliases:lower():find(dest_name_lower, 1, true) then
      score = score + config.suggestions.weights.alias_folder_match
    end

        -- Score based on learned associations
    local pattern_key = learn.get_pattern_key(note.frontmatter)
    if pattern_key and learned_data[pattern_key] and learned_data[pattern_key][dest.path] then
      local times_chosen = learned_data[pattern_key][dest.path]
      score = score + (config.suggestions.weights.learned_association * times_chosen)
    end

    if score > 0 then
      table.insert(suggestions, {
        score = score,
        type = dest.type:sub(1, 1), -- P, A, R
        path = dest.path,
        display = dest_path:shorten(40),
      })
    end
  end

  -- Sort suggestions by score, descending
  table.sort(suggestions, function(a, b) return a.score > b.score end)

  -- Always add 'Archive' as a default option
  table.insert(suggestions, { score = 0, type = '🗑', path = 'archive', display = 'Archive Now' })

  return suggestions
end

return M
