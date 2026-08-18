--- para-organize.ui.render — PURE line/highlight builders for the organize UI.
---
--- Nothing in this module touches Neovim state, the filesystem, or the core.
--- It turns a session state table (spec 03 §3) into `{lines, marks, map}` so
--- every rendering rule of the spec can be asserted without mounting windows.
---
--- THIN CLIENT LAW (spec 10 §1): no file reads happen here. Every value
--- rendered arrives from an RPC payload that the caller already fetched.
local M = {}

--- Type letters exactly as spec 03 §3 states them: `[P]`/`[A]`/`[R]`/`[🗑]`
--- for suggestions, `[D]`/`[F]` while browsing a folder's children.
M.TYPE_LETTERS = {
  project = "P",
  area = "A",
  resource = "R",
  archive = "🗑",
  dir = "D",
  file = "F",
  capture = "F",
  other = "F",
}

--- Plural PARA-type names as the core reports them (`Suggestion.type`).
M.TYPE_ALIASES = {
  projects = "project",
  areas = "area",
  resources = "resource",
  archives = "archive",
}

--- Sort cycle for the browse view (spec 03 §3 state 2). `S` cycles these.
M.SORT_MODES = { "alphabetical", "modified", "intelligent" }
M.SORT_LABELS = {
  alphabetical = "Alphabetical",
  modified = "Last Modified",
  intelligent = "Intelligent Suggestions",
}

--- Score → highlight bucket. The thresholds were `render.SCORE_HIGH` /
--- `SCORE_MEDIUM` constants; spec 15 §6 promotes them OUT of the source into
--- `ui.organize.score_thresholds` (ruling R3), because a differently-weighted
--- vault (04 §1) needs different buckets. These are the fallbacks used when
--- no config is passed — the spec 03 §3 literals.
M.SCORE_THRESHOLDS = { high = 2.0, medium = 1.0 }

-- --- small helpers ---------------------------------------------------------

---@param value any
---@return integer
--- `processed`/`skipped` may be a counter or a list of paths depending on how
--- the (integrator-owned) state module models them; both are read here.
function M.count(value)
  if type(value) == "number" then
    return value
  end
  if type(value) == "table" then
    return #value
  end
  return 0
end

local function as_list(value)
  if value == nil then
    return {}
  end
  if type(value) == "table" then
    if vim.islist and vim.islist(value) then
      return value
    end
    if vim.tbl_islist and vim.tbl_islist(value) then
      return value
    end
    return value
  end
  return { value }
end

local function join(value, sep)
  local out = {}
  for _, item in ipairs(as_list(value)) do
    if item ~= nil and item ~= "" then
      table.insert(out, tostring(item))
    end
  end
  if #out == 0 then
    return nil
  end
  return table.concat(out, sep or ", ")
end

--- Read one frontmatter field off a capture record, tolerating every shape
--- the core can hand back: a promoted `NoteRecord` attribute, the `extra`
--- bag (arbitrary spec-07 fields), or the raw `frontmatter` map from
--- `note.get`.
---@param record table|nil
---@param key string
function M.field_value(record, key)
  if type(record) ~= "table" then
    return nil
  end
  if type(record.frontmatter) == "table" and record.frontmatter[key] ~= nil then
    return record.frontmatter[key]
  end
  if record[key] ~= nil and key ~= "path" then
    return record[key]
  end
  if type(record.extra) == "table" and record.extra[key] ~= nil then
    return record.extra[key]
  end
  return nil
end

--- Render a frontmatter value for the one-line metadata summary.
function M.display_value(value)
  if value == nil then
    return nil
  end
  if type(value) == "boolean" then
    return value and "true" or "false"
  end
  if type(value) == "table" then
    return join(value, ", ")
  end
  return tostring(value)
end

--- `[P]` / `[D]` / … honouring `ui.icons.*` overrides when configured.
---@param kind string|nil
---@param cfg table|nil
function M.type_marker(kind, cfg)
  local key = tostring(kind or ""):lower()
  key = M.TYPE_ALIASES[key] or key
  local icons = cfg and cfg.ui and cfg.ui.icons or {}
  local icon = icons[key]
  if type(icon) == "string" and icon ~= "" then
    return icon
  end
  return "[" .. (M.TYPE_LETTERS[key] or "?") .. "]"
end

