-- lua/para_org/learn.lua

-- This module handles the learning mechanism, persisting associations
-- between note metadata and destination folders to improve future suggestions.

local Path = require('plenary.path')
local config = require('para_org.config').options

local M = {}

local function get_learn_file_path()
  return Path:new(vim.fn.stdpath('data'), 'para_org_learn.json')
end

function M.load_data()
  local learn_file = get_learn_file_path()
  if not learn_file:exists() then
    return {}
  end
  local content, err = learn_file:read()
  if err or not content then
    return {}
  end
  return vim.fn.json_decode(content)
end

local function save_data(data)
  get_learn_file_path():write(vim.fn.json_encode(data), 'w')
end

-- Creates a unique key from the note's tags to represent the pattern.
-- A simple approach is to sort and join the tags.
local function get_pattern_key(note_metadata)
  local tags = note_metadata.tags or ''
  if tags == '' then return nil end

  local tag_list = vim.split(tags, '%s*,%s*')
  table.sort(tag_list)
  return table.concat(tag_list, '|'):lower()
end

-- Records a successful move to improve future suggestions.
function M.record_move(note_metadata, destination_path)
  local pattern_key = get_pattern_key(note_metadata)
  if not pattern_key then return end

  local data = M.load_data()
  data[pattern_key] = data[pattern_key] or {}
  data[pattern_key][destination_path] = (data[pattern_key][destination_path] or 0) + 1

  save_data(data)
  vim.notify('Learning: Recorded move to ' .. Path:new(destination_path):shorten(), vim.log.levels.INFO)
end

return M
