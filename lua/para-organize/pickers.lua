--- Telescope pickers (spec 03 §4) — destination search, note search, saved
--- searches and the generic filter picker.
---
--- Thin-client law (spec 10 §1): every list rendered here comes from an RPC
--- call. Nothing in this module opens a directory, stats a file or reads the
--- vault; `vim.fs.dirname`/`basename` are used only as string functions over
--- paths the CORE returned.
---
--- Telescope is a hard dependency of the plugin (spec 03 §1), but a picker
--- that hard-errors when it is missing turns an optional-looking dependency
--- into a crash, so every picker degrades to `vim.ui.select` (spec 10 §1:
--- clear behaviour, no stack trace).
---
--- Owned by the pickers+health+commands seat.

local M = {}

local PREFIX = "para-organize: "

--- Sync-RPC timeout (ms) used when the client only offers `request_sync`.
M.timeout = 5000

--- Seconds a derived destination list stays cached. The core pushes
--- `index-updated` events (spec 10 §2); whoever subscribes should call
--- `M.invalidate_cache()` on one, at which point this TTL is only a backstop.
M.cache_ttl = 30

--- `nil` = auto-detect, `false` = force the `vim.ui.select` path (tests, and
--- users who genuinely have no telescope).
M.telescope_enabled = nil

--- Injectable client source. `nil` ⇒ ask `para-organize.core` to connect.
---@type nil|fun(opts: table|nil): table|nil, string|nil
M.client_provider = nil

--- NoteRecord `para_type` is SINGULAR ("project"); Suggestion `type` is the
--- PLURAL para_folders key ("projects"). Verified against the live core — the
--- UI groups on the plural form, so records are translated on the way in.
M.PARA_TYPE_PLURAL = {
  project = "projects",
  area = "areas",
  resource = "resources",
  archive = "archives",
  capture = "capture",
  other = "other",
}

--- Type letters rendered in front of every folder line (spec 03 §3).
M.TYPE_LETTER = {
  projects = "P",
  areas = "A",
  resources = "R",
  archives = "🗑",
  capture = "C",
  other = "?",
}

--- Session-filter keys the core accepts, and the frontmatter key `meta.values`
--- answers for each (spec 03 §2 "Session filters"). `status` is stored as
--- `processing_status`; `since`/`until_date`/`text` are free text with no value
--- list to offer.
M.FILTER_VALUE_KEYS = {
  tags = "tags",
  sources = "sources",
  modalities = "modalities",
  status = "processing_status",
  para_type = "para_type",
}

M.FILTER_ORDER = { "tags", "sources", "modalities", "status", "para_type", "since", "until_date", "text" }

--------------------------------------------------------------------------
-- small helpers
--------------------------------------------------------------------------

local function notify(msg, level)
  vim.notify(PREFIX .. msg, level or vim.log.levels.ERROR)
end

M.notify = notify

