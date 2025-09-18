-- para-organize/suggest.lua
-- Suggestion engine for destination recommendations

local config_mod = require("para-organize.config")

local score_mod = require("para-organize.suggest.score")
local folders_mod = require("para-organize.suggest.folders")
local patterns_mod = require("para-organize.suggest.patterns")

local M = {}

M.generate_suggestions = function(capture)
  local config = config_mod.get()
  local para_folders = config_mod.get_para_folders()
  local suggestions = {}

  for folder_type, folder_path in pairs(para_folders) do
    if folder_type ~= "archives" then
      local subfolders = M.get_subfolders(folder_path)
      for _, subfolder in ipairs(subfolders) do
        local score, reasons = M.calculate_score(capture, subfolder, folder_type)
        if score > 0 then
          table.insert(suggestions, {
            path = subfolder.path,
            name = subfolder.name,
            type = folder_type,
            score = score,
            reasons = reasons or {},
          })
        end
      end
    end
  end

  if config.suggestions.always_show_archive then
    local archive_path = config_mod.get_archive_path(capture.filename)
    table.insert(suggestions, {
      path = vim.fn.fnamemodify(archive_path, ":h"),
      name = "Archive Now",
      type = "archives",
      score = 0.1,
      reasons = { "Safe default option" },
    })
  end

  table.sort(suggestions, function(a, b)
    return a.score > b.score
  end)

  local max = config.suggestions.max_suggestions
  if #suggestions > max then
    local top_suggestions = {}
    for i = 1, math.min(max - 1, #suggestions) do
      if suggestions[i].type ~= "archives" then
        table.insert(top_suggestions, suggestions[i])
      end
    end
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

M.get_subfolders = folders_mod.get_subfolders
M.calculate_score = score_mod.calculate_score
M.suggest_new_folders = patterns_mod.suggest_new_folders

-- (Leave remaining logic and glue here)
return M
