-- para-organize/learn.lua
-- Learning system for improving suggestions over time

local M = {}

local data_mod = require("para-organize.learn.data")
local features_mod = require("para-organize.learn.features")

-- Module state
local learning_data = {}
local learning_file = vim.fn.stdpath("data") .. "/para-organize/learning.json"

M.load_data = function()
  learning_data = data_mod.load_data(learning_file)
end

M.load_data()

M.save_data = function()
  data_mod.save_data(learning_file, learning_data)
end

M.extract_features = features_mod.extract_features
M.create_association_key = features_mod.create_association_key

-- (Leave remaining learning logic and glue here)

-- Record patterns for analysis
function M.record_patterns(capture, destination)
  if not learning_data.patterns then
    learning_data.patterns = {}
  end
  
  -- Record tag->destination patterns
  if capture.tags then
    for _, tag in ipairs(capture.tags) do
      local pattern_key = "tag:" .. tag .. "->dest:" .. destination
      if not learning_data.patterns[pattern_key] then
        learning_data.patterns[pattern_key] = {
          count = 0,
          created_at = os.time(),
          last_seen = os.time(),
        }
      end
      learning_data.patterns[pattern_key].count = 
        learning_data.patterns[pattern_key].count + 1
      learning_data.patterns[pattern_key].last_seen = os.time()
    end
  end
  
  -- Record source->destination patterns
  if capture.sources then
    for _, source in ipairs(capture.sources) do
      local pattern_key = "source:" .. source .. "->dest:" .. destination
      if not learning_data.patterns[pattern_key] then
        learning_data.patterns[pattern_key] = {
          count = 0,
          created_at = os.time(),
          last_seen = os.time(),
        }
      end
      learning_data.patterns[pattern_key].count = 
        learning_data.patterns[pattern_key].count + 1
      learning_data.patterns[pattern_key].last_seen = os.time()
    end
  end
end

-- Get association score for a capture and destination
function M.get_association_score(capture, destination)
  local config = require("para-organize.config").get()
  local learning_config = config.suggestions.learning
  
  local features = M.extract_features(capture)
  local association_key = M.create_association_key(features)
  
  local score = 0
  
  -- Check direct association
  if learning_data.associations[association_key] then
    local association = learning_data.associations[association_key]
    if association.destinations[destination] then
      local dest_data = association.destinations[destination]
      
      -- Calculate base score from frequency
      local frequency_score = math.log(1 + dest_data.count) / 
                             math.log(1 + learning_data.statistics.total_moves)
      
      -- Apply recency boost
      local days_ago = (os.time() - dest_data.last_used) / (24 * 60 * 60)
      local recency_multiplier = math.pow(learning_config.recency_decay, days_ago)
      
      -- Apply frequency boost
      local frequency_multiplier = 1.0
      if dest_data.count > 5 then
        frequency_multiplier = learning_config.frequency_boost
      end
      
      score = frequency_score * recency_multiplier * frequency_multiplier
    end
  end
  
  -- Check pattern-based associations
  local pattern_score = M.get_pattern_score(capture, destination)
  score = score + (pattern_score * 0.5) -- Weight pattern score lower
  
  return score
end

-- Get pattern-based score
function M.get_pattern_score(capture, destination)
  local score = 0
  
  if not learning_data.patterns then
    return score
  end
  
  -- Check tag patterns
  if capture.tags then
    for _, tag in ipairs(capture.tags) do
      local pattern_key = "tag:" .. tag .. "->dest:" .. destination
      if learning_data.patterns[pattern_key] then
        local pattern = learning_data.patterns[pattern_key]
        score = score + (pattern.count / 100) -- Normalize
      end
    end
  end
  
  -- Check source patterns
  if capture.sources then
    for _, source in ipairs(capture.sources) do
      local pattern_key = "source:" .. source .. "->dest:" .. destination
      if learning_data.patterns[pattern_key] then
        local pattern = learning_data.patterns[pattern_key]
        score = score + (pattern.count / 100) -- Normalize
      end
    end
  end
  
  return math.min(score, 1.0) -- Cap at 1.0
end

-- Apply decay to old associations
function M.apply_decay()
  local config = require("para-organize.config").get()
  local learning_config = config.suggestions.learning
  
  local now = os.time()
  local max_age = 90 * 24 * 60 * 60 -- 90 days in seconds
  
  -- Decay associations
  for key, association in pairs(learning_data.associations) do
    local age = now - association.last_used
    if age > max_age then
      -- Remove very old associations
      learning_data.associations[key] = nil
    end
  end
  
  -- Decay patterns
  for key, pattern in pairs(learning_data.patterns or {}) do
    local age = now - pattern.last_seen
    if age > max_age then
      -- Remove very old patterns
      learning_data.patterns[key] = nil
    end
  end
  
  -- Limit total records
  local max_history = learning_config.max_history or 1000
  
  -- If we have too many associations, remove oldest
  local associations_list = {}
  for key, assoc in pairs(learning_data.associations) do
    table.insert(associations_list, { key = key, last_used = assoc.last_used })
  end
  
  if #associations_list > max_history then
    -- Sort by last used (oldest first)
    table.sort(associations_list, function(a, b)
      return a.last_used < b.last_used
    end)
    
    -- Remove oldest
    local to_remove = #associations_list - max_history
    for i = 1, to_remove do
      learning_data.associations[associations_list[i].key] = nil
    end
  end
end

-- Get learning statistics
function M.get_statistics()
  return vim.deepcopy(learning_data.statistics or {
    total_moves = 0,
    destinations = {},
    last_updated = os.time(),
  })
end

-- Get top destinations
function M.get_top_destinations(limit)
  limit = limit or 10
  
  local destinations = {}
  for dest, count in pairs(learning_data.statistics.destinations or {}) do
    table.insert(destinations, { path = dest, count = count })
  end
  
  -- Sort by count
  table.sort(destinations, function(a, b)
    return a.count > b.count
  end)
  
  -- Return top N
  local top = {}
  for i = 1, math.min(limit, #destinations) do
    table.insert(top, destinations[i])
  end
  
  return top
end

-- Clear learning data
function M.clear()
  learning_data = {
    associations = {},
    patterns = {},
    statistics = {
      total_moves = 0,
      destinations = {},
      last_updated = os.time(),
    },
  }
  M.save_data()
end

-- Export learning data for backup
function M.export()
  return vim.deepcopy(learning_data)
end

-- Import learning data from backup
function M.import(data)
  if type(data) == "table" and data.associations and data.statistics then
    learning_data = data
    M.save_data()
    return true
  end
  return false
end

return M
