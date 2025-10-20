-- para-organize/suggest/score.lua
-- Scoring logic for suggestion engine

local M = {}

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
      if type(alias) == "string" and not alias:match("^capture_") then
        local similarity = utils.string_similarity(alias:lower(), destination.name:lower())
        if similarity > 0.6 then
          score = score + (similarity * weights.alias_similarity)
          table.insert(reasons, string.format("Alias '%s' similar", alias))
        end
      end
    end
  end

  -- 6. Context match
  if capture.context then
    local context_str = ""
    if type(capture.context) == "table" then
      context_str = table.concat(capture.context, " ")
    elseif type(capture.context) == "string" then
      context_str = capture.context
    end
    if context_str ~= "" then
      local context_lower = context_str:lower()
      local name_lower = destination.name:lower()
      if context_lower:find(name_lower, 1, true) or name_lower:find(context_lower, 1, true) then
        score = score + weights.context_match
        table.insert(reasons, "Context matches folder")
      end
    end
  end

  -- 7. Folder type bonus
  local type_bonus = { projects = 0.3, areas = 0.2, resources = 0.1 }
  if type_bonus[folder_type] then
    score = score + type_bonus[folder_type]
    table.insert(reasons, string.format("Type bonus for %s", folder_type))
  end

  return score, reasons
end

return M