--- Today, and `n` days ago, as `YYYY-MM-DD` (the core's date filter format).
local function day_offset(days)
  return os.date("%Y-%m-%d", os.time() - (days or 0) * 86400)
end

--- Force a table to encode as a JSON OBJECT.
---
--- `vim.json.encode({})` emits `[]`, and the core rejects an array where it
--- wants an object ("params must be an object", "'criteria' must be an object
--- of filter=value pairs"). Lua cannot distinguish the two, so every params
--- table that may legitimately be empty goes through here.
---@param t table|nil
function M.obj(t)
  if type(t) ~= "table" or vim.tbl_isempty(t) then
    return vim.empty_dict()
  end
  return t
end

local obj = M.obj

local function strip_trailing_slash(path)
  if type(path) ~= "string" then
    return path
  end
  local out = path:gsub("/+$", "")
  return out
end

--- Display path for an absolute path the core returned: vault-relative when
--- the root is known, else the trailing two segments (`projects/blog`).
---@param path string
---@param root string|nil
---@return string
function M.short_path(path, root)
  path = strip_trailing_slash(tostring(path or ""))
  if type(root) == "string" and root ~= "" then
    root = strip_trailing_slash(root)
    if path:sub(1, #root + 1) == root .. "/" then
      return path:sub(#root + 2)
    end
  end
  local parent = vim.fs.dirname(path)
  local base = vim.fs.basename(path)
  local grandparent = vim.fs.basename(parent)
  if grandparent and grandparent ~= "" and grandparent ~= "/" and parent ~= path then
    return grandparent .. "/" .. base
  end
  return base
end

---@return string the `[P]`-style prefix for a folder entry
function M.type_letter(para_type)
  local plural = M.PARA_TYPE_PLURAL[para_type] or para_type
  return M.TYPE_LETTER[plural] or "?"
end

--- One folder line: `[P] blog  projects/blog  (2.10)` (spec 03 §3; the score
--- is rendered only when `show_scores` is on, per `ui.display.show_scores`).
function M.format_folder(entry, opts)
  opts = opts or {}
  local parts = {
    ("[%s]"):format(M.type_letter(entry.type)),
    entry.name or M.short_path(entry.path, opts.vault_root),
  }
  local short = M.short_path(entry.path, opts.vault_root)
  if short ~= entry.name then
    parts[#parts + 1] = ("(%s)"):format(short)
  end
  if entry.route and entry.route ~= vim.NIL then
    parts[#parts + 1] = ("← route:%s"):format(entry.route)
  end
  if opts.show_scores and type(entry.score) == "number" then
    parts[#parts + 1] = ("%.2f"):format(entry.score)
  end
  return table.concat(parts, " ")
end

--- One note line: `[F] alias-or-title  (projects/blog)` — alias first, per
--- spec 03 §3 ("alias = frontmatter aliases[1], fall back to filename").
function M.format_note(record, opts)
  opts = opts or {}
  local aliases = record.aliases
  local label
  if type(aliases) == "table" and type(aliases[1]) == "string" and aliases[1] ~= "" then
    label = aliases[1]
  end
  label = label or record.title
  if label == nil or label == "" or label == vim.NIL then
    -- `folder.children` notes carry no `filename`, so basename is the last
    -- resort rather than a nil concatenation.
    label = record.filename or vim.fs.basename(tostring(record.path or "?"))
  end
  return ("[F] %s (%s)"):format(label, M.short_path(record.path, opts.vault_root))
end

--------------------------------------------------------------------------
-- RPC plumbing
--------------------------------------------------------------------------

--- Is this error the core's "I do not serve that method" answer? The core
--- returns JSON-RPC -32601 with `data.kind = "MethodNotFound"` and a
--- `data.known_methods` list, so a client can feature-detect instead of
--- hard-coding which core version it is talking to.
function M.is_method_not_found(err)
  if type(err) == "table" then
    if err.code == -32601 then
      return true
    end
    local data = err.data
    if type(data) == "table" and data.kind == "MethodNotFound" then
      return true
    end
    err = err.message
  end
  if type(err) ~= "string" then
    return false
  end
  return err:find("-32601", 1, true) ~= nil or err:find("unknown method", 1, true) ~= nil
end

--- Human text for whatever the RPC layer handed back as an error.
function M.error_message(err)
  if err == nil then
    return "unknown error"
  end
  if type(err) == "string" then
    return err
  end
  if type(err) == "table" then
    local msg = err.message or err.msg or err.error
    local hint = type(err.data) == "table" and err.data.hint or err.hint
    if msg and hint then
      return ("%s (%s)"):format(msg, hint)
    end
    if msg then
      return tostring(msg)
    end
  end
  return vim.inspect(err)
end

--- Call one RPC method, async when the client supports it, sync otherwise.
---
--- The two `para-organize.rpc` Client entry points use OPPOSITE argument
--- orders — `Client:request(m, p, cb)` calls back `cb(err, result)` while
--- `Client:request_sync(m, p, timeout)` returns `result, err`. That is the
--- rpc module's documented contract, so this is the one place that knows it:
--- every caller below gets `cb(result, err)`.
---
--- It also enforces the ARCHITECTURE.md obligation centrally: `op.*` and
--- `folder.create` report application failure via `result.ok = false` WITH a
--- normal JSON-RPC success envelope, so checking only the RPC error is not
--- enough. Absent `ok` (every list/query method) passes through untouched —
--- `nil ~= false`.
---@param client table
function M.rpc(client, method, params, cb)
  if type(client) ~= "table" then
    return cb(nil, "no core client (is `organize serve` reachable?)")
  end
  local function deliver(result, err)
    if err == nil and type(result) == "table" and result.ok == false then
      local reason = result.error or result.message or result.errors
      return cb(nil, ("%s failed: %s"):format(method, M.error_message(reason ~= nil and reason or result)))
    end
    cb(result, err)
  end
  if type(client.request) == "function" then
    local ok, err = pcall(client.request, client, method, params, function(rerr, result)
      deliver(result, rerr)
    end)
    if not ok then
      cb(nil, tostring(err))
    end
    return
  end
  if type(client.request_sync) == "function" then
    local ok, result, err = pcall(client.request_sync, client, method, params, M.timeout)
    if not ok then
      return cb(nil, tostring(result))
    end
    return deliver(result, err)
  end
  cb(nil, "rpc client exposes neither request() nor request_sync()")
end

--- Resolve the client to talk to. Never spawns anything itself: that is
--- `para-organize.core`'s job, and it is the only module allowed to do it.
---@return table|nil client, string|nil err
function M.client(opts)
  if M.client_provider then
    return M.client_provider(opts)
  end
  if type(opts) == "table" and opts.client then
    return opts.client
  end
  local ok, core = pcall(require, "para-organize.core")
  if not ok or type(core) ~= "table" or type(core.ensure_running) ~= "function" then
    return nil, "para-organize.core is unavailable — cannot reach the organize core"
  end
  local cfg = {}
  local cfg_ok, config = pcall(require, "para-organize.config")
  if cfg_ok and type(config) == "table" then
    if type(config.get) == "function" then
      local got_ok, got = pcall(config.get)
      if got_ok and type(got) == "table" then
        cfg = got
      end
    elseif type(config.options) == "table" then
      cfg = config.options
    end
  end
  local client, cerr = core.ensure_running(cfg)
  if not client then
    -- `core.ensure_running` fails with a STRUCTURED `rpc.error` table; this
    -- function's contract is `string|nil err` (and `notify` concatenates),
    -- so flatten it here — the one seam where core's error type crosses
    -- into pickers.
    return nil, M.error_message(cerr)
  end
  return client
end

--------------------------------------------------------------------------
-- the telescope block of `setup{}` (spec 03 §4)
--------------------------------------------------------------------------

--- The resolved `telescope` config, or `{}` when `setup()` has not run.
---
--- Spec 03 §1 makes "every config key must be either honored or deleted" a
--- hard law, and §4 names the five keys the pickers must respect. They were
--- all dead: `telescope_select` hardcoded `get_dropdown({})` and merged an
--- `opts.telescope_opts` no caller ever supplied.
function M.telescope_config()
  local ok, config = pcall(require, "para-organize.config")
  if not (ok and type(config) == "table" and type(config.get) == "function") then
    return {}
  end
  local got_ok, got = pcall(config.get)
  if not (got_ok and type(got) == "table") then
    return {}
  end
  return got.telescope or {}
end

--- `telescope.theme` → the theme table, with `layout_strategy`,
--- `layout_config` and `previewer` layered on top.
---
---@param opts table|nil per-call overrides (`telescope_opts` still wins, so a
---       caller — or a spec — can force a shape without touching setup()).
function M.telescope_theme(opts)
  opts = opts or {}
  local cfg = M.telescope_config()
  local themes = require("telescope.themes")
  local builders = {
    dropdown = themes.get_dropdown,
    ivy = themes.get_ivy,
    cursor = themes.get_cursor,
  }
  local theme
  local name = cfg.theme or "dropdown"
  if name == "none" then
    theme = {}
  else
    local build = builders[name] or themes.get_dropdown
    theme = build({})
  end
  if cfg.layout_strategy ~= nil then
    theme.layout_strategy = cfg.layout_strategy
  end
  if type(cfg.layout_config) == "table" and next(cfg.layout_config) ~= nil then
    theme.layout_config = vim.tbl_deep_extend("force", theme.layout_config or {}, cfg.layout_config)
  end
  -- `previewer = false` is meaningful (it is telescope's own "no previewer"),
  -- so this must be an explicit nil check rather than a truthiness test.
  if cfg.previewer ~= nil then
    theme.previewer = cfg.previewer
  end
  return vim.tbl_deep_extend("force", theme, opts.telescope_opts or {})
end

--- Does `telescope.multi_select` allow picking more than one entry?
function M.multi_select_enabled(opts)
  if type(opts) == "table" and opts.multi_select ~= nil then
    return opts.multi_select and true or false
  end
  return M.telescope_config().multi_select and true or false
end

--------------------------------------------------------------------------
-- destination cache (spec 03 §4: "folder listing cache keyed by root path")
--------------------------------------------------------------------------

M._cache = {}

--- Drop the cached folder listing (all roots, or one).
function M.invalidate_cache(root)
  if root == nil then
    M._cache = {}
  else
    M._cache[root] = nil
  end
end

local function cache_get(key)
  local hit = M._cache[key]
  if not hit then
    return nil
  end
  if (os.time() - hit.at) > M.cache_ttl then
    M._cache[key] = nil
    return nil
  end
  return hit.items
end

local function cache_put(key, items)
  M._cache[key] = { items = items, at = os.time() }
end

--------------------------------------------------------------------------
-- data: destinations
--------------------------------------------------------------------------

local function suggestion_entry(raw)
  local route = raw.route
  if route == vim.NIL then
    route = nil
  end
  return {
    path = strip_trailing_slash(raw.path),
    name = raw.name or vim.fs.basename(strip_trailing_slash(raw.path or "")),
    -- ARCHITECT-SANCTIONED INTERIM (2026-08-15) — DELETE when the core fix
    -- lands. Ruling: destination-kind vocabulary is the PLURAL form
    -- ("projects"|"areas"|"resources") for Suggestion.type, folder.list
    -- output and folder.create's para_type; SINGULAR ParaType is note-record
    -- identity only. `folder.list` currently still emits the SINGULAR
    -- (server.py PARA_KEY_TO_TYPE) — a one-line core fix is queued. Until it
    -- ships, normalize here so `derive_destinations`' merged list never holds
    -- "project" and "projects" for sibling folders. Once folder.list emits
    -- the plural, this map is a no-op mask for future drift — remove it.
    type = M.PARA_TYPE_PLURAL[raw.type] or raw.type,
    score = type(raw.score) == "number" and raw.score or nil,
    reasons = type(raw.reasons) == "table" and raw.reasons or {},
    route = route,
    description = raw.description ~= vim.NIL and raw.description or nil,
    source = "suggest",
  }
end

--- Fold NoteRecord[] into the set of PARA folders that contain notes.
--- `record.folder` is only the immediate parent NAME, so the folder PATH comes
--- from `dirname(record.path)` — string work on core-supplied data, not a
--- directory read.
function M.folders_from_records(records, opts)
  opts = opts or {}
  local out, seen = {}, {}
  for _, record in ipairs(records or {}) do
    local plural = M.PARA_TYPE_PLURAL[record.para_type]
    local wanted = plural == "projects" or plural == "areas" or plural == "resources"
    if plural == "archives" and opts.include_archive then
      wanted = true
    end
    if wanted and type(record.path) == "string" then
      local dir = strip_trailing_slash(vim.fs.dirname(record.path))
      if dir ~= "" and not seen[dir] then
        seen[dir] = true
        out[#out + 1] = {
          path = dir,
          name = vim.fs.basename(dir),
          type = plural,
          reasons = {},
          source = "index",
        }
      end
    end
  end
  table.sort(out, function(a, b)
    return a.path < b.path
  end)
  return out
end

--- Destination folders for the picker.
---
--- Preferred source is a `folder.list` RPC; the core does not serve one yet
--- (verified: RPC_METHODS has no folder listing), so this feature-detects it
--- and otherwise unions what IS reachable:
---   * `suggest.for_note` — the core enumerates candidate folders from DISK,
---     so empty folders appear, but the list is capped at
---     `suggestions.max_suggestions`; and
---   * `search.query` — every folder that holds at least one indexed note.
--- The union is complete for folders with notes and best-effort for empty
--- ones. A core-side `folder.list` removes the caveat; see seam requests.
---@param client table
---@param opts table|nil `{ note = <path>, include_archive = bool, vault_root = str }`
---@param cb fun(items: table[]|nil, err: string|nil)
function M.fetch_destinations(client, opts, cb)
  opts = opts or {}
  local key = opts.cache_key or (opts.vault_root or "") .. "|destinations"
  if not opts.no_cache then
    local hit = cache_get(key)
    if hit then
      return cb(hit)
    end
  end

  local function finish(items)
    cache_put(key, items)
    cb(items)
  end

  M.rpc(client, "folder.list", obj({ para_type = opts.para_type }), function(result, err)
    if type(result) == "table" then
      local raw = result.folders or result
      local items = {}
      for _, entry in ipairs(raw) do
        -- `folder.list` returns archive folders too. Archiving is its own
        -- action (03 §6), so the destination picker hides them unless asked.
        local plural = M.PARA_TYPE_PLURAL[entry.type] or entry.type
        if plural ~= "archives" or opts.include_archive then
          items[#items + 1] = suggestion_entry(entry)
        end
      end
      return finish(items)
    end
    if err ~= nil and not M.is_method_not_found(err) then
      return cb(nil, M.error_message(err))
    end
    M.derive_destinations(client, opts, function(items, derr)
      if not items then
        return cb(nil, derr)
      end
      finish(items)
    end)
  end)
end

--- The `folder.list`-less fallback described on `fetch_destinations`.
function M.derive_destinations(client, opts, cb)
  opts = opts or {}
  M.rpc(client, "search.query", { criteria = vim.empty_dict() }, function(records, err)
    if type(records) ~= "table" then
      return cb(nil, M.error_message(err))
    end
    local ordered = {}
    local by_path = {}

    local function add(entry)
      local existing = by_path[entry.path]
      if existing then
        -- A suggestion carries score/reasons/route the index cannot; let it win.
        if entry.source == "suggest" then
          for k, v in pairs(entry) do
            existing[k] = v
          end
        end
        return
      end
      by_path[entry.path] = entry
      ordered[#ordered + 1] = entry
    end

    local function with_suggestions(suggestions)
      for _, raw in ipairs(suggestions or {}) do
        if type(raw) == "table" and type(raw.path) == "string" then
          add(suggestion_entry(raw))
        end
      end
      for _, entry in ipairs(M.folders_from_records(records, opts)) do
        add(entry)
      end
      cb(ordered)
    end

    if type(opts.note) == "string" and opts.note ~= "" then
      M.rpc(client, "suggest.for_note", { path = opts.note }, function(suggestions, serr)
        if type(suggestions) ~= "table" then
          -- Suggestions are a bonus here, not the answer: a note the core
          -- cannot suggest for must not empty the destination picker.
          if serr ~= nil then
            vim.notify(
              PREFIX .. "suggestions unavailable for this capture (" .. M.error_message(serr) .. ")",
              vim.log.levels.DEBUG
            )
          end
          return with_suggestions(nil)
        end
        with_suggestions(suggestions)
      end)
    else
      with_suggestions(nil)
    end
  end)
end

--------------------------------------------------------------------------
-- data: notes
--------------------------------------------------------------------------

--- `search.query` with the criteria a session filter table uses.
function M.fetch_notes(client, criteria, cb)
  M.rpc(client, "search.query", { criteria = obj(criteria) }, function(records, err)
    if type(records) ~= "table" then
      return cb(nil, M.error_message(err))
    end
    cb(records)
  end)
end

--- One directory level: `{dirs[], notes[]}` (spec 03 §3 browsing).
--- `dirs` entries are `{path, name, description?}`; `notes` entries are
--- `{path, title, aliases, para_type}` — note the browse shape is NOT a full
--- NoteRecord, so renderers must not assume `filename`.
function M.fetch_folder_children(client, folder, cb)
  M.rpc(client, "folder.children", { path = folder }, function(result, err)
    if type(result) ~= "table" then
      return cb(nil, M.error_message(err), M.is_method_not_found(err))
    end
    cb({ dirs = result.dirs or {}, notes = result.notes or {} })
  end)
end

--- Notes directly inside `folder`.
---
--- `folder.children` is the core's own answer (and the 03 §3 browse contract);
--- the `search.query`-and-filter path stays as a fallback for a core that does
--- not serve it, since client-side filtering of records the core returned is
--- allowed where walking the directory would not be.
function M.fetch_folder_notes(client, folder, cb)
  local want = strip_trailing_slash(tostring(folder or ""))

  local function by_query()
    M.fetch_notes(client, {}, function(records, err)
      if not records then
        return cb(nil, err)
      end
      local out = {}
      for _, record in ipairs(records) do
        if
          type(record.path) == "string"
          and strip_trailing_slash(vim.fs.dirname(record.path)) == want
        then
          out[#out + 1] = record
        end
      end
      table.sort(out, function(a, b)
        return tostring(a.path or "") < tostring(b.path or "")
      end)
      cb(out)
    end)
  end

  M.fetch_folder_children(client, want, function(children, err, unsupported)
    if not children then
      if unsupported then
        return by_query()
      end
      return cb(nil, err)
    end
    local notes = children.notes
    table.sort(notes, function(a, b)
      return tostring(a.path or "") < tostring(b.path or "")
    end)
    cb(notes)
  end)
end

--- Distinct values of one session-filter key, from `meta.values` (spec 07's
--- `complete = "existing"` surface, which answers for `para_type` too).
function M.fetch_filter_values(client, filter_key, cb)
  local meta_key = M.FILTER_VALUE_KEYS[filter_key]
  if not meta_key then
    return cb({})
  end
  M.rpc(client, "meta.values", { key = meta_key }, function(result, err)
    if type(result) ~= "table" then
      return cb(nil, M.error_message(err))
    end
    cb(result.values or {})
  end)
end

--------------------------------------------------------------------------
-- saved searches (spec 03 §4: the nine built-ins)
--------------------------------------------------------------------------

--- Built at call time, never at require time — "This Week" depends on today
--- (spec 09 §2: no side effects on require).
function M.saved_searches()
  return {
    { name = "Unprocessed Captures", criteria = { status = { "raw" }, para_type = { "capture" } } },
    { name = "Today's Notes", criteria = { since = day_offset(0), until_date = day_offset(0) } },
    { name = "This Week", criteria = { since = day_offset(6) } },
    { name = "With Audio", criteria = { modalities = { "audio" } } },
    { name = "Meeting Notes", criteria = { tags = { "meeting" } } },
    -- No filter expresses "has no tags", so this one post-filters the records
    -- the core returned.
    { name = "No Tags", criteria = {}, post = function(record)
      local tags = record.tags
      return type(tags) ~= "table" or #tags == 0
    end },
    { name = "Projects", criteria = { para_type = { "project" } } },
    { name = "Areas", criteria = { para_type = { "area" } } },
    { name = "Resources", criteria = { para_type = { "resource" } } },
  }
end

--------------------------------------------------------------------------
-- picker backend
--------------------------------------------------------------------------

---@return boolean
function M.has_telescope()
  if M.telescope_enabled == false then
    return false
  end
  local ok = pcall(require, "telescope")
  return ok
end

--- Which backend `M.select` would use: `"telescope"` or `"ui.select"`.
function M.backend(opts)
  opts = opts or {}
  if opts.telescope == false then
    return "ui.select"
  end
  return M.has_telescope() and "telescope" or "ui.select"
end

local function telescope_select(items, opts, on_choice)
  local t_pickers = require("telescope.pickers")
  local t_finders = require("telescope.finders")
  local t_conf = require("telescope.config").values
  local t_actions = require("telescope.actions")
  local t_state = require("telescope.actions.state")

  -- Spec 03 §4: honour `telescope.theme` / `layout_strategy` / `layout_config`
  -- / `previewer` / `multi_select` from `setup{}`.
  local theme = M.telescope_theme(opts)
  theme.prompt_title = opts.prompt
  local multi = M.multi_select_enabled(opts)

  t_pickers
    .new(theme, {
      finder = t_finders.new_table({
        results = items,
        entry_maker = function(item)
          local display = opts.format_item(item)
          return { value = item, display = display, ordinal = display }
        end,
      }),
      sorter = t_conf.generic_sorter(theme),
      attach_mappings = function(bufnr)
        t_actions.select_default:replace(function()
          -- With `telescope.multi_select` on, a `<Tab>`-marked selection wins
          -- over the cursor entry and every marked value is handed back.
          local chosen
          if multi then
            local picker = t_state.get_current_picker(bufnr)
            local marked = picker and picker:get_multi_selection() or {}
            if #marked > 0 then
              chosen = {}
              for _, entry in ipairs(marked) do
                chosen[#chosen + 1] = entry.value
              end
            end
          end
          if chosen == nil then
            local entry = t_state.get_selected_entry()
            chosen = entry and entry.value or nil
          elseif #chosen == 1 then
            chosen = chosen[1]
          end
          t_actions.close(bufnr)
          if on_choice then
            on_choice(chosen)
          end
        end)
        return true
      end,
    })
    :find()
end

--- Present `items`, via telescope when available and `vim.ui.select` when not.
---@return string backend `"telescope"`, `"ui.select"` or `"empty"`
function M.select(items, opts, on_choice)
  items = items or {}
  opts = opts or {}
  opts.format_item = opts.format_item or tostring
  on_choice = on_choice or function() end

  if #items == 0 then
    notify(opts.empty_message or "nothing to pick from", vim.log.levels.WARN)
    on_choice(nil)
    return "empty"
  end

  if M.backend(opts) == "telescope" then
    local ok, err = pcall(telescope_select, items, opts, on_choice)
    if ok then
      return "telescope"
    end
    notify(("telescope picker failed (%s) — falling back to vim.ui.select"):format(
      vim.split(tostring(err), "\n")[1]
    ), vim.log.levels.WARN)
  end

  vim.ui.select(items, {
    prompt = opts.prompt,
    format_item = opts.format_item,
    kind = opts.kind or "para-organize",
  }, function(choice)
    on_choice(choice)
  end)
  return "ui.select"
end

--------------------------------------------------------------------------
-- the pickers (spec 03 §4)
--------------------------------------------------------------------------

local function resolve_client(opts, cb)
  local client, err = M.client(opts)
  if not client then
    notify(err or "the organize core is not reachable", vim.log.levels.ERROR)
    return false
  end
  cb(client)
  return true
end

--- Pick a PARA destination folder (merge method 2 and `move`, spec 03 §4).
---@param on_select fun(entry: table|nil)
function M.open_folder_picker(on_select, opts)
  opts = opts or {}
  return resolve_client(opts, function(client)
    M.fetch_destinations(client, opts, function(items, err)
      if not items then
        return notify("cannot list destinations: " .. tostring(err))
      end
      M.select(items, {
        prompt = opts.prompt or "PARA destination",
        telescope = opts.telescope,
        telescope_opts = opts.telescope_opts,
        empty_message = "no destination folders found — create one with :ParaOrganize new-project",
        format_item = function(item)
          return M.format_folder(item, opts)
        end,
      }, function(choice)
        if on_select then
          on_select(choice)
        end
      end)
    end)
  end)
end

--- Pick a note inside `folder` (merge method 2, step 2 — spec 03 §4).
function M.open_folder_notes_picker(folder, on_select, opts)
  opts = opts or {}
  return resolve_client(opts, function(client)
    M.fetch_folder_notes(client, folder, function(records, err)
      if not records then
        return notify("cannot list notes: " .. tostring(err))
      end
      M.select(records, {
        prompt = opts.prompt or ("Notes in " .. M.short_path(folder, opts.vault_root)),
        telescope = opts.telescope,
        telescope_opts = opts.telescope_opts,
        empty_message = "no notes in that folder",
        format_item = function(item)
          return M.format_note(item, opts)
        end,
      }, function(choice)
        if on_select then
          on_select(choice)
        end
      end)
    end)
  end)
end

--- Search the index and pick a note (spec 03 §4 `open_search_picker`).
---@param query string|table a free-text query, or a criteria table
function M.open_search_picker(query, opts)
  opts = opts or {}
  local criteria
  if type(query) == "table" then
    criteria = query
  elseif type(query) == "string" and vim.trim(query) ~= "" then
    criteria = { text = vim.trim(query) }
  else
    criteria = {}
  end
  return resolve_client(opts, function(client)
    M.fetch_notes(client, criteria, function(records, err)
      if not records then
        return notify("search failed: " .. tostring(err))
      end
      if type(opts.post) == "function" then
        records = vim.tbl_filter(opts.post, records)
      end
      M.select(records, {
        prompt = opts.prompt or "Search notes",
        telescope = opts.telescope,
        telescope_opts = opts.telescope_opts,
        empty_message = "no notes matched",
        format_item = function(item)
          return M.format_note(item, opts)
        end,
      }, function(choice)
        if opts.on_select then
          return opts.on_select(choice)
        end
        if choice and type(choice.path) == "string" then
          vim.cmd.edit(vim.fn.fnameescape(choice.path))
        end
      end)
    end)
  end)
end

--- Live-updating query over the index (spec 03 §4 `live_search`). Telescope's
--- async finder re-queries the core on every keystroke; without telescope this
--- degrades to one prompt plus a static result picker.
function M.live_search(opts)
  opts = opts or {}
  return resolve_client(opts, function(client)
    if M.backend(opts) ~= "telescope" then
      return vim.ui.input({ prompt = opts.prompt or "Search notes: " }, function(input)
        if input == nil then
          return
        end
        M.open_search_picker(input, opts)
      end)
    end

    local t_pickers = require("telescope.pickers")
    local t_finders = require("telescope.finders")
    local t_conf = require("telescope.config").values
    local t_actions = require("telescope.actions")
    local t_state = require("telescope.actions.state")
    local theme = M.telescope_theme(opts)
    theme.prompt_title = opts.prompt or "Live search"

    t_pickers
      .new(theme, {
        finder = t_finders.new_dynamic({
          fn = function(prompt)
            local out = {}
            M.fetch_notes(client, prompt and prompt ~= "" and { text = prompt } or {}, function(records)
              out = records or {}
            end)
            return out
          end,
          entry_maker = function(item)
            local display = M.format_note(item, opts)
            return { value = item, display = display, ordinal = display }
          end,
        }),
        sorter = t_conf.generic_sorter(theme),
        attach_mappings = function(bufnr)
          t_actions.select_default:replace(function()
            local entry = t_state.get_selected_entry()
            t_actions.close(bufnr)
            if entry and opts.on_select then
              opts.on_select(entry.value)
            elseif entry and type(entry.value.path) == "string" then
              vim.cmd.edit(vim.fn.fnameescape(entry.value.path))
            end
          end)
          return true
        end,
      })
      :find()
  end)
end

--- The nine saved searches (spec 03 §4), plus a live-search entry.
---
--- This is what `:ParaOrganize search` (no query) opens, which is what makes
--- both surfaces reachable — `saved_searches()`, `open_saved_searches_picker`
--- and `live_search` were all implemented with no caller anywhere in `lua/`
--- or `plugin/`, i.e. dead against spec 03 §1's honored-or-deleted law.
---
--- `saved_searches()` still returns exactly the nine the spec names; the live
--- entry is an affordance of the PICKER, not a tenth saved search.
function M.open_saved_searches_picker(opts)
  opts = opts or {}
  local items = { { name = "Live search…", live = true } }
  for _, search in ipairs(M.saved_searches()) do
    items[#items + 1] = search
  end
  M.select(items, {
    prompt = opts.prompt or "Saved searches",
    telescope = opts.telescope,
    telescope_opts = opts.telescope_opts,
    format_item = function(item)
      return item.name
    end,
  }, function(choice)
    if not choice then
      return
    end
    if choice.live then
      return M.live_search(opts)
    end
    M.open_search_picker(
      choice.criteria,
      vim.tbl_extend("force", opts, { prompt = choice.name, post = choice.post })
    )
  end)
  return true
end

--- Generic filter picker: choose a session-filter key, then one of its values
--- (from `meta.values`), and hand `(key, value)` back. Used to build
--- `:ParaOrganize start` filters interactively.
---@param on_select fun(key: string|nil, value: string|nil)
function M.open_filter_picker(on_select, opts)
  opts = opts or {}
  on_select = on_select or function() end
  return resolve_client(opts, function(client)
    M.select(vim.deepcopy(M.FILTER_ORDER), {
      prompt = opts.prompt or "Filter by",
      telescope = opts.telescope,
      telescope_opts = opts.telescope_opts,
      format_item = tostring,
    }, function(key)
      if not key then
        return on_select(nil, nil)
      end
      if not M.FILTER_VALUE_KEYS[key] then
        -- since / until_date / text have no value list to offer.
        return vim.ui.input({ prompt = key .. " = " }, function(value)
          on_select(key, value)
        end)
      end
      M.fetch_filter_values(client, key, function(values, err)
        if not values then
          notify(("cannot list %s values: %s"):format(key, tostring(err)))
          return on_select(nil, nil)
        end
        M.select(values, {
          prompt = key,
          telescope = opts.telescope,
          telescope_opts = opts.telescope_opts,
          empty_message = ("no %s values in the index"):format(key),
          format_item = tostring,
        }, function(value)
          on_select(key, value)
        end)
      end)
    end)
  end)
end

return M
