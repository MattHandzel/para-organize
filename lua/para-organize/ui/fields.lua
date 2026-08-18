--- para-organize.ui.fields — the capture pane's FIELD POLICY and FORMATTERS
--- (spec 15 §2 + §4), plus the built-in card builder (spec 15 §3/§5).
---
--- PURE. Nothing here touches a window, a buffer, the filesystem or the core.
--- It turns `note.get`'s frontmatter dict plus the merged UI config into a
--- list of virtual-line chunks; `ui.lua` is the only thing that hands those to
--- `nvim_buf_set_extmark`. There is no code path from this module to
--- `nvim_buf_set_lines` or to disk (spec 15 §5 rule 4), which is what makes
--- the spec 10 §4 exception safe: the left pane is the REAL note, and
--- everything drawn around it is virtual text that `:w` cannot see.
---
--- The one impurity is deliberate and injectable: `M.notify` (a broken user
--- formatter has to say so out loud, exactly once).
local M = {}

local uv = vim.uv or vim.loop

---------------------------------------------------------------------------
-- shipped defaults (spec 15 §7, ruling R15)
---------------------------------------------------------------------------

--- The measured list of doc/FEEDBACK-EVIDENCE-2026-08-16.md §2, in the order
--- ruling R15 fixes. DEFAULTS, never law — `ui.capture.fields.pinned`
--- replaces the whole list (never index-merges it, see `apply_list_leaves`).
M.DEFAULT_PINNED = { "title", "summary", "timestamp", "context", "tags", "sources" }

--- The duplicate-identity and bookkeeping keys: four spellings of one
--- timestamp (`id`, `aliases`, `capture_id`, `created_date`), a 5-key nested
--- `location` map that costs five rendered lines, and `metadata` (R15).
M.DEFAULT_HIDDEN = {
  "location",
  "processing_status",
  "created_date",
  "last_edited_date",
  "id",
  "aliases",
  "capture_id",
  "modalities",
  "metadata",
}

--- Keys whose default formatter is `calendar` (spec 15 §4).
M.DEFAULT_FORMATTERS = {
  timestamp = "calendar",
  created_date = "calendar",
  last_edited_date = "calendar",
  tags = "tags",
}

--- Labels are padded to the widest label IN THIS CARD, capped here. A wider
--- label is not truncated; it pushes its own value one column past the cap
--- for that row alone (spec 15 §2).
M.LABEL_CAP = 20

--- The three modes of the `cycle_fields` key, in cycle order.
M.MODES = { "compact", "full", "raw" }

---------------------------------------------------------------------------
-- session-scoped diagnostics
---------------------------------------------------------------------------

--- Reset by `ui.mount` — "once per session" in spec 15 §4/§5 means once per
--- mounted session, not once per Neovim process.
M._session = {
  formatter_warned = {}, -- key -> true (one WARN per key per session)
  formatter_errors = {}, -- key -> last error message
  budget_warned = false,
  render_failures = 0,
  render_disabled = false,
  row_failures = 0,
  row_disabled = false,
}

function M.reset_session()
  M._session = {
    formatter_warned = {},
    formatter_errors = {},
    budget_warned = false,
    render_failures = 0,
    render_disabled = false,
    row_failures = 0,
    row_disabled = false,
  }
  return M._session
end

--- Injectable so tests can count notifications without a UI (spec 15 §10.7
--- asserts an exact notify count).
function M.notify(msg, level)
  vim.notify("para-organize: " .. msg, level or vim.log.levels.WARN)
end