--- Highlight group for a score.
---
--- ⚠ `ui.highlights.score_high|score_medium|score_low` are HIGHLIGHT-GROUP
--- NAME strings and are unrelated to the numeric `ui.organize.score_thresholds`
--- (ruling R3). Both are read here, and neither is the other's spelling.
---@param thresholds table|nil { high = number, medium = number }
function M.score_hl(score, highlights, thresholds)
  highlights = highlights or {}
  thresholds = thresholds or M.SCORE_THRESHOLDS
  local high = tonumber(thresholds.high) or M.SCORE_THRESHOLDS.high
  local medium = tonumber(thresholds.medium) or M.SCORE_THRESHOLDS.medium
  if type(score) ~= "number" then
    return highlights.score_low or "ParaOrganizeScoreLow"
  end
  if score >= high then
    return highlights.score_high or "ParaOrganizeScoreHigh"
  end
  if score >= medium then
    return highlights.score_medium or "ParaOrganizeScoreMedium"
  end
  return highlights.score_low or "ParaOrganizeScoreLow"
end

-- --- the rendered-buffer builder -------------------------------------------

local Rendered = {}
Rendered.__index = Rendered

--- @return table builder with `lines`, `marks` (extmark specs) and `map`
--- (1-based line → item, used for `<CR>` dispatch and cursor sync).
function M.new()
  return setmetatable({ lines = {}, marks = {}, map = {} }, Rendered)
end

function Rendered:line(text, hl_group, item)
  table.insert(self.lines, text or "")
  local lnum = #self.lines
  if hl_group then
    table.insert(self.marks, { line = lnum, col = 0, end_col = -1, hl = hl_group })
  end
  if item then
    self.map[lnum] = item
  end
  return lnum
end

function Rendered:mark(lnum, col, end_col, hl_group)
  table.insert(self.marks, { line = lnum, col = col, end_col = end_col, hl = hl_group })
end

function Rendered:header(text, hl_group)
  local lnum = self:line(text, hl_group)
  self:line("")
  return lnum
end

--- First line whose mapped item satisfies `pred` (default: any item).
function Rendered:line_of(pred)
  for lnum = 1, #self.lines do
    local item = self.map[lnum]
    if item and (pred == nil or pred(item)) then
      return lnum
    end
  end
  return nil
end

-- --- right-pane views (spec 03 §3) -----------------------------------------

local function hl_of(cfg)
  return (cfg and cfg.ui and cfg.ui.highlights) or {}
end

--- `ui.organize.*` — the right pane's own closed record (spec 15 §6/§7).
--- `ui.display.*` is DELETED as a section (ruling R1); `config.MOVED_KEYS`
--- names the replacement for each of its keys.
local function organize_of(cfg)
  return (cfg and cfg.ui and cfg.ui.organize) or {}
end

--- `ui.organize.render_row` — the per-row override (spec 15 §6).
---
--- Returns `nil` to fall back to the built-in row for THAT row. The same five
--- safety rules and the same 3-failure disable as `ui.capture.render`: a
--- broken user function may not spam its way through a 200-capture backlog,
--- and row DISPATCH stays keyed on `item.kind`, never on rendered text, so a
--- custom row can never break `<CR>`.
---@return string|nil text, string|nil hl
local function custom_row(cfg, row_ctx)
  local fn = organize_of(cfg).render_row
  if type(fn) ~= "function" then
    return nil
  end
  local fields = require("para-organize.ui.fields")
  if fields._session.row_disabled then
    return nil
  end
  local ok, result = pcall(fn, row_ctx)
  local text, hl
  if ok then
    if result == nil then
      return nil
    end
    if type(result) == "string" then
      text = result
    elseif type(result) == "table" and type(result[1]) == "string" then
      text, hl = result[1], type(result[2]) == "string" and result[2] or nil
    end
    if text and text:find("\n", 1, true) then
      text = nil
    end
  end
  if text then
    return text, hl
  end
  fields._session.row_failures = fields._session.row_failures + 1
  if fields._session.row_failures == 1 then
    fields.notify(
      ("ui.organize.render_row failed (%s) — using the built-in row"):format(ok and "invalid return value" or tostring(result)),
      vim.log.levels.WARN
    )
  end
  if fields._session.row_failures >= 3 then
    fields._session.row_disabled = true
  end
  return nil
end

--- State 0: loading. Rendered while an RPC round-trip is in flight so the
--- pane is never blank and never blocks (spec 09 §2: UI never blocks >50 ms).
function M.loading(state, cfg)
  local out = M.new()
  local hl = hl_of(cfg)
  out:header("Organize", hl.header)
  out:line("  Loading… " .. (state and state.message or ""), hl.reason)
  return out
