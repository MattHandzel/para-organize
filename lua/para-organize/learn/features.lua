-- para-organize/learn/features.lua
-- Feature extraction and association key logic for learning system

local M = {}

function M.extract_features(capture)
  local features = {
    tags = capture.tags or {},
    sources = capture.sources or {},
    has_context = capture.context ~= nil,
    modalities = capture.modalities or {},
    word_count = 0,
  }
  if capture.title then
    features.word_count = #vim.split(capture.title, "%s+")
  end
  table.sort(features.tags)
  table.sort(features.sources)
  table.sort(features.modalities)
  return features
end

function M.create_association_key(features)
  local key_parts = {}
  if #features.tags > 0 then table.insert(key_parts, "tags:" .. table.concat(features.tags, ",")) end
  if #features.sources > 0 then table.insert(key_parts, "sources:" .. table.concat(features.sources, ",")) end
  if #features.modalities > 0 then table.insert(key_parts, "modalities:" .. table.concat(features.modalities, ",")) end
  if #key_parts == 0 then table.insert(key_parts, "generic") end
  return table.concat(key_parts, "|")
end

return M