--- `:ParaOrganize debug` rows for this document's failure modes (spec 15 §4
--- and §5 both require the failure to be REPORTED, not only survived).
function M.diagnostics()
  local s = M._session
  local out = {}
  if s.render_disabled then
    out[#out + 1] = ("capture_render = \"override-disabled (%d errors)\""):format(s.render_failures)
  elseif s.render_failures > 0 then
    out[#out + 1] = ("capture_render = \"%d error(s)\""):format(s.render_failures)
  end
  if s.row_disabled then
    out[#out + 1] = ("organize_render_row = \"override-disabled (%d errors)\""):format(s.row_failures)
  elseif s.row_failures > 0 then
    out[#out + 1] = ("organize_render_row = \"%d error(s)\""):format(s.row_failures)
  end
  local keys = {}
  for key in pairs(s.formatter_errors) do
    keys[#keys + 1] = key
  end
  table.sort(keys)
  for _, key in ipairs(keys) do
    out[#out + 1] = ("formatter[%s] = %s"):format(key, s.formatter_errors[key])
  end
  if s.budget_warned then
    out[#out + 1] = "capture_render_budget = \"exceeded (see ui.capture.render_budget_ms)\""
  end
  return out
end

---------------------------------------------------------------------------
-- list leaves: `pinned` / `hidden` are REPLACED, never index-merged
---------------------------------------------------------------------------

--- ⚠ spec 15 §2: `vim.tbl_deep_extend` merges array-like tables BY INDEX, so
--- `pinned = { "tags" }` over the six-entry default would yield six rows with
--- `tags` in it twice — the user asked for one row and got the default back.
--- Both config layers (`ui.setup` and `config.setup`) run this after their
--- deep-extend, so a list leaf is a LEAF everywhere.
M.LIST_LEAVES = {
  { "ui", "capture", "fields", "pinned" },
  { "ui", "capture", "fields", "hidden" },
}

local function dig(tbl, path, upto)
  local node = tbl
  for i = 1, upto do
    if type(node) ~= "table" then
      return nil
    end
    node = node[path[i]]
  end
  return node
end

--- Copy every list leaf present in `user` over `merged`, wholesale.
---@param merged table the deep-extended result (mutated)
---@param user table|nil the table the user actually passed
---@return table merged
function M.apply_list_leaves(merged, user)
  if type(merged) ~= "table" or type(user) ~= "table" then
    return merged
  end
  for _, path in ipairs(M.LIST_LEAVES) do
    local value = dig(user, path, #path)
    if type(value) == "table" then
      local parent = dig(merged, path, #path - 1)
      if type(parent) == "table" then
        parent[path[#path]] = vim.deepcopy(value)
      end
    end
  end
  return merged
end

---------------------------------------------------------------------------
-- value helpers
---------------------------------------------------------------------------

--- JSON `null` decodes to `vim.NIL`, which is TRUTHY in Lua.
local function present(value)
  return value ~= nil and value ~= vim.NIL
end

M.present = present

local function is_list(value)
  if type(value) ~= "table" then
    return false
  end
  if next(value) == nil then
    return true
  end
  if vim.islist then
    return vim.islist(value)
  end
  return vim.tbl_islist(value)
end

local function sorted_keys(tbl)
  local keys = {}
  for key in pairs(tbl or {}) do
    keys[#keys + 1] = tostring(key)
  end
  table.sort(keys)
  return keys
end

---------------------------------------------------------------------------
-- the tolerant timestamp matcher (spec 15 §4)
---------------------------------------------------------------------------

--- Seconds the local zone is ahead of UTC at `epoch`.
local function local_offset(epoch)
  local utc = os.date("!*t", epoch)
  utc.isdst = false
  return os.difftime(epoch, os.time(utc))
end

--- Parse an ISO-ish timestamp.
---
--- ⚠ DATE-ONLY INPUT PARSES. `created_date`/`last_edited_date` are written
--- date-only by the core, and the old `render.format_timestamp` REQUIRED a
--- time component — so the `calendar` formatter that ships for those two keys
--- would silently never have applied to either (spec 15 §4).
---
---@param value any
---@return integer|nil epoch, boolean date_only
function M.parse_timestamp(value)
  if type(value) ~= "string" or value == "" then
    return nil, false
  end
  local y, mo, d = value:match("^(%d%d%d%d)-(%d%d)-(%d%d)")
  if not y then
    return nil, false
  end
  local rest = value:sub(11)
  local h, mi, s = rest:match("^[T ](%d%d):(%d%d):?(%d*)")
  local date_only = h == nil
  local fields = {
    year = tonumber(y),
    month = tonumber(mo),
    day = tonumber(d),
    hour = tonumber(h) or 0,
    min = tonumber(mi) or 0,
    sec = tonumber(s) or 0,
    isdst = false,
  }
  local ok, epoch = pcall(os.time, fields)
  if not ok or type(epoch) ~= "number" then
    return nil, false
  end

  -- TIMEZONE RULE (spec 15 §4): a value carrying `Z` or a numeric UTC offset
  -- is converted to LOCAL before bucketing and formatting; a naive value is
  -- treated as already local and is NEVER shifted. Both halves matter — the
  -- first stops a `…T02:00Z` capture being bucketed as its UTC day, the
  -- second stops every naive local timestamp in the vault sliding by the
  -- offset.
  if not date_only then
    local zone = rest:match("[T ]%d%d:%d%d:?%d*%.?%d*(.*)$") or ""
    local offset_seconds
    if zone:match("^Z") then
      offset_seconds = 0
    else
      local sign, oh, om = zone:match("^([%+%-])(%d%d):?(%d%d)")
      if sign then
        offset_seconds = (tonumber(oh) * 3600 + tonumber(om) * 60) * (sign == "-" and -1 or 1)
      end
    end
    if offset_seconds then
      epoch = epoch + local_offset(epoch) - offset_seconds
    end
  end
  return epoch, date_only
end

--- Whole calendar days from `now`'s day to `epoch`'s day (local time).
local function day_delta(epoch, now)
  local a = os.date("*t", epoch)
  local b = os.date("*t", now)
  local a0 = os.time({ year = a.year, month = a.month, day = a.day, hour = 12, isdst = false })
  local b0 = os.time({ year = b.year, month = b.month, day = b.day, hour = 12, isdst = false })
  return math.floor(os.difftime(a0, b0) / 86400 + 0.5)
end

M.DEFAULT_DATE_FORMAT = "%a %d %b %Y"
M.DEFAULT_TIME_FORMAT = "%I:%M %p"
M.DEFAULT_DATETIME_FORMAT = "%b %d, %I:%M %p"

local function clock(epoch, time_format)
  local text = os.date(time_format or M.DEFAULT_TIME_FORMAT, epoch)
  if (time_format or M.DEFAULT_TIME_FORMAT) == M.DEFAULT_TIME_FORMAT then
    -- `%I` zero-pads; Matt's example reads `7:45 PM`, not `07:45 PM`.
    text = tostring(text):gsub("^0", "")
  end
  return text
end

local function days_phrase(delta)
  if delta == 0 then
    return "today"
  end
  if delta < 0 then
    local n = -delta
    return ("%d day%s ago"):format(n, n == 1 and "" or "s")
  end
  return ("in %d day%s"):format(delta, delta == 1 and "" or "s")
end

---------------------------------------------------------------------------
-- named formatters (spec 15 §4)
---------------------------------------------------------------------------

local NAMED = {}

function NAMED.raw(value)
  if not present(value) then
    return ""
  end
  if type(value) == "boolean" then
    return value and "true" or "false"
  end
  if type(value) == "table" then
    if is_list(value) then
      local out = {}
      for _, item in ipairs(value) do
        if present(item) then
          out[#out + 1] = type(item) == "table" and NAMED.raw(item) or tostring(item)
        end
      end
      return table.concat(out, ", ")
    end
    local parts = {}
    for _, key in ipairs(sorted_keys(value)) do
      parts[#parts + 1] = ("%s=%s"):format(key, NAMED.raw(value[key]))
    end
    return table.concat(parts, " ")
  end
  return tostring(value)
end

function NAMED.list(value, opts)
  opts = opts or {}
  local sep = type(opts.sep) == "string" and opts.sep or ", "
  local max = tonumber(opts.max) or 0
  local items = {}
  for _, item in ipairs(is_list(value) and value or { value }) do
    if present(item) and item ~= "" then
      items[#items + 1] = type(item) == "table" and NAMED.raw(item) or tostring(item)
    end
  end
  if max > 0 and #items > max then
    local head = {}
    for i = 1, max do
      head[i] = items[i]
    end
    return table.concat(head, sep) .. sep .. ("+%d"):format(#items - max)
  end
  return table.concat(items, sep)
end

function NAMED.tags(value, opts)
  opts = opts or {}
  local prefix = type(opts.prefix) == "string" and opts.prefix or "#"
  local items = {}
  for _, item in ipairs(is_list(value) and value or { value }) do
    if present(item) and item ~= "" then
      items[#items + 1] = prefix .. tostring(item)
    end
  end
  return table.concat(items, " ")
end

function NAMED.link(value, opts)
  opts = opts or {}
  local text = NAMED.raw(value)
  text = text:gsub("%.md$", "")
  if opts.basename then
    text = text:match("([^/]+)$") or text
  end
  return text
end

function NAMED.boolean(value, opts)
  opts = opts or {}
  if not present(value) then
    return type(opts.absent) == "string" and opts.absent or "—"
  end
  if value == false or value == "false" or value == 0 then
    return type(opts.no) == "string" and opts.no or "✗"
  end
  return type(opts.yes) == "string" and opts.yes or "✓"
end

function NAMED.number(value, opts)
  opts = opts or {}
  local n = tonumber(value)
  if n == nil then
    return NAMED.raw(value)
  end
  local precision = tonumber(opts.precision)
  if precision then
    return ("%." .. math.floor(precision) .. "f"):format(n)
  end
  if n == math.floor(n) then
    return ("%d"):format(n)
  end
  return tostring(n)
end

function NAMED.datetime(value, opts)
  opts = opts or {}
  local epoch = M.parse_timestamp(value)
  if not epoch then
    -- The core keeps timestamps as opaque strings: never drop one.
    return NAMED.raw(value)
  end
  return os.date(type(opts.format) == "string" and opts.format or M.DEFAULT_DATETIME_FORMAT, epoch)
end

function NAMED.relative(value, opts, ctx)
  opts = opts or {}
  local epoch = M.parse_timestamp(value)
  if not epoch then
    return NAMED.raw(value)
  end
  local now = (ctx and ctx.now) or os.time()
  local diff = os.difftime(epoch, now)
  local precision = opts.precision or "minute"
  local abs = math.abs(diff)
  local unit, size
  if precision == "day" or abs >= 86400 then
    unit, size = "day", 86400
  elseif precision == "hour" or abs >= 3600 then
    unit, size = "hour", 3600
  else
    unit, size = "minute", 60
  end
  local n = math.floor(abs / size)
  if n == 0 then
    return "just now"
  end
  local plural = n == 1 and "" or "s"
  if diff < 0 then
    return ("%d %s%s ago"):format(n, unit, plural)
  end
  return ("in %d %s%s"):format(n, unit, plural)
end

--- Matt: *"timestamp could be a calendar and you can parse it so its easier
--- to read"*. Buckets are CALENDAR-DAY comparisons in local time (§4).
function NAMED.calendar(value, opts, ctx)
  opts = opts or {}
  local epoch, date_only = M.parse_timestamp(value)
  if not epoch then
    return NAMED.raw(value)
  end
  local now = (ctx and ctx.now) or os.time()
  local delta = day_delta(epoch, now)
  -- ⚠ NOT `date_only and nil or clock(…)`: in Lua the `and nil` collapses and
  -- the `or` branch ALWAYS runs, so a date-only value would render the
  -- fabricated `· 12:00 AM` that spec 15 §4's table forbids by name.
  local time_half
  if not date_only then
    time_half = clock(epoch, opts.time_format)
  end

  local head
  if delta == 0 then
    head = "Today"
  elseif delta == -1 then
    head = "Yesterday"
  elseif delta == 1 then
    head = "Tomorrow"
  elseif delta >= -6 and delta <= 6 then
    head = os.date("%a", epoch)
  else
    head = os.date(type(opts.date_format) == "string" and opts.date_format or M.DEFAULT_DATE_FORMAT, epoch)
  end

  local parts = { head }
  if time_half then
    parts[#parts + 1] = time_half
  end
  if delta ~= 0 and delta ~= -1 and delta ~= 1 and delta >= -6 and delta <= 6 then
    parts[#parts + 1] = days_phrase(delta)
  end
  return table.concat(parts, " · ")
end

M.NAMED = NAMED

---------------------------------------------------------------------------
-- the formatter stack
---------------------------------------------------------------------------

local TYPE_FORMATTER = { list = "list", boolean = "boolean", number = "number", enum = "raw", string = "raw" }

--- Which formatter spec applies to `key`, per §4's precedence: an explicit
--- `ui.capture.formatters[key]`, then `["*"]`, then the core-declared
--- `meta.fields[].type`, then `raw`.
function M.formatter_spec(key, capture_cfg, meta_fields)
  local formatters = (capture_cfg or {}).formatters or {}
  if formatters[key] ~= nil then
    return formatters[key]
  end
  for _, field in ipairs(meta_fields or {}) do
    if field.key == key and TYPE_FORMATTER[field.type] then
      return TYPE_FORMATTER[field.type]
    end
  end
  if formatters["*"] ~= nil then
    return formatters["*"]
  end
  return "raw"
end

local function chunks_valid(value)
  if type(value) == "string" then
    return not value:find("\n", 1, true)
  end
  if type(value) ~= "table" then
    return false
  end
  if type(value[1]) == "string" and (value[2] == nil or type(value[2]) == "string") then
    return not value[1]:find("\n", 1, true)
  end
  for _, chunk in ipairs(value) do
    if type(chunk) ~= "table" or type(chunk[1]) ~= "string" then
      return false
    end
    if chunk[1]:find("\n", 1, true) then
      return false
    end
  end
  return #value > 0
end

local function chunks_text(value)
  if type(value) == "string" then
    return value
  end
  if type(value[1]) == "string" then
    return value[1]
  end
  local out = {}
  for _, chunk in ipairs(value) do
    out[#out + 1] = chunk[1]
  end
  return table.concat(out, "")
end

--- A formatter that ERRORS must never break the pane (spec 15 §4).
---
--- Every call is `pcall`ed. On an error, or on an invalid return (not a
--- string / chunk / chunk list, or any text containing `\n` — Neovim rejects
--- newlines in virtual text) we fall back to `raw` for that key, emit exactly
--- ONE WARN per (key, session) and record the failure for
--- `:ParaOrganize debug`.
---@return string text
function M.format(key, value, ctx)
  ctx = ctx or {}
  local spec = ctx.spec
  if spec == nil then
    spec = M.formatter_spec(key, ctx.capture_config, ctx.meta_fields)
  end

  local name, opts
  if type(spec) == "string" then
    name, opts = spec, {}
  elseif type(spec) == "table" then
    name, opts = spec.name or "raw", spec
  elseif type(spec) == "function" then
    local ok, result = pcall(spec, value, {
      key = key,
      record = ctx.record,
      frontmatter = ctx.frontmatter,
      mode = ctx.mode,
      width = ctx.width,
      config = ctx.config,
    })
    if ok and chunks_valid(result) then
      return chunks_text(result)
    end
    M.formatter_failed(key, ok and "invalid return value (not a string/chunk list, or it contains a newline)" or tostring(result))
    return NAMED.raw(value)
  else
    name, opts = "raw", {}
  end

  local fn = NAMED[name]
  if not fn then
    M.formatter_failed(key, ("unknown formatter %q"):format(tostring(name)))
    return NAMED.raw(value)
  end
  local ok, result = pcall(fn, value, opts, ctx)
  if not ok or type(result) ~= "string" or result:find("\n", 1, true) then
    M.formatter_failed(key, ok and "invalid return value" or tostring(result))
    return NAMED.raw(value)
  end
  return result
end

function M.formatter_failed(key, message)
  M._session.formatter_errors[key] = tostring(message)
  if not M._session.formatter_warned[key] then
    M._session.formatter_warned[key] = true
    M.notify(("the formatter for `%s` failed (%s) — showing the raw value"):format(key, message), vim.log.levels.WARN)
  end
end

---------------------------------------------------------------------------
-- field policy (spec 15 §2)
---------------------------------------------------------------------------

--- Bucket every frontmatter key into PINNED / REST / HIDDEN.
---
--- * PINNED — `ui.capture.fields.pinned`, in the declared order, then (when
---   `pin_metadata_fields`) every spec-07 `metadata_fields` key in core
---   order, so 07's "state is visible immediately after each edit" cannot be
---   broken by a field falling into REST.
--- * HIDDEN — `ui.capture.fields.hidden`. A hidden key that never appears in
---   this vault is a NO-OP, not an error: `hidden` is a denylist of POSSIBLE
---   keys, and erroring would make configs vault-specific.
--- * REST — everything else present in the frontmatter, sorted
---   lexicographically so the card is deterministic across renders.
---@return table { pinned = string[], rest = string[], hidden = string[] }
function M.classify(frontmatter, capture_cfg, meta_fields)
  capture_cfg = capture_cfg or {}
  local field_cfg = capture_cfg.fields or {}
  local fm = type(frontmatter) == "table" and frontmatter or {}

  local hidden_set = {}
  for _, key in ipairs(field_cfg.hidden or {}) do
    hidden_set[key] = true
  end

  local pinned, seen = {}, {}
  for _, key in ipairs(field_cfg.pinned or {}) do
    if not hidden_set[key] and not seen[key] then
      seen[key] = true
      pinned[#pinned + 1] = key
    end
  end
  if field_cfg.pin_metadata_fields ~= false then
    for _, field in ipairs(meta_fields or {}) do
      local key = field.key
      if key and not hidden_set[key] and not seen[key] then
        seen[key] = true
        pinned[#pinned + 1] = key
      end
    end
  end

  local rest, hidden = {}, {}
  for key in pairs(fm) do
    key = tostring(key)
    if hidden_set[key] then
      hidden[#hidden + 1] = key
    elseif not seen[key] then
      rest[#rest + 1] = key
    end
  end
  table.sort(rest)
  table.sort(hidden)
  return { pinned = pinned, rest = rest, hidden = hidden }
end

---------------------------------------------------------------------------
-- the built-in card (spec 15 §2 layout rules)
---------------------------------------------------------------------------

local function hl_of(cfg)
  return (cfg and cfg.ui and cfg.ui.highlights) or {}
end

--- The label a key renders under. A label is DISPLAY-ONLY: it never changes
--- which key is pinned, hidden, sorted or formatted (spec 15 §2).
function M.label_of(key, capture_cfg)
  local labels = ((capture_cfg or {}).fields or {}).labels or {}
  local label = labels[key]
  if type(label) == "string" and label ~= "" then
    return label
  end
  return key
end

local function pad(label, width)
  local w = vim.fn.strdisplaywidth(label)
  if w >= width then
    return label
  end
  return label .. string.rep(" ", width - w)
end

--- `ctx` is exactly the `ui.capture.render` contract of spec 15 §5, so the
--- built-in card and a user override see the same world.
---@return table[] virt_lines — list of chunk lists
function M.card(ctx)
  local cfg = ctx.config or {}
  local capture_cfg = (cfg.ui or {}).capture or {}
  local hl = hl_of(cfg)
  local dim = hl.hint or "Comment"
  local head = hl.header or "Title"
  local lines = {}

  local function add(text, group)
    lines[#lines + 1] = { { text, group or "Normal" } }
  end

  -- In `split` layout there is no border to carry the mode, so the mode is
  -- the first line of the card instead (spec 15 §2).
  -- ...and in `raw` there is no card to carry it and the border is not
  -- enough, so the mode line is drawn in every layout.
  if ctx.mode ~= "compact" and ((cfg.ui or {}).layout == "split" or ctx.mode == "raw") then
    add((" -- %s --"):format(ctx.mode), dim)
  end

  if capture_cfg.show_position ~= false and (ctx.total or 0) > 0 then
    add((" Capture %d of %d"):format(ctx.index or 1, ctx.total or 0), head)
  end

  if ctx.mode == "raw" then
    -- `raw` draws no field card at all: the metadata IS the buffer text, and
    -- the frontmatter fold is open (spec 15 §2).
    return lines
  end

  -- ⚠ `nil` (the async `note.get` reply has not landed) and `{}` (fetched,
  -- empty) are DIFFERENT states. Claiming `(no frontmatter)` in the first one
  -- is a statement the client cannot make, and it would flash falsely once
  -- per capture advance across a 1,862-capture backlog (spec 15 §3).
  if ctx.frontmatter == nil then
    add(" (loading…)", dim)
    return lines
  end

  if ctx.parse_error == true then
    add(" ⚠ frontmatter unparseable — showing raw", hl.reason or "WarningMsg")
    return lines
  end

  local field_cfg = capture_cfg.fields or {}
  local fields = ctx.fields or M.classify(ctx.frontmatter, capture_cfg, ctx.meta_fields)
  local show_empty = field_cfg.show_empty_pinned == true

  local rows = {}
  local function row(key, group, suffix)
    local value = ctx.frontmatter[key]
    if not present(value) then
      if not show_empty then
        return
      end
      rows[#rows + 1] = { label = M.label_of(key, capture_cfg), text = "—", hl = group, suffix = suffix }
      return
    end
    rows[#rows + 1] = {
      label = M.label_of(key, capture_cfg),
      text = ctx.format(key, value),
      hl = group,
      suffix = suffix,
    }
  end

  local meta_keymap = {}
  for _, field in ipairs(ctx.meta_fields or {}) do
    if field.key and type(field.keymap) == "string" and field.keymap ~= "" then
      meta_keymap[field.key] = field.keymap
    end
  end

  for _, key in ipairs(fields.pinned) do
    row(key, nil, meta_keymap[key] and ("(%s)"):format(meta_keymap[key]) or nil)
  end

  if ctx.mode == "full" then
    for _, key in ipairs(fields.rest) do
      row(key, nil, meta_keymap[key] and ("(%s)"):format(meta_keymap[key]) or nil)
    end
    for _, key in ipairs(fields.hidden) do
      row(key, dim, nil)
    end
  end

  local width = 0
  for _, r in ipairs(rows) do
    width = math.max(width, math.min(vim.fn.strdisplaywidth(r.label), M.LABEL_CAP))
  end
  for _, r in ipairs(rows) do
    local chunks = { { " " .. pad(r.label, width) .. " ", r.hl or dim }, { r.text, r.hl or "Normal" } }
    if r.suffix then
      chunks[#chunks + 1] = { "   " .. r.suffix, dim }
    end
    lines[#lines + 1] = chunks
  end

  if next(ctx.frontmatter) == nil then
    add(" (no frontmatter)", dim)
    return lines
  end

  if ctx.mode == "compact" and #fields.rest > 0 then
    -- The hint names the LIVE binding (never a hardcoded `zi`), and is
    -- dropped entirely when the user unbound the key rather than pointing at
    -- a key that does nothing.
    local hint = ctx.cycle_key and (" · " .. ctx.cycle_key) or ""
    if field_cfg.show_rest_keys == false then
      add((" + %d more%s"):format(#fields.rest, hint), dim)
    else
      add((" + %d more: %s%s"):format(#fields.rest, table.concat(fields.rest, ", "), hint), dim)
    end
  end

  if ctx.dirty then
    add(" (edited — :w to refresh)", dim)
  end
  return lines
end

--- Enforce the §5 safety rules on ANY card (built-in or override) before it
--- reaches `nvim_buf_set_extmark`: shape, no newlines, `max_card_lines`.
---@return table[]|nil virt_lines, string|nil error
function M.sanitize(virt_lines, cfg, cycle_key)
  if type(virt_lines) ~= "table" or (next(virt_lines) ~= nil and not is_list(virt_lines)) then
    return nil, "the renderer must return a LIST of lines"
  end
  local capture_cfg = ((cfg or {}).ui or {}).capture or {}
  local max = tonumber(capture_cfg.max_card_lines) or 40
  local out = {}
  for _, line in ipairs(virt_lines) do
    local chunks
    if type(line) == "string" then
      chunks = { { line, "Normal" } }
    elseif type(line) == "table" and type(line[1]) == "string" then
      chunks = { { line[1], type(line[2]) == "string" and line[2] or "Normal" } }
    elseif type(line) == "table" then
      chunks = {}
      for _, chunk in ipairs(line) do
        if type(chunk) ~= "table" or type(chunk[1]) ~= "string" then
          return nil, "every chunk must be { text, hl_group }"
        end
        chunks[#chunks + 1] = { chunk[1], type(chunk[2]) == "string" and chunk[2] or "Normal" }
      end
    else
      return nil, "a card line must be a string or a chunk list"
    end
    for _, chunk in ipairs(chunks) do
      if chunk[1]:find("\n", 1, true) then
        return nil, "virtual text may not contain a newline"
      end
    end
    out[#out + 1] = chunks
  end
  if max > 0 and #out > max then
    local dim = hl_of(cfg).hint or "Comment"
    local dropped = #out - max
    local capped = {}
    for i = 1, max - 1 do
      capped[i] = out[i]
    end
    capped[max] = { { (" + %d more · %s"):format(dropped + 1, cycle_key or "zi"), dim } }
    return capped
  end
  return out
end

--- The card for `ctx`, honouring `ui.capture.render` with the §5 fallback:
--- `pcall`, one WARN on the first failure, and after THREE failures in one
--- session the override is disabled for the rest of it — a broken user
--- function may not spam its way through a 200-capture backlog.
function M.render_card(ctx)
  local cfg = ctx.config or {}
  local override = ((cfg.ui or {}).capture or {}).render
  local started = uv.hrtime()

  local lines
  if type(override) == "function" and not M._session.render_disabled then
    local ok, result = pcall(override, ctx)
    local sane, err
    if ok then
      sane, err = M.sanitize(result, cfg, ctx.cycle_key)
    else
      err = tostring(result)
    end
    if sane then
      lines = sane
    else
      M._session.render_failures = M._session.render_failures + 1
      if M._session.render_failures == 1 then
        M.notify(("ui.capture.render failed (%s) — using the built-in card"):format(err), vim.log.levels.WARN)
      end
      if M._session.render_failures >= 3 then
        M._session.render_disabled = true
      end
    end
  end

  if not lines then
    lines = M.sanitize(M.card(ctx), cfg, ctx.cycle_key) or {}
  end

  local budget = tonumber(((cfg.ui or {}).capture or {}).render_budget_ms) or 50
  local elapsed_ms = (uv.hrtime() - started) / 1e6
  if budget > 0 and elapsed_ms > budget and not M._session.budget_warned then
    M._session.budget_warned = true
    M.notify(
      ("the capture card took %.1f ms (ui.capture.render_budget_ms = %d) — rendering continued"):format(elapsed_ms, budget),
      vim.log.levels.WARN
    )
  end
  return lines
end

---------------------------------------------------------------------------
-- foldtext (spec 15 §3.4)
---------------------------------------------------------------------------

M.DEFAULT_FOLDTEXT = "▸ frontmatter ({count} fields) — {cycle_fields} cycles, zo opens"

--- `cycle_fields` unbound: naming a key that does nothing would be a lie.
M.DEFAULT_FOLDTEXT_UNBOUND = "▸ frontmatter ({count} fields) — zo opens"

--- ⚠ The foldtext must NEVER contain a hardcoded key literal: a user who
--- rebinds `cycle_fields` must read their own binding back (spec 15 §3.4).
function M.foldtext(ctx)
  local capture_cfg = ((ctx.config or {}).ui or {}).capture or {}
  local spec = capture_cfg.foldtext
  local builtin = (ctx.cycle_key == nil or ctx.cycle_key == "") and M.DEFAULT_FOLDTEXT_UNBOUND or M.DEFAULT_FOLDTEXT
  if type(spec) == "function" then
    local ok, result = pcall(spec, ctx)
    if ok and type(result) == "string" and not result:find("\n", 1, true) then
      return result
    end
    return M.expand_foldtext(builtin, ctx)
  end
  return M.expand_foldtext(type(spec) == "string" and spec or builtin, ctx)
end

function M.expand_foldtext(template, ctx)
  local slots = {
    count = tostring(ctx.count or 0),
    cycle_fields = tostring(ctx.cycle_key or "zi"),
    lines = tostring(ctx.lines or 0),
  }
  -- ⚠ `[%w_]`, not `%w`: the slot this document actually ships is
  -- `{cycle_fields}`, and `%w` does not match the underscore — the built-in
  -- foldtext would render the placeholder verbatim.
  return (template:gsub("{([%w_]+)}", function(name)
    return slots[name] or ("{" .. name .. "}")
  end))
end

return M
