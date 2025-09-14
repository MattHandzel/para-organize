-- para-organize/learn.lua
-- Learning system for improving suggestions over time

local M = {}

-- Module state
local learning_data = {}
local learning_file = nil

-- Initialize learning system
function M.init()
  local utils = require("para-organize.utils")
  
  -- Set learning data file path
  learning_file = vim.fn.stdpath("data") .. "/para-organize/learning.json"
  
  -- Load existing learning data
  M.load_data()
  
  utils.log("DEBUG", "Learning system initialized with %d records", 
    vim.tbl_count(learning_data))
end

-- Load learning data from disk
function M.load_data()
  local utils = require("para-organize.utils")
  
  if not utils.path_exists(learning_file) then
    learning_data = {
      associations = {},
      patterns = {},
      statistics = {
        total_moves = 0,
        destinations = {},
        last_updated = os.time(),
      },
    }
    return
  end
  
  local content = utils.read_file(learning_file)
  if content then
    local ok, data = pcall(vim.json.decode, content)
    if ok and type(data) == "table" then
      learning_data = data
      utils.log("DEBUG", "Loaded learning data with %d associations",
        vim.tbl_count(learning_data.associations or {}))
    else
      utils.log("ERROR", "Failed to parse learning data")
      learning_data = {
        associations = {},
        patterns = {},
        statistics = {
          total_moves = 0,
          destinations = {},
          last_updated = os.time(),
        },
      }
    end
  end
end

-- Save learning data to disk
function M.save_data()
  local utils = require("para-organize.utils")
  
  -- Ensure directory exists
  local learning_dir = vim.fn.fnamemodify(learning_file, ":h")
  if vim.fn.isdirectory(learning_dir) == 0 then
    vim.fn.mkdir(learning_dir, "p")
  end
  
  -- Update timestamp
  learning_data.statistics.last_updated = os.time()
  
  -- Save data
  local content = vim.json.encode(learning_data)
  if utils.write_file_atomic(learning_file, content) then
    utils.log("DEBUG", "Saved learning data")
  else
    utils.log("ERROR", "Failed to save learning data")
  end
end

-- Record a successful move
function M.record_move(capture, destination)
  local config = require("para-organize.config").get()
  local utils = require("para-organize.utils")
  
  -- Extract key features for learning
  local features = M.extract_features(capture)
  
  -- Create association key
  local association_key = M.create_association_key(features)
  
  -- Initialize association if needed
  if not learning_data.associations[association_key] then
    learning_data.associations[association_key] = {
      destinations = {},
      created_at = os.time(),
      last_used = os.time(),
    }
  end
  
  local association = learning_data.associations[association_key]
  
  -- Update destination data
  if not association.destinations[destination] then
    association.destinations[destination] = {
      count = 0,
      first_used = os.time(),
      last_used = os.time(),
      success_rate = 1.0,
    }
  end
  
  local dest_data = association.destinations[destination]
  dest_data.count = dest_data.count + 1
  dest_data.last_used = os.time()
  
  -- Update association metadata
  association.last_used = os.time()
  
  -- Record patterns
  M.record_patterns(capture, destination)
  
  -- Update statistics
  learning_data.statistics.total_moves = learning_data.statistics.total_moves + 1
  
  if not learning_data.statistics.destinations[destination] then
    learning_data.statistics.destinations[destination] = 0
  end
  learning_data.statistics.destinations[destination] = 
    learning_data.statistics.destinations[destination] + 1
  
  -- Apply decay to old associations
  M.apply_decay()
  
  -- Save data
  M.save_data()
  
  utils.log("INFO", "Recorded move: %s -> %s", capture.path, destination)
end

-- Extract learning features from a capture
function M.extract_features(capture)
  local features = {
    tags = capture.tags or {},
    sources = capture.sources or {},
    has_context = capture.context ~= nil,
    modalities = capture.modalities or {},
    word_count = 0,
  }
  
  -- Estimate word count from title and aliases
  if capture.title then
    features.word_count = #vim.split(capture.title, "%s+")
  end
  
  -- Sort for consistent key generation
  table.sort(features.tags)
  table.sort(features.sources)
  table.sort(features.modalities)
  
  return features
end

-- Create association key from features
function M.create_association_key(features)
  local key_parts = {}
  
  -- Add tags
  if #features.tags > 0 then
    table.insert(key_parts, "tags:" .. table.concat(features.tags, ","))
  end
  
  -- Add sources
  if #features.sources > 0 then
    table.insert(key_parts, "sources:" .. table.concat(features.sources, ","))
  end
  
  -- Add modalities
  if #features.modalities > 0 then
    table.insert(key_parts, "modalities:" .. table.concat(features.modalities, ","))
  end
  
  -- If no specific features, use generic key
  if #key_parts == 0 then
    table.insert(key_parts, "generic")
  end
  
  return table.concat(key_parts, "|")
end

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

-- Initialize on module load
M.init()

return M
