-- para-organize/suggest.lua
-- Suggestion engine for destination recommendations

local M = {}

-- Generate suggestions for a capture
function M.generate_suggestions(capture)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  local learn = require("para-organize.learn")
  local indexer = require("para-organize.indexer")
  
  local suggestions = {}
  local para_folders = config.get_para_folders()
  
  -- Score each potential destination
  for folder_type, folder_path in pairs(para_folders) do
    -- Skip archives for automatic suggestions
    if folder_type ~= "archives" then
      -- Get subfolders
      local subfolders = M.get_subfolders(folder_path)
      
      for _, subfolder in ipairs(subfolders) do
        local score = M.calculate_score(capture, subfolder, folder_type)
        
        if score > 0 then
          table.insert(suggestions, {
            path = subfolder.path,
            name = subfolder.name,
            type = folder_type,
            score = score,
            reasons = subfolder.reasons or {},
          })
        end
      end
    end
  end
  
  -- Add archive option if configured
  if config.suggestions.always_show_archive then
    local archive_path = config.get_archive_path(capture.filename)
    table.insert(suggestions, {
      path = vim.fn.fnamemodify(archive_path, ":h"),
      name = "Archive Now",
      type = "archives",
      score = 0.1, -- Low score so it appears last
      reasons = { "Safe default option" },
    })
  end
  
  -- Sort by score (highest first)
  table.sort(suggestions, function(a, b)
    return a.score > b.score
  end)
  
  -- Limit to max suggestions
  local max = config.suggestions.max_suggestions
  if #suggestions > max then
    -- Keep first max-1 and always include archive
    local top_suggestions = {}
    for i = 1, math.min(max - 1, #suggestions) do
      if suggestions[i].type ~= "archives" then
        table.insert(top_suggestions, suggestions[i])
      end
    end
    
    -- Add archive option at the end
    for _, s in ipairs(suggestions) do
      if s.type == "archives" then
        table.insert(top_suggestions, s)
        break
      end
    end
    
    suggestions = top_suggestions
  end
  
  return suggestions
end

-- Get subfolders in a PARA folder
function M.get_subfolders(folder_path)
  local utils = require("para-organize.utils")
  local subfolders = {}
  
  if not utils.is_directory(folder_path) then
    return subfolders
  end
  
  -- Use find command to get immediate subdirectories
  local cmd = string.format("find '%s' -mindepth 1 -maxdepth 1 -type d 2>/dev/null", folder_path)
  local handle = io.popen(cmd)
  
  if handle then
    for line in handle:lines() do
      local name = vim.fn.fnamemodify(line, ":t")
      table.insert(subfolders, {
        path = line,
        name = name,
        normalized_name = utils.normalize_tag(name),
      })
    end
    handle:close()
  end
  
  return subfolders
end

-- Calculate score for a potential destination
function M.calculate_score(capture, destination, folder_type)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  local learn = require("para-organize.learn")
  
  local weights = config.suggestions.weights
  local score = 0
  local reasons = {}
  
  -- 1. Exact tag match
  if capture.tags then
    for _, tag in ipairs(capture.tags) do
      if tag == destination.name or tag == destination.normalized_name then
        score = score + weights.exact_tag_match
        table.insert(reasons, string.format("Tag '%s' matches folder", tag))
      end
    end
  end
  
  -- 2. Normalized tag match
  if capture.normalized_tags then
    for _, tag in ipairs(capture.normalized_tags) do
      if tag == destination.normalized_name then
        score = score + weights.normalized_tag_match
        table.insert(reasons, string.format("Tag '%s' (normalized) matches", tag))
      end
    end
  end
  
  -- 3. Learned associations
  local learned_score = learn.get_association_score(capture, destination.path)
  if learned_score > 0 then
    score = score + (learned_score * weights.learned_association)
    table.insert(reasons, "Previously used destination")
  end
  
  -- 4. Source match
  if capture.sources then
    for _, source in ipairs(capture.sources) do
      local source_normalized = utils.normalize_tag(source)
      if source_normalized == destination.normalized_name then
        score = score + weights.source_match
        table.insert(reasons, string.format("Source '%s' matches", source))
      end
    end
  end
  
  -- 5. Alias similarity
  if capture.aliases then
    for _, alias in ipairs(capture.aliases) do
      -- Skip capture_id aliases
      if not alias:match("^capture_") then
        local similarity = utils.string_similarity(
          alias:lower(),
          destination.name:lower()
        )
        if similarity > 0.6 then
          score = score + (similarity * weights.alias_similarity)
          table.insert(reasons, string.format("Alias '%s' similar", alias))
        end
      end
    end
  end
  
  -- 6. Context match
  if capture.context then
    local context_lower = capture.context:lower()
    local name_lower = destination.name:lower()
    
    if context_lower:find(name_lower, 1, true) or
       name_lower:find(context_lower, 1, true) then
      score = score + weights.context_match
      table.insert(reasons, "Context matches folder")
    end
  end
  
  -- 7. Folder type bonus
  -- Prefer projects > areas > resources based on PARA philosophy
  local type_bonus = {
    projects = 0.3,
    areas = 0.2,
    resources = 0.1,
    archives = 0,
  }
  score = score + (type_bonus[folder_type] or 0)
  
  -- 8. Fallback: filename similarity
  if score == 0 and capture.title then
    local similarity = utils.string_similarity(
      capture.title:lower(),
      destination.name:lower()
    )
    if similarity > 0.5 then
      score = similarity * 0.5 -- Lower weight for fallback
      table.insert(reasons, "Title similarity")
    end
  end
  
  -- Store reasons in destination
  destination.reasons = reasons
  
  -- Apply minimum confidence threshold
  if score < config.suggestions.learning.min_confidence then
    return 0
  end
  
  return score
end

-- Get suggestions for batch processing
function M.get_batch_suggestions(captures)
  local suggestions_map = {}
  
  for _, capture in ipairs(captures) do
    local suggestions = M.generate_suggestions(capture)
    suggestions_map[capture.path] = suggestions
  end
  
  return suggestions_map
end

-- Analyze common patterns in captures
function M.analyze_patterns(captures)
  local patterns = {
    common_tags = {},
    common_sources = {},
    common_folders = {},
  }
  
  -- Count occurrences
  for _, capture in ipairs(captures) do
    -- Count tags
    if capture.tags then
      for _, tag in ipairs(capture.tags) do
        patterns.common_tags[tag] = (patterns.common_tags[tag] or 0) + 1
      end
    end
    
    -- Count sources
    if capture.sources then
      for _, source in ipairs(capture.sources) do
        patterns.common_sources[source] = (patterns.common_sources[source] or 0) + 1
      end
    end
  end
  
  -- Sort by frequency
  local function sort_by_count(tbl)
    local sorted = {}
    for k, v in pairs(tbl) do
      table.insert(sorted, { name = k, count = v })
    end
    table.sort(sorted, function(a, b)
      return a.count > b.count
    end)
    return sorted
  end
  
  patterns.common_tags = sort_by_count(patterns.common_tags)
  patterns.common_sources = sort_by_count(patterns.common_sources)
  
  return patterns
end

-- Suggest creating new folders based on patterns
function M.suggest_new_folders(captures)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  
  local new_folder_suggestions = {}
  local existing_folders = {}
  
  -- Get all existing folder names
  for _, folder_path in pairs(config.get_para_folders()) do
    local subfolders = M.get_subfolders(folder_path)
    for _, subfolder in ipairs(subfolders) do
      existing_folders[subfolder.normalized_name] = true
    end
  end
  
  -- Analyze patterns
  local patterns = M.analyze_patterns(captures)
  
  -- Suggest folders for common tags
  for _, tag_info in ipairs(patterns.common_tags) do
    if tag_info.count >= 3 then -- At least 3 occurrences
      local normalized = utils.normalize_tag(tag_info.name)
      if not existing_folders[normalized] then
        table.insert(new_folder_suggestions, {
          name = tag_info.name,
          type = "projects", -- Default to projects
          reason = string.format("Tag '%s' appears in %d captures", 
            tag_info.name, tag_info.count),
          confidence = tag_info.count / #captures,
        })
      end
    end
  end
  
  -- Sort by confidence
  table.sort(new_folder_suggestions, function(a, b)
    return a.confidence > b.confidence
  end)
  
  return new_folder_suggestions
end

-- Re-score suggestions based on user feedback
function M.adjust_scores_with_feedback(suggestions, feedback)
  -- This would integrate with the learning system
  -- to boost/reduce scores based on user behavior
  
  for _, suggestion in ipairs(suggestions) do
    -- Check if this destination was recently rejected
    if feedback.rejected and feedback.rejected[suggestion.path] then
      suggestion.score = suggestion.score * 0.5 -- Reduce score
    end
    
    -- Check if this destination was recently accepted
    if feedback.accepted and feedback.accepted[suggestion.path] then
      suggestion.score = suggestion.score * 1.5 -- Boost score
    end
  end
  
  -- Re-sort
  table.sort(suggestions, function(a, b)
    return a.score > b.score
  end)
  
  return suggestions
end

return M