end

--- State: empty session. Two distinct notices — nothing matched the filters
--- at all (spec 03 §2), versus the session running to completion with counts
--- (spec 03 §6).
function M.empty(state, cfg)
  state = state or {}
  local out = M.new()
  local hl = hl_of(cfg)
  if #(state.captures or {}) == 0 then
    out:header("No captures found matching filters", hl.header)
    out:line("  Adjust the filters and run :ParaOrganize start again.", hl.reason)
    return out
  end
  out:header("Session complete", hl.header)
  out:line(("  Processed: %d"):format(M.count(state.processed)))
  out:line(("  Skipped:   %d"):format(M.count(state.skipped)))
  out:line(("  Captures:  %d"):format(#(state.captures or {})))
  out:line("")
  out:line("  <Esc> closes the session.", hl.reason)
  return out
end

--- Is a decoded-JSON field actually there?
---
--- The wire's `null` decodes to `vim.NIL` — a userdata that is TRUTHY in Lua —
--- so `if suggestion.route then` rendered "→ route: vim.NIL" on every entry
--- whose route was null (which is every entry the SQ-1 archive-only default
--- produces, and most scored ones). One presence test for every optional
--- string the core may null out.
function M.present(value)
  return value ~= nil and value ~= vim.NIL and value ~= ""
end

--- Render order of the browse-tree roots in state 1 (ruling D).
M.ROOT_ORDER = { project = 1, area = 2, resource = 3, archive = 4 }

--- The browse tree that shares state 1 with the suggestions list.
---
--- Spec 01 describes the right pane as "ranked destination suggestions +
--- browsable PARA folder tree, scores visible", which is the architect's
--- resolution (ruling D, ARCHITECTURE.md) of spec 03 §3's self-contradiction
--- between "`<CR>` on a suggestion accepts it" and "`[P]/[A]/[R]/[D]`
--- descend". Both live here: the suggestions accept, the tree descends, and
--- `<CR>` tells them apart by the `kind` on the mapped item — never by the
--- rendered marker text.
---
--- Roots carry no `index`, so cursoring onto one never disturbs which
--- SUGGESTION is selected (`<A-j>`/`<A-k>` and `<Plug>(ParaOrganizeAccept)`
--- keep working from anywhere in the pane).
function M.browse_tree(state, cfg, out)
  local roots = (state or {}).roots or {}
  if #roots == 0 then
    return out
  end
  local hl = hl_of(cfg)
  out:line("")
  out:line("Browse", hl.header)
  for _, root in ipairs(roots) do
    out:line(
      ("%s %s/"):format(M.type_marker(root.type or "dir", cfg), root.name or root.path or "?"),
      nil,
      { kind = "root", item = root, path = root.path }
    )
  end
  return out
end

--- State 1: the suggestions list, followed by the browse tree.
function M.suggestions(state, cfg)
  state = state or {}
  local out = M.new()
  local hl = hl_of(cfg)
  local organize = organize_of(cfg)
  local sort_mode = state.sort or "intelligent"
  out:header(("Suggestions — sort: %s"):format(M.SORT_LABELS[sort_mode] or tostring(sort_mode)), hl.header)

  local suggestions = state.suggestions or {}
  if #suggestions == 0 then
    out:line("  (no suggestions for this capture — press r to regenerate)", hl.reason)
    return M.browse_tree(state, cfg, out)
  end

  local selected = state.selected or 1
  -- `0` = all (spec 15 §6): 04 §2 requires every fired signal to contribute a
  -- reason, and six of them push the next suggestion off a short pane.
  local max_reasons = tonumber(organize.max_reasons) or 3
  for index, suggestion in ipairs(suggestions) do
    local text = ("%s %s"):format(M.type_marker(suggestion.type, cfg), suggestion.name or suggestion.path or "?")
    if M.present(suggestion.route) then
      -- spec 11 §1: route-sourced entries outrank scored ones and render
      -- distinctly so Matt can tell a rule from a score.
      text = text .. ("  → route: %s"):format(suggestion.route)
    end
    local score_col
    if organize.show_scores ~= false and type(suggestion.score) == "number" then
      local score_text = ("%.2f"):format(suggestion.score)
      score_col = { from = #text + 2, text = score_text }
      text = text .. "  " .. score_text
    end
    local row_hl = index == selected and (hl.selected or "ParaOrganizeSelected") or nil
    local override, override_hl = custom_row(cfg, {
      view = "suggestions",
      index = index,
      selected = index == selected,
      item = suggestion,
      marker = M.type_marker(suggestion.type, cfg),
      config = cfg,
      width = state.width,
    })
    if override then
      text, score_col, row_hl = override, nil, override_hl or row_hl
    end
    local lnum = out:line(
      text,
      row_hl,
      { kind = "suggestion", index = index, item = suggestion, path = suggestion.path }
    )
    if score_col then
      out:mark(
        lnum,
        score_col.from,
        score_col.from + #score_col.text,
        M.score_hl(suggestion.score, hl, organize.score_thresholds)
      )
    end
    if organize.show_reasons ~= false then
      local shown = 0
      for _, reason in ipairs(suggestion.reasons or {}) do
        if max_reasons > 0 and shown >= max_reasons then
          break
        end
        shown = shown + 1
        out:line("    " .. tostring(reason), hl.reason or "Comment")
      end
    end
    if state.preview and index == selected then
      local description = suggestion.description
      if M.present(description) then
        out:line("    ┊ " .. tostring(description), hl.reason or "Comment")
      else
        out:line("    ┊ (no description)", hl.reason or "Comment")
      end
    end
  end
  return M.browse_tree(state, cfg, out)
end

--- State 2: directory browse. Entries come from the core (`folder.children`,
--- or derived from `search.query` records) — never from a filesystem read.
function M.browse(state, cfg)
  state = state or {}
  local out = M.new()
  local hl = hl_of(cfg)
  local browse = state.browse or {}
  local sort_mode = state.sort or "alphabetical"
  out:header(
    ("Browse: %s — sort: %s"):format(browse.label or browse.path or "/", M.SORT_LABELS[sort_mode] or tostring(sort_mode)),
    hl.header
  )
  local entries = browse.entries or {}
  if #entries == 0 then
    out:line("  (empty folder)", hl.reason)
    return out
  end
  local selected = state.selected or 1
  for index, entry in ipairs(entries) do
    local kind = entry.kind == "dir" and "dir" or "file"
    local label = entry.display or entry.name or entry.path or "?"
    local marker = M.type_marker(kind, cfg)
    local text = ("%s %s"):format(marker, label)
    local row_hl = index == selected and (hl.selected or "ParaOrganizeSelected") or nil
    local override, override_hl = custom_row(cfg, {
      view = "browse",
      index = index,
      selected = index == selected,
      item = entry,
      marker = marker,
      config = cfg,
      width = state.width,
    })
    if override then
      text, row_hl = override, override_hl or row_hl
    end
    out:line(
      text,
      row_hl,
      -- ⚠ Row DISPATCH stays keyed on `kind`, never on the rendered text, so
      -- a custom row can never break `<CR>` (spec 15 §6).
      { kind = kind, index = index, item = entry, path = entry.path }
    )
    if state.preview and index == selected and entry.description then
      out:line("    ┊ " .. tostring(entry.description), hl.reason or "Comment")
    end
  end
  return out
end

--- State 3: search results. Notes always render `[F]` so `<CR>`'s
--- prefix dispatch (spec 03 §3) starts a merge instead of descending.
function M.search(state, cfg)
  state = state or {}
  local out = M.new()
  local hl = hl_of(cfg)
  local search = state.search or {}
  local results = search.results or {}
  local scope = search.scope and (" in " .. search.scope) or ""
  out:header(("Search: %s%s (%d)"):format(search.query or "", scope, #results), hl.header)
  if #results == 0 then
    out:line("  (no matches)", hl.reason)
    return out
  end
  local selected = state.selected or 1
  for index, record in ipairs(results) do
    local label = record.title or record.filename or record.path or "?"
    local marker = M.type_marker("file", cfg)
    local text = ("%s %s"):format(marker, label)
    if record.folder and record.folder ~= "" then
      text = text .. ("   (%s)"):format(record.folder)
    end
    local row_hl = index == selected and (hl.selected or "ParaOrganizeSelected") or nil
    local override, override_hl = custom_row(cfg, {
      view = "search",
      index = index,
      selected = index == selected,
      item = record,
      marker = marker,
      config = cfg,
      width = state.width,
    })
    if override then
      text, row_hl = override, override_hl or row_hl
    end
    out:line(text, row_hl, { kind = "file", index = index, item = record, path = record.path })
  end
  return out
end

--- State 4: merge editor. The buffer holds ONLY mergeable content — the
--- instructions live in `M.merge_hint()` and are drawn as virtual text
--- (spec 03 §5: never as buffer lines that could be saved).
function M.merge(state, cfg)
  state = state or {}
  local out = M.new()
  local merge = state.merge or {}
  local content = merge.content or ""
  for _, line in ipairs(vim.split(content, "\n", { plain = true })) do
    out:line(line)
  end
  if #out.lines == 0 then
    out:line("")
  end
  local _ = cfg
  return out
end

function M.merge_hint(state, cfg)
  local merge = (state or {}).merge or {}
  local keys = (cfg and cfg.keymaps and cfg.keymaps.buffer) or {}
  return ("MERGE → %s   %s complete · %s cancel"):format(
    merge.target or "?",
    keys.merge_complete or "<leader>mc",
    keys.merge_cancel or "<leader>mx"
  )
end

--- The VIEW REGISTRY (spec 15 §6, ruling R37).
---
--- The per-view renderers used to be a dispatch `if`-chain in `right_pane`.
--- Three documents (15, 16's `mru`, 17's `history`) need a new view, and only
--- ONE of them may perform the refactor — so doc 15 lands the registry as a
--- standalone change and 16/17 each add a single table entry with no edit to
--- the dispatcher. This is 14 §5's declared seam.
---@type table<string, fun(state: table, cfg: table): table>
M.VIEWS = {
  loading = function(state, cfg)
    return M.loading(state, cfg)
  end,
  empty = function(state, cfg)
    return M.empty(state, cfg)
  end,
  done = function(state, cfg)
    return M.empty(state, cfg)
  end,
  browse = function(state, cfg)
    return M.browse(state, cfg)
  end,
  search = function(state, cfg)
    return M.search(state, cfg)
  end,
  merge = function(state, cfg)
    return M.merge(state, cfg)
  end,
  suggestions = function(state, cfg)
    return M.suggestions(state, cfg)
  end,
}

--- The fallback view for an unregistered name — spec 03 §3's state 1.
M.DEFAULT_VIEW = "suggestions"

--- Dispatch to the registered renderer for `state.view`.
function M.right_pane(state, cfg)
  state = state or {}
  local fn = M.VIEWS[state.view or M.DEFAULT_VIEW] or M.VIEWS[M.DEFAULT_VIEW]
  return fn(state, cfg)
end

-- --- left pane (the real capture buffer's card) ----------------------------

--- ⚠ `capture_header`, `metadata_summary`, `HEADER_KEYS` and
--- `format_timestamp` are GONE. Spec 15 replaces the fixed 03 §3 header list
--- with the configurable field policy of `para-organize.ui.fields`:
---
---   * which keys show, and in what order   -> `ui.capture.fields.*`
---   * how one value renders                 -> `ui.capture.formatters.*`
---   * the whole card                        -> `ui.capture.render`
---
--- `format_timestamp` in particular REQUIRED a time component, so the
--- `calendar` formatter that now ships for `created_date`/`last_edited_date`
--- (both written date-only by the core) would silently never have applied to
--- either. `fields.parse_timestamp` is the tolerant, timezone-correct,
--- date-only-accepting replacement (spec 15 §4).

-- --- help overlay ----------------------------------------------------------

--- Help is GENERATED from the live keymap table (spec 03 §2: "must be
--- generated from the actual keymap table, never hand-maintained").
---@param entries table[] {lhs, desc, panes, group}
function M.help(entries, cfg)
  local out = M.new()
  local hl = hl_of(cfg)
  out:header("para-organize — active keymaps", hl.header)
  local groups, order = {}, {}
  for _, entry in ipairs(entries or {}) do
    local group = entry.group or "Organize"
    if not groups[group] then
      groups[group] = {}
      table.insert(order, group)
    end
    table.insert(groups[group], entry)
  end
  for _, group in ipairs(order) do
    out:line(group, hl.header)
    for _, entry in ipairs(groups[group]) do
      local panes = table.concat(entry.panes or {}, "+")
      out:line(("  %-14s %-28s %s"):format(entry.lhs or "", entry.desc or "", panes))
    end
    out:line("")
  end
  out:line("  q / <Esc> / ? closes this help", hl.reason)
  return out
end

return M
