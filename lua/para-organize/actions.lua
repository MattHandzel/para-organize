--- para-organize.actions — one function per row of spec 03's keymap table.
---
--- THIN CLIENT LAW (spec 10 §1): every action is `RPC → state update →
--- ui.refresh()`. Nothing in this module reads or writes a vault file, shells
--- out, or consults the environment for behaviour. The RPC client is
--- INJECTED (`actions.setup{ client = … }`) so the whole surface is testable
--- against a scripted mock, exactly as spec 10 §2 requires.
---
--- Keymap resolution: `config.keymaps.buffer.<name>`, falling back to the
--- spec-03 defaults in `ui.DEFAULTS`. `s` stays skip GLOBALLY and sort-cycle
--- moved to `S` — the documented resolution of the old double-binding
--- (spec 03 §3 keymap table).
local M = {}

local render = require("para-organize.ui.render")

local uv = vim.uv or vim.loop

--- `vim.json.encode({})` emits `[]`, and the core rejects an array where an
--- object is required (-32602) BEFORE method lookup. `rpc.lua` normalises the
--- TOP-LEVEL params table only, so every NESTED empty object must be built
--- here (the same helper `init.lua` documents; ARCHITECTURE.md "Wire fact").
local function obj(t)
  if type(t) ~= "table" or next(t) == nil then
    return vim.empty_dict()
  end
  return t
end

M._obj = obj

--- Injected context. `client` implements the rpc seat's Client contract:
--- `Client:request(method, params, cb)` where `cb(err, result)`.
local ctx = {
  client = nil,
  state = nil,
  config = nil,
  ui = nil,
  callback_style = "err_first",
}

-- --- plumbing --------------------------------------------------------------

local function notify(msg, level)
  vim.notify("para-organize: " .. msg, level or vim.log.levels.INFO)
end

local function warn(msg)
  notify(msg, vim.log.levels.WARN)
end

local function fail(msg)
  notify(msg, vim.log.levels.ERROR)
end

local function ui()
  return ctx.ui or require("para-organize.ui")
end

local function state()
  return ctx.state
end

local function cfg()
  if ctx.config then
    return ctx.config
  end
  return ui().config()
end

local function refresh()
  local mod = ui()
  if type(mod.refresh) == "function" then
    mod.refresh(state())
  end
  local s = state()
  if s and type(s.emit) == "function" then
    pcall(s.emit, s)
  end
end

--- `processed`/`skipped` may be counters or path lists depending on how the
--- integrator-owned state module models them — support both.
local function mark(key, path)
  local s = state()
  if not s then
    return
  end
  local value = s[key]
  if type(value) == "number" then
    s[key] = value + 1
  elseif type(value) == "table" then
    table.insert(value, path)
  else
    s[key] = { path }
  end
end

--- Normalise the client callback into `(err, result)`. The convention is
--- err-first (documented seam with the rpc seat); `ctx.callback_style =
--- "result_first"` flips it without touching any call site.
local function normalize(a, b)
  if ctx.callback_style == "result_first" then
    return b, a
  end
  return a, b
end

--- Render one error as a single line.
---
--- `rpc.error()` puts `kind`/`hint` at the TOP level of the error table;
--- server-mapped errors additionally carry them under `data`. Reading only
--- `data` (as this did) silently dropped the `[Kind]` tag and the actionable
--- hint from every client-side transport failure — Closed / Disconnected /
--- Timeout / Transport — which is exactly the degradation surface spec 10 §1
--- requires to be clear. Top level first, `data` as the fallback.
local function error_text(err)
  if type(err) == "table" then
    local msg = err.message or err.msg or vim.inspect(err)
    local data = err.data
    local hint = err.hint or (type(data) == "table" and data.hint or nil)
    local kind = err.kind or (type(data) == "table" and data.kind or nil)
    if kind then
      msg = ("%s [%s]"):format(msg, kind)
    end
    if hint then
      msg = ("%s (%s)"):format(msg, hint)
    end
    return msg
  end
  return tostring(err)
end

--- Fire one RPC. Graceful degradation (spec 10 §1): a missing/unreachable
--- core produces ONE clear notification and no stack trace.
---@param method string
---@param params table
---@param done fun(result:any, err:any)|nil
---@param opts table|nil { quiet = boolean }  quiet suppresses the notify so
---       the caller can degrade to a fallback path.
--- A client we can still talk through. A scripted mock (the unit specs) has
--- no `is_alive`, so absence of the method means "assume alive".
local function live(client)
  if type(client) ~= "table" or type(client.request) ~= "function" then
    return false
  end
  if type(client.is_alive) ~= "function" then
    return true
  end
  local ok, alive = pcall(client.is_alive, client)
  if not ok then
    return false
  end
  return alive and true or false
end

--- Resolve an RPC client, lazily (re)connecting through the composition root
--- when the injected one is missing or dead. Two defects this closes:
---
---  * spec 03 §2's session-INDEPENDENT subcommands (`new-project`/`new-area`/
---    `new-resource`) failed with "core is not running" whenever no session
---    had been started, because `ctx.client` is only populated by
---    `actions.setup{}` inside `init.open()`.
---  * after the core died mid-session the injected client stayed wedged for
---    good: `init.reindex` went through `require_client()` and respawned a
---    healthy core, while every session action kept addressing the closed one
---    ("the organize-core connection is closed", forever).
---
--- Gated on `config.is_configured()` so a spec that deliberately runs with no
--- client (the degradation tests) can never reach out to — or auto-spawn — a
--- core against the LIVE vault (CLAUDE.md, spec 09 §1.4).
local function resolve_client()
  if live(ctx.client) then
    return ctx.client
  end
  local ok_cfg, config = pcall(require, "para-organize.config")
  if not (ok_cfg and type(config) == "table" and config.is_configured and config.is_configured()) then
    return nil
  end
  local ok, root = pcall(require, "para-organize")
  if not (ok and type(root) == "table" and type(root.require_client) == "function") then
    return nil
  end
  local ok_client, client = pcall(root.require_client)
  if ok_client and live(client) then
    ctx.client = client
    return client
  end
  -- `require_client` already notified with the core's own kind + hint.
  return false
end

local function rpc(method, params, done, opts)
  opts = opts or {}
  local client = resolve_client()
  if not client then
    if client == nil then
      fail("core is not running — start it with :ParaOrganize start or run :checkhealth para-organize")
    end
    if done then
      done(nil, { message = "no rpc client" })
    end
    return false
  end
  -- A reply that arrives after the session it belongs to has been torn down
  -- (`<Esc>` / `:ParaOrganize stop` mid-load, or a restart with new filters)
  -- must neither drive the UI nor scold the user: the request was abandoned
  -- ON PURPOSE. Comparing the session identity at reply time is the only way
  -- to tell "the core failed" from "we stopped listening".
  local session_at_call = ctx.state
  local abandoned = function()
    return ctx.state ~= session_at_call
  end

  local ok, err = pcall(function()
    client:request(method, params or {}, function(a, b)
      if abandoned() then
        return
      end
      local rerr, result = normalize(a, b)
      if rerr then
        if not opts.quiet then
          fail(method .. ": " .. error_text(rerr))
        end
        if done then
          done(nil, rerr)
        end
        return
      end
      if done then
        done(result, nil)
      end
    end)
  end)
  if not ok then
    if not opts.quiet then
      fail(method .. ": " .. tostring(err))
    end
    if done then
      done(nil, err)
    end
    return false
  end
  return true
end

M._rpc = rpc

--- The documented spelling of the same seam. `para-organize.integrate` fires
--- its two RPCs through here rather than holding a client of its own, so it
--- inherits the lazy reconnect of `resolve_client()`, the abandoned-session
--- guard and the single-notification error rendering — three behaviours a
--- second call site would otherwise have to re-implement and could get
--- subtly wrong.
M.request = rpc

--- Lazily required: `integrate` requires THIS module back (for `request`,
--- `decision_context` and the keymap table), so a top-level require either
--- way would be a load-order cycle.
local function integrate()
  local ok, mod = pcall(require, "para-organize.integrate")
  if ok and type(mod) == "table" then
    return mod
  end
  return nil
end

M._integrate = integrate

-- --- setup / context -------------------------------------------------------

---@param opts table { client, state, config, ui, callback_style }
function M.setup(opts)
  opts = opts or {}
  if opts.client ~= nil then
    ctx.client = opts.client
  end
  if opts.state ~= nil then
    ctx.state = opts.state
  end
  if opts.config ~= nil then
    ctx.config = opts.config
  end
  if opts.ui ~= nil then
    ctx.ui = opts.ui
  end
  if opts.callback_style ~= nil then
    ctx.callback_style = opts.callback_style
  end
  return ctx
end

function M.set_client(client)
  ctx.client = client
end

function M.set_state(session_state)
  ctx.state = session_state
end

function M.context()
  return ctx
end

function M.reset()
  ctx.client, ctx.state, ctx.config, ctx.ui = nil, nil, nil, nil
  ctx.callback_style = "err_first"
end

-- --- session navigation ----------------------------------------------------

function M.current_capture()
  local s = state()
  if not s then
    return nil
  end
  return (s.captures or {})[s.current or 1]
end

--- Load the current capture: refresh its record (`note.get`) and regenerate
--- suggestions (`suggest.for_note`). The pane shows the loading state first
--- so the UI never blocks on the round-trip (spec 09 §2/§4).
function M.load_current()
  local s = state()
  if not s then
    return
  end
  local record = M.current_capture()
  if not record then
    s.view = "empty"
    refresh()
    return
  end
  s.view = "loading"
  s.suggestions = {}
  s.selected = 1
  s.browse = nil
  s.search = nil
  s.merge = nil
  -- A review gate belongs to ONE capture: carrying it across a load would
  -- offer Matt a diff proposed from a note he is no longer looking at.
  if s.integrate then
    s.integrate = nil
    local gate = integrate()
    if gate then
      gate.unbind_gate()
    end
  end
  refresh()

  rpc("note.get", { path = record.path }, function(result, err)
    if not err and result and result.record and s.captures and s.captures[s.current or 1] then
      local merged = vim.tbl_extend("force", record, result.record)
      merged.frontmatter = result.frontmatter or {}
      merged.parse_error = result.parse_error
      s.captures[s.current or 1] = merged
    end
    refresh()
  end)

  rpc("suggest.for_note", { path = record.path }, function(result, err)
    if not err then
      s.suggestions = result or {}
      s.selected = 1
      -- The clock the spec 12 §2 `durations_ms.decision` is measured from:
      -- "the suggestions are on screen" → "Matt chose".
      s.shown_at = uv.now()
    end
    if s.view == "loading" then
      s.view = "suggestions"
    end
    refresh()
  end)
  M.load_meta_fields()
  M.load_roots()
  -- Spec 12 §1's per-route edit mode. Resolved once per capture (the routes
  -- match on ITS tags) so `<CR>` on a destination is instant and never has to
  -- ask mid-keystroke which of the three modes it is in.
  local mod = integrate()
  if mod then
    mod.load_routes()
  end
end

--- The PARA roots rendered as the browse tree of state 1 (see `M.accept`).
---
--- Fetched ONCE per session and derived from `folder.list`'s entries — a thin
--- client may not walk the filesystem (spec 10 §1), and `folder.list` is the
--- only method that enumerates PARA folders. The root of each entry is its
--- parent, which is exactly `vault.para_folders[<key>]`.
---@param done fun(roots: table[])|nil
function M.load_roots(done)
  local s = state()
  if not s then
    return
  end
  if s.roots then
    if done then
      done(s.roots)
    end
    return
  end
  rpc("folder.list", obj({}), function(result, err)
    if err or type(result) ~= "table" then
      return
    end
    local by_path, roots = {}, {}
    for _, folder in ipairs(result.folders or {}) do
      local path = type(folder) == "table" and folder.path or nil
      if type(path) == "string" and path ~= "" then
        local root = vim.fn.fnamemodify(path:gsub("/+$", ""), ":h")
        if root and root ~= "" and root ~= "." and not by_path[root] then
          by_path[root] = true
          roots[#roots + 1] = {
            kind = "root",
            type = folder.type,
            path = root,
            name = vim.fn.fnamemodify(root, ":t"),
          }
        end
      end
    end
    table.sort(roots, function(a, b)
      local ra = render.ROOT_ORDER[render.TYPE_ALIASES[a.type] or a.type] or 99
      local rb = render.ROOT_ORDER[render.TYPE_ALIASES[b.type] or b.type] or 99
      if ra ~= rb then
        return ra < rb
      end
      return tostring(a.name) < tostring(b.name)
    end)
    if s ~= state() then
      return
    end
    s.roots = roots
    if done then
      done(roots)
    end
    refresh()
  end, { quiet = true })
end

--- Spec 03 §6: auto-advance after accept/merge/archive; on exhaustion show a
--- completion message with counts and close.
function M.advance()
  local s = state()
  if not s then
    return false
  end
  local total = #(s.captures or {})
  if (s.current or 1) >= total then
    s.view = "empty"
    refresh()
    notify(("session complete — processed %d, skipped %d"):format(render.count(s.processed), render.count(s.skipped)))
    if (cfg().ui or {}).close_on_complete ~= false then
      M.quit()
    end
    return false
  end
  s.current = (s.current or 1) + 1
  M.load_current()
  return true
end

function M.next_capture()
  local s = state()
  if not s then
    return
  end
  local total = #(s.captures or {})
  if (s.current or 1) >= total then
    warn("already at the last capture")
    return
  end
  s.current = (s.current or 1) + 1
  M.load_current()
end

function M.prev_capture()
  local s = state()
  if not s then
    return
  end
  if (s.current or 1) <= 1 then
    warn("already at the first capture")
    return
  end
  s.current = (s.current or 1) - 1
  M.load_current()
end

--- Spec 03 §6: skip has NO file effect — but per the architect's op.skip
--- ruling (spec 12 §2's "a skip is signal too") it DOES emit one `op.skip`
--- carrying the decision context the core cannot know: `session_id`,
--- `suggestions_shown` (the rendered entries with their ranks) and
--- `durations_ms.decision`. `chosen_rank` is absent — nothing was chosen.
---
--- Fire-and-forget, and `quiet`: the skip must stay instant (spec 09 §4) and
--- an older core without `op.skip` answers MethodNotFound, which must not
--- scold — the learning record is a bonus, never a gate on Matt's flow.
function M.skip()
  local record = M.current_capture()
  if not record then
    warn("no capture to skip")
    return
  end
  -- Context is captured BEFORE advance() — it reads THIS capture's
  -- suggestions and shown-at clock, which advance() resets.
  --
  -- `session_id` is REQUIRED by the core: op.skip records against a live
  -- in-memory session, so with no id — injected unit state, or an id a core
  -- restart invalidated — there is nothing to record against and no request
  -- is sent, rather than one doomed to -32602.
  local s = state()
  if s and s.session_id then
    -- The one op.* asymmetry: the capture param is `note`, NOT `path`.
    -- `filters` is trimmed because op.skip's wire contract names only
    -- session_id / suggestions_shown / durations_ms / dry_run.
    local params = vim.tbl_extend(
      "force",
      { note = record.path },
      M.decision_context({ chosen_rank = M.NONE, filters = M.NONE })
    )
    rpc("op.skip", params, nil, { quiet = true })
  end
  mark("skipped", record.path)
  M.advance()
end

-- --- decision context (spec 12 §2) -----------------------------------------

--- The per-call decision context every mutating op carries.
---
--- Spec 12 §2: "The counterfactual is stored, not just the choice:
--- `suggestions_shown` + `chosen_rank` turn every session action into a
--- labeled ranking example." The core has accepted these as op params all
--- along; the nvim client — the only session-aware client there is — never
--- sent them, so every ActionRecord a real session produced had
--- `session_id: null`, `chosen_rank: null`, `suggestions_shown: []`, and
--- spec 12 §3's acceptance test ("a session of 5 actions yields 5 records
--- with consistent `session_id` and correct `chosen_rank`s") was
--- unsatisfiable through the product.
---
--- Shapes mirror the core's validators exactly (`server._suggestions_shown_from`
--- → `{path, score, rank, reasons}`; `_opt_rank` → integer; `_filters_from`
--- → object; `_durations_from` → object of numbers), and every nested empty
--- table is built with `vim.empty_dict()` because `vim.json.encode({})`
--- emits `[]` (init.lua's wire fact).
---@param overrides table|nil extra context keys (e.g. `chosen_rank = nil`)
---@return table
function M.decision_context(overrides)
  local s = state()
  if not s then
    return overrides or {}
  end

  local shown = {}
  for rank, suggestion in ipairs(s.suggestions or {}) do
    if type(suggestion) == "table" and type(suggestion.path) == "string" then
      local reasons = {}
      for _, reason in ipairs(suggestion.reasons or {}) do
        reasons[#reasons + 1] = tostring(reason)
      end
      shown[#shown + 1] = {
        path = suggestion.path,
        score = tonumber(suggestion.score) or 0,
        rank = rank,
        reasons = reasons,
      }
    end
  end

  local context = {
    session_id = s.session_id,
    filters = (next(s.filters or {}) ~= nil) and s.filters or vim.empty_dict(),
    suggestions_shown = shown,
    chosen_rank = (#shown > 0) and math.floor(s.selected or 1) or nil,
  }

  if type(s.shown_at) == "number" then
    -- Milliseconds from "the suggestions rendered" to "Matt decided" — the
    -- only duration the client is in a position to measure.
    context.durations_ms = { decision = math.max(0, math.floor((vim.uv or vim.loop).now() - s.shown_at)) }
  end

  for key, value in pairs(overrides or {}) do
    if value == M.NONE then
      context[key] = nil
    else
      context[key] = value
    end
  end
  return context
end

--- Sentinel for `decision_context{ key = actions.NONE }` — Lua cannot store
--- nil in a table literal, and `chosen_rank` must be absent (not 0) on a
--- skip, where by definition nothing was chosen.
M.NONE = setmetatable({}, { __tostring = function() return "actions.NONE" end })

-- --- mutating actions ------------------------------------------------------

--- Guard against a double `<CR>`/`a` while a mutating op is still in flight.
--- Without it the second keypress fired a second `op.move` for a capture the
--- core had already moved, and the reply came back as a red ERROR about a
--- missing note — an alarming message for an ordinary double keypress
--- (spec 09 §1.5: loud failure is for real misconfiguration).
local function begin_op(path)
  local s = state()
  if not s then
    return true
  end
  if s.in_flight then
    return false
  end
  s.in_flight = path or true
  return true
end

local function end_op()
  local s = state()
  if s then
    s.in_flight = nil
  end
end

local function op_failed(result, label)
  if type(result) == "table" and result.ok == false then
    fail(("%s failed: %s"):format(label, tostring(result.error or "unknown error")))
    return true
  end
  return false
end

--- Every loaded buffer whose name is exactly `path`.
local function buffers_for(path)
  local out = {}
  if type(path) ~= "string" or path == "" then
    return out
  end
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buf) and vim.api.nvim_buf_get_name(buf) == path then
      out[#out + 1] = buf
    end
  end
  return out
end

--- Refuse a vault operation whose capture buffer holds unsaved edits.
---
--- The left pane is the REAL capture file (spec 10 §4) and the core re-reads
--- the file on every operation — so an unsaved edit is not in the bytes the
--- core is about to move, and proceeding would silently drop it. Spec 09 §1.5
--- ("loud failure") makes this a refusal with a way out, never a silent
--- discard.
---@return boolean ok false ⇒ the caller must not dispatch
local function capture_is_saved(path, what)
  for _, buf in ipairs(buffers_for(path)) do
    if vim.bo[buf].modified then
      fail(
        ("%s: the capture has unsaved changes — :w to keep them, :e! to discard, then try again"):format(
          what
        )
      )
      return false
    end
  end
  return true
end

--- Retire the capture buffer once the core has moved the file out from under
--- it.
---
--- Without this the stale buffer stays loaded, still pointing at a path the
--- core has emptied — and an ordinary `:w`/`:wall`/`:wqa` RE-CREATES the
--- capture at its original vault path, undoing the move and duplicating the
--- note. That is an unlogged vault write the core's mtime/hash guard cannot
--- even see, falsifying spec 10 §4's premise that this client cannot lose
--- data.
---
--- The buffer is NEUTRALISED, not deleted, while the UI is up: the capture
--- pane is a popup mounted over this very buffer, so `nvim_buf_delete` here
--- closed the pane → WinClosed → full session teardown, killing the
--- auto-advance of spec 03 §6 (the E2E suite caught exactly that). A
--- `BufWriteCmd` REPLACES the write, so `:w`/`:wall` run the refusal instead
--- of re-creating the file. `ui.unmount()` still deletes plugin-created
--- buffers at teardown; deletion here is only for a buffer no window shows.
---
--- A modified buffer is left completely alone: `capture_is_saved` refused the
--- operation before dispatch, so reaching here modified means Matt typed
--- during the round-trip — his text is not ours to touch.
local function retire_capture_buffer(path)
  for _, buf in ipairs(buffers_for(path)) do
    if not vim.bo[buf].modified then
      pcall(vim.api.nvim_create_autocmd, "BufWriteCmd", {
        buffer = buf,
        desc = "para-organize: refuse to re-create an organized capture",
        callback = function()
          fail(
            ("this capture was already organized — %s no longer exists; writing would re-create it (:saveas to keep a copy)"):format(
              path
            )
          )
          return true
        end,
      })
      local mod = ui()
      local mounted = type(mod.is_mounted) == "function" and mod.is_mounted()
      if not mounted and #vim.fn.win_findbuf(buf) == 0 then
        pcall(vim.api.nvim_buf_delete, buf, { force = true })
      end
    end
  end
end

M._retire_capture_buffer = retire_capture_buffer

--- The rank (1-based) of `destination` among the suggestions on screen, or
--- nil when the destination did not come from the list (an explicit
--- `:ParaOrganize move <path>`, a picker, a freshly created folder).
local function rank_of(s, destination)
  for rank, suggestion in ipairs((s or {}).suggestions or {}) do
    if type(suggestion) == "table" and suggestion.path == destination then
      return rank
    end
  end
  return nil
end

--- Public because `integrate` records the same counterfactual for the same
--- reason merge does (spec 03 §6: an integration into a note THERE is a
--- decision for that folder).
M.rank_of = rank_of

--- Move the current capture to `destination` (spec 05 §2 via `op.move`).
function M.move(destination)
  local s = state()
  local record = M.current_capture()
  if not record then
    warn("no capture open")
    return
  end
  if type(destination) ~= "string" or destination == "" then
    warn("no destination given")
    return
  end
  if not capture_is_saved(record.path, "move") then
    return
  end
  if not begin_op(record.path) then
    return
  end
  local params = vim.tbl_extend("force", {
    path = record.path,
    destination = destination,
  }, M.decision_context({ chosen_rank = rank_of(s, destination) or M.NONE }))
  rpc("op.move", params, function(result, err)
    end_op()
    if err or op_failed(result, "move") then
      return
    end
    mark("processed", record.path)
    M.advance()
    retire_capture_buffer(record.path)
  end)
end

M.move_to = M.move

--- Is this suggestion row the archive destination?
---
--- The core reports the PARA type both singular and plural depending on the
--- surface (`render.TYPE_ALIASES` exists for exactly that), so both spellings
--- are accepted rather than one being guessed at.
---@param suggestion table|nil
---@return boolean
function M.is_archive_suggestion(suggestion)
  if type(suggestion) ~= "table" then
    return false
  end
  local kind = tostring(suggestion.type or ""):lower()
  return kind == "archive" or kind == "archives"
end

function M.archive()
  local record = M.current_capture()
  if not record then
    warn("no capture open")
    return
  end
  if not capture_is_saved(record.path, "archive") then
    return
  end
  if not begin_op(record.path) then
    return
  end
  -- Archiving with suggestions on screen IS the spec 12 §2 negative label —
  -- "a rejection is as much signal as an acceptance" — so the counterfactual
  -- is sent with NO chosen rank.
  local params = vim.tbl_extend(
    "force",
    { path = record.path },
    M.decision_context({ chosen_rank = M.NONE })
  )
  rpc("op.archive", params, function(result, err)
    end_op()
    if err or op_failed(result, "archive") then
      return
    end
    mark("processed", record.path)
    M.advance()
    retire_capture_buffer(record.path)
  end)
end

--- The render-map item under the organize pane's cursor, or nil when the
--- cursor is elsewhere (the capture pane, a picker, a programmatic call).
local function cursor_item()
  local mod = ui()
  if type(mod.item_at_cursor) ~= "function" then
    return nil
  end
  if type(mod.current_wins) == "function" then
    local ok_wins, wins = pcall(mod.current_wins)
    wins = ok_wins and wins or {}
    if wins.organize and vim.api.nvim_get_current_win() ~= wins.organize then
      return nil
    end
  end
  local ok, item = pcall(mod.item_at_cursor)
  return ok and item or nil
end

M._cursor_item = cursor_item

--- `<CR>` — spec 03 §3, resolved by the architect's ruling D (recorded in
--- ARCHITECTURE.md).
---
--- Spec 03 §3 contradicts itself: state 1 says "`<CR>` on a suggestion accepts
--- it" while the dispatch paragraph says `[P]/[A]/[R]/[D]` descend — and
--- suggestion lines carry exactly those markers. The resolution follows spec
--- 01's description of the pane ("ranked destination suggestions + browsable
--- PARA folder tree, scores visible"): state 1 renders BOTH sections, and
--- `<CR>` dispatches on the LINE KIND carried in the render map —
---
---   * `suggestion` ⇒ accept (move there),
---   * `root` / `dir` ⇒ descend into it (state 2),
---   * `file` ⇒ start a merge (state 3 / state 2's `[F]` lines).
---
--- Dispatch is on the map's `kind`, NEVER on the rendered prefix text: the
--- pre-rewrite code matched `^%[P%] ` against buffer lines, which is the 08
--- §A19 bug class (a re-themed icon silently changed what `<CR>` did).
---
--- Called with the cursor anywhere else — the capture pane, or
--- programmatically via `<Plug>(ParaOrganizeAccept)` — it keeps its
--- session-level meaning: accept the SELECTED suggestion.
function M.accept()
  local s = state()
  if not s then
    return
  end
  M.sync_selection_from_cursor()
  local view = s.view or "suggestions"
  if view == "integrate" then
    -- The review gate's accept half (spec 12 §1: "`<CR>`/`<leader>mc`
    -- applies"). Dispatched on the VIEW, never on the rendered text — the
    -- same 08 §A19 discipline `<CR>`'s line-kind dispatch already follows.
    local gate = integrate()
    if gate then
      gate.accept()
    end
    return
  end
  if view == "merge" then
    local keys = (cfg().keymaps or {}).buffer or {}
    notify(("press %s to complete the merge, %s to cancel"):format(keys.merge_complete or "<leader>mc", keys.merge_cancel or "<leader>mx"))
    return
  end

  local item = cursor_item()
  local kind = type(item) == "table" and item.kind or nil
  if kind == "root" or kind == "dir" then
    M.open_item(item)
    return
  end
  if kind == "file" then
    M.integrate_or_merge(item.path)
    return
  end

  if view == "suggestions" then
    local suggestion = (s.suggestions or {})[s.selected or 1]
    if not suggestion then
      warn("no suggestion selected")
      return
    end
    -- `<CR>` on the archive row IS `a`. Moving a capture INTO the archive
    -- root and archiving it are different operations — archiving files it
    -- under `archive_capture_path` with the archive layout, records the
    -- spec 12 §2 negative label, and is what the row means to the person
    -- pressing it. Anything else makes the same visible choice behave two
    -- ways depending on which key expressed it.
    if M.is_archive_suggestion(suggestion) then
      M.archive()
      return
    end
    M.move(suggestion.path)
    return
  end
  local entry = M.selected_entry()
  if not entry then
    warn("nothing selected")
    return
  end
  if entry.kind == "dir" then
    M.open_item(entry)
  else
    M.integrate_or_merge(entry.path)
  end
end

--- Put the capture into an EXISTING file, in whichever of spec 12 §1's three
--- edit modes governs that destination.
---
--- `merge_with` stays the `manual` mode's own entry point and is never
--- rerouted: `m` / `:ParaOrganize merge` mean "I will edit this myself", and
--- a key that sometimes summoned an LLM instead would be the 08 §A19 class of
--- surprise. Only the generic "open this note" gesture (`<CR>` on an `[F]`
--- line) consults the mode.
---
--- Degrades to `manual` whenever the mode is unknown — no `integrate` module,
--- no route table, an older core with no `routes.resolve`: the merge editor
--- is what this key did before spec 12 existed, and doing that silently is
--- the `op.skip` precedent for a passive surface.
function M.integrate_or_merge(target)
  local gate = integrate()
  if not gate then
    return M.merge_with(target)
  end
  return gate.dispatch(target)
end

-- --- merge (spec 03 §5) ----------------------------------------------------

--- `m`: merge via pickers (folder → note). Falls back to a prompt when the
--- pickers module is not loaded, so merge is never dead.
function M.merge()
  local record = M.current_capture()
  if not record then
    warn("no capture open")
    return
  end
  local ok, pickers = pcall(require, "para-organize.pickers")
  if ok and type(pickers) == "table" and type(pickers.open_folder_picker) == "function" then
    pickers.open_folder_picker(function(folder)
      if not folder then
        return
      end
      local folder_path = type(folder) == "table" and (folder.path or folder.value) or folder
      if type(pickers.open_folder_notes_picker) ~= "function" then
        warn("pickers.open_folder_notes_picker is unavailable")
        return
      end
      pickers.open_folder_notes_picker(folder_path, function(note)
        if not note then
          return
        end
        M.merge_with(type(note) == "table" and (note.path or note.value) or note)
      end)
    end)
    return
  end
  vim.ui.input({ prompt = "Merge into note: " }, function(target)
    if target and target ~= "" then
      M.merge_with(target)
    end
  end)
end

--- Both merge entry points converge here (spec 03 §5: ONE merge semantics).
function M.merge_with(target)
  local s = state()
  local record = M.current_capture()
  if not (s and record) then
    return
  end
  if type(target) ~= "string" or target == "" then
    warn("no merge target")
    return
  end
  local previous_view = s.view
  s.view = "loading"
  refresh()
  rpc("op.merge_preview", { path = record.path, target = target }, function(result, err)
    if err then
      s.view = previous_view or "suggestions"
      refresh()
      return
    end
    s.merge = {
      target = target,
      content = (result or {}).content or "",
      snapshot = (result or {}).snapshot,
      previous_view = previous_view or "suggestions",
    }
    s.view = "merge"
    refresh()
    ui().focus("organize")
  end)
end

--- Re-encode the core's opaque `snapshot` token WITHOUT losing precision.
---
--- `op.merge_preview` hands back `{path, mtime, sha256}` where `mtime` is a
--- full-precision float (`1786864908.3653965`). `vim.json.encode` emits Lua
--- numbers at `%.14g`, which drops the last significant digit — so sending
--- the decoded table straight back made `op.merge_commit` fail EVERY time
--- with ConcurrentModificationError ("mtime 1786864908.365396**5** ->
--- 1786864908.365396"), i.e. spec 03 §5 step 3 was unreachable and the `m`
--- keymap / `:ParaOrganize merge` were dead surfaces.
---
--- Numbers are therefore stringified at `%.17g` — 17 significant digits
--- round-trip any IEEE-754 double exactly — and the core's `float(...)`
--- coercion reconstructs the identical value. The client stays a faithful
--- echo of a token it does not interpret.
function M.wire_snapshot(snapshot)
  if type(snapshot) ~= "table" then
    return snapshot
  end
  local out = {}
  for key, value in pairs(snapshot) do
    if type(value) == "number" then
      out[key] = ("%.17g"):format(value)
    else
      out[key] = value
    end
  end
  return out
end

function M.merge_complete()
  local s = state()
  -- Spec 12 §1 gives the review gate the SAME completion/cancel keys as the
  -- merge editor ("`<CR>`/`<leader>mc` applies … `<leader>mx` rejects"), so
  -- the two share one pair of bindings and split on the view.
  if s and s.view == "integrate" then
    local gate = integrate()
    if gate then
      gate.accept()
    end
    return
  end
  local record = M.current_capture()
  if not (s and record and s.merge) then
    warn("not in a merge")
    return
  end
  local merge = s.merge
  local content = ui().merge_content(s)
  if not capture_is_saved(record.path, "merge") then
    return
  end
  if not begin_op(record.path) then
    return
  end
  -- The merge destination is the TARGET's folder, so that is what the
  -- counterfactual's chosen rank refers to (spec 03 §6: merge learns with
  -- "the target's folder").
  local destination = vim.fn.fnamemodify(merge.target, ":h")
  rpc("op.merge_commit", vim.tbl_extend("force", {
    path = record.path,
    target = merge.target,
    content = content,
    snapshot = M.wire_snapshot(merge.snapshot),
  }, M.decision_context({ chosen_rank = rank_of(s, destination) or M.NONE })), function(result, err)
    end_op()
    if err or op_failed(result, "merge") then
      return
    end
    s.merge = nil
    mark("processed", record.path)
    M.advance()
    retire_capture_buffer(record.path)
  end)
end

--- Cancel: right pane returns to the previous state, nothing written.
function M.merge_cancel()
  local s = state()
  -- In the review gate this key is REJECT, not cancel: spec 12 §1 names three
  -- verdicts and all three are recorded, because "a rejection is as much
  -- signal as an acceptance" (12 §2). There is deliberately no fourth,
  -- record-nothing exit.
  if s and s.view == "integrate" then
    local gate = integrate()
    if gate then
      gate.reject()
    end
    return
  end
  if not (s and s.merge) then
    warn("not in a merge")
    return
  end
  s.view = s.merge.previous_view or "suggestions"
  s.merge = nil
  refresh()
end

-- --- browse / search -------------------------------------------------------

function M.selected_entry()
  local s = state()
  if not s then
    return nil
  end
  local view = s.view or "suggestions"
  local index = s.selected or 1
  if view == "browse" then
    return (s.browse and s.browse.entries or {})[index]
  end
  if view == "search" then
    local record = (s.search and s.search.results or {})[index]
    if record then
      return { kind = "file", path = record.path, name = record.title or record.filename, item = record }
    end
  end
  return nil
end

--- Sync `state.selected` from the organize pane's cursor so `<CR>` acts on
--- the line Matt is actually looking at.
function M.sync_selection_from_cursor()
  local s = state()
  local mod = ui()
  if not (s and type(mod.item_at_cursor) == "function") then
    return
  end
  if vim.api.nvim_get_current_win() ~= (mod.current_wins() or {}).organize then
    return
  end
  local item = mod.item_at_cursor()
  if item and item.index then
    s.selected = item.index
  end
end

local function list_length(s)
  local view = s.view or "suggestions"
  if view == "browse" then
    return #((s.browse or {}).entries or {})
  end
  if view == "search" then
    return #((s.search or {}).results or {})
  end
  return #(s.suggestions or {})
end

function M.next_suggestion()
  local s = state()
  if not s then
    return
  end
  local total = list_length(s)
  if total == 0 then
    return
  end
  s.selected = math.min((s.selected or 1) + 1, total)
  refresh()
end

function M.prev_suggestion()
  local s = state()
  if not s then
    return
  end
  if list_length(s) == 0 then
    return
  end
  s.selected = math.max((s.selected or 1) - 1, 1)
  refresh()
end

--- One directory level as browse entries.
---
--- The core's `folder.children` answers `{dirs[], notes[]}` (server.py: "one
--- directory level for 03 §3 browsing"); `{entries[]}` and a bare list are
--- also accepted so a differently-shaped reply degrades instead of rendering
--- an empty folder.
---
--- `[F]` lines show `aliases[1]`, falling back to the title and then the
--- filename, exactly as spec 03 §3 state 2 specifies.
local function normalize_entries(payload)
  local entries = {}
  local list = payload
  if type(payload) == "table" and (payload.dirs or payload.notes) then
    for _, dir in ipairs(payload.dirs or {}) do
      if type(dir) == "table" then
        entries[#entries + 1] = {
          kind = "dir",
          path = dir.path,
          name = dir.name,
          display = dir.name or dir.path,
          description = dir.description,
        }
      end
    end
    for _, note in ipairs(payload.notes or {}) do
      if type(note) == "table" then
        local alias = type(note.aliases) == "table" and note.aliases[1] or nil
        local filename = type(note.path) == "string" and note.path:match("([^/]+)$") or nil
        entries[#entries + 1] = {
          kind = "file",
          path = note.path,
          name = filename,
          display = alias or note.title or filename or note.path,
          modified = note.modified,
        }
      end
    end
    return entries
  end
  if type(payload) == "table" and payload.entries then
    list = payload.entries
  end
  for _, raw in ipairs(list or {}) do
    if type(raw) == "table" then
      local kind = raw.kind or raw.type
      if kind == "directory" or kind == "folder" then
        kind = "dir"
      elseif kind ~= "dir" then
        kind = "file"
      end
      table.insert(entries, {
        kind = kind,
        path = raw.path,
        name = raw.name,
        display = raw.display or raw.alias or raw.name,
        modified = raw.modified or raw.mtime,
        description = raw.description,
      })
    end
  end
  return entries
end

--- Derive a folder's children from indexed records when the core has no
--- `folder.children` method. Still thin-client legal: the data comes from
--- `search.query` over RPC, not from a filesystem read.
function M._entries_from_records(path, records)
  local prefix = path:gsub("/+$", "") .. "/"
  local dirs, files, seen = {}, {}, {}
  for _, record in ipairs(records or {}) do
    local rpath = record.path or ""
    if rpath:sub(1, #prefix) == prefix then
      local rest = rpath:sub(#prefix + 1)
      local head = rest:match("^([^/]+)/")
      if head then
        if not seen[head] then
          seen[head] = true
          table.insert(dirs, { kind = "dir", path = prefix .. head, name = head, display = head })
        end
      else
        local alias = (type(record.aliases) == "table" and record.aliases[1]) or nil
        table.insert(files, {
          kind = "file",
          path = rpath,
          name = record.filename or rest,
          display = alias or record.title or record.filename or rest,
          modified = record.modified,
        })
      end
    end
  end
  local out = {}
  vim.list_extend(out, dirs)
  vim.list_extend(out, files)
  return out
end

--- `<CR>` on a folder line: descend (spec 03 §3 state 2).
function M.open_item(entry)
  local s = state()
  if not s then
    return
  end
  local path = type(entry) == "table" and entry.path or entry
  if type(path) ~= "string" or path == "" then
    return
  end
  local function show(entries)
    s.browse = s.browse or { stack = {} }
    s.browse.stack = s.browse.stack or {}
    if s.browse.path and s.browse.path ~= path then
      table.insert(s.browse.stack, s.browse.path)
    end
    s.browse.path = path
    s.browse.label = vim.fn.fnamemodify(path, ":t")
    s.browse.entries = entries
    s.view = "browse"
    s.selected = 1
    M.apply_sort()
    refresh()
  end
  -- `folder.children`, NOT `folder.list`: the latter ignores `path` entirely
  -- and answers with every PARA subfolder in the vault under a `folders` key,
  -- so browsing rendered "(empty folder)" for every directory. `folder.list`
  -- keeps its own job — the destination picker, where the `para_type`
  -- semantics are the right ones.
  rpc("folder.children", { path = path }, function(result, err)
    if err then
      rpc("search.query", { criteria = obj({}) }, function(records, fallback_err)
        if fallback_err then
          return
        end
        show(M._entries_from_records(path, records or {}))
      end)
      return
    end
    show(normalize_entries(result))
  end, { quiet = true })
end

--- `<BS>`: back to parent while browsing; from the root, back to suggestions.
function M.back_to_parent()
  local s = state()
  if not s then
    return
  end
  local browse = s.browse
  if not browse or not browse.path then
    s.view = "suggestions"
    s.selected = 1
    refresh()
    return
  end
  local parent = table.remove(browse.stack or {})
  if not parent then
    s.browse = nil
    s.view = "suggestions"
    s.selected = 1
    refresh()
    return
  end
  browse.path = nil
  M.open_item(parent)
end

--- `/`: inline destination search. Context-aware — scoped to the folder
--- currently browsed, else vault-wide (spec 03 §3 state 3).
function M.search()
  local s = state()
  if not s then
    return
  end
  vim.ui.input({ prompt = "Search: " }, function(query)
    if not query or query == "" then
      return
    end
    local scope = s.browse and s.browse.path or nil
    rpc("search.query", { criteria = { text = query } }, function(result, err)
      if err then
        return
      end
      local results = result or {}
      if scope then
        local prefix = scope:gsub("/+$", "") .. "/"
        local filtered = {}
        for _, record in ipairs(results) do
          if (record.path or ""):sub(1, #prefix) == prefix then
            table.insert(filtered, record)
          end
        end
        results = filtered
      end
      s.search = { query = query, results = results, scope = scope and vim.fn.fnamemodify(scope, ":t") or nil }
      s.view = "search"
      s.selected = 1
      refresh()
    end)
  end)
end

--- `:ParaOrganize search [query]` — spec 03 §2: "Open Telescope search picker
--- over the index."
---
--- Session-independent (searching the index needs no capture open), and the
--- counterpart to `M.search`, which is the `/` INLINE search of §3 state 3.
--- With no query it opens the saved-search picker, which is what makes spec
--- 03 §4's nine built-ins and `live_search` reachable at all.
---@param query string|nil
function M.search_picker(query)
  local ok, pickers = pcall(require, "para-organize.pickers")
  if not (ok and type(pickers) == "table") then
    fail("para-organize.pickers is unavailable — telescope is a required dependency (spec 03 §1)")
    return false
  end
  query = type(query) == "string" and vim.trim(query) or ""
  if query == "" then
    if type(pickers.open_saved_searches_picker) == "function" then
      return pickers.open_saved_searches_picker()
    end
  end
  if type(pickers.open_search_picker) ~= "function" then
    fail("pickers.open_search_picker is unavailable")
    return false
  end
  return pickers.open_search_picker(query)
end

--- `r`: re-run suggestion generation for the current capture.
function M.refresh_suggestions()
  local s = state()
  local record = M.current_capture()
  if not (s and record) then
    warn("no capture open")
    return
  end
  rpc("suggest.for_note", { path = record.path }, function(result, err)
    if err then
      return
    end
    s.suggestions = result or {}
    s.selected = 1
    s.view = "suggestions"
    refresh()
    notify(("%d suggestions"):format(#(s.suggestions or {})))
  end)
end

--- `p`: toggle the right pane's preview of the selected item.
function M.toggle_preview()
  local s = state()
  if not s then
    return
  end
  s.preview = not s.preview
  refresh()
end

-- --- sorting (spec 03 §3: `S` cycles the three modes) ----------------------

--- Reorder the SUGGESTIONS list (state 1) in the current sort mode.
---
--- The header renders "Suggestions — sort: <mode>" and `S` cycles the mode, so
--- the mode has to mean something here too; it used to reorder only
--- `browse.entries`, leaving a score-ordered list under an "Alphabetical"
--- header. `intelligent` is the core's own ranking (score descending), which
--- is the order the list arrives in.
local function sort_suggestions(s, mode)
  local suggestions = s.suggestions or {}
  if #suggestions < 2 then
    return
  end
  local chosen = suggestions[s.selected or 1]
  table.sort(suggestions, function(a, b)
    if mode == "alphabetical" then
      local an = tostring(a.name or a.path or ""):lower()
      local bn = tostring(b.name or b.path or ""):lower()
      if an ~= bn then
        return an < bn
      end
    end
    local sa = tonumber(a.score) or 0
    local sb = tonumber(b.score) or 0
    if sa ~= sb then
      return sa > sb
    end
    return tostring(a.path or "") < tostring(b.path or "")
  end)
  -- The selection follows the entry it was on, never a bare index.
  if chosen then
    for index, suggestion in ipairs(suggestions) do
      if suggestion == chosen then
        s.selected = index
        break
      end
    end
  end
end

function M.apply_sort()
  local s = state()
  if not s then
    return
  end
  local mode = s.sort or render.SORT_MODES[1]
  if (s.view or "suggestions") == "suggestions" then
    sort_suggestions(s, mode)
  end
  local browse = s.browse
  if not (browse and browse.entries) then
    return
  end
  local scores = {}
  for _, suggestion in ipairs(s.suggestions or {}) do
    if suggestion.path then
      scores[suggestion.path] = suggestion.score or 0
    end
  end
  local entries = browse.entries
  table.sort(entries, function(a, b)
    if mode == "intelligent" then
      local sa, sb = scores[a.path] or -math.huge, scores[b.path] or -math.huge
      if sa ~= sb then
        return sa > sb
      end
    end
    local a_dir = a.kind == "dir"
    local b_dir = b.kind == "dir"
    if a_dir ~= b_dir then
      return a_dir
    end
    if mode == "modified" then
      local ma, mb = tonumber(a.modified) or 0, tonumber(b.modified) or 0
      if ma ~= mb then
        return ma > mb
      end
    end
    local an = tostring(a.display or a.name or a.path or ""):lower()
    local bn = tostring(b.display or b.name or b.path or ""):lower()
    return an < bn
  end)
end

function M.sort_cycle()
  local s = state()
  if not s then
    return
  end
  local modes = render.SORT_MODES
  local index = 1
  for i, mode in ipairs(modes) do
    if mode == (s.sort or modes[1]) then
      index = i
      break
    end
  end
  s.sort = modes[(index % #modes) + 1]
  M.apply_sort()
  refresh()
  notify("sort: " .. (render.SORT_LABELS[s.sort] or s.sort))
end

-- --- folders ---------------------------------------------------------------

function M.new_folder(para_type, name)
  local function create(folder_name)
    rpc("folder.create", { para_type = para_type, name = folder_name }, function(result, err)
      if err or op_failed(result, "folder.create") then
        return
      end
      local destination = type(result) == "table"
        and (result.destination or (type(result.details) == "table" and result.details.path))
        or nil
      notify(("created %s/%s"):format(para_type, folder_name))
      if (cfg().ui or {}).auto_move_to_new_folder and destination and M.current_capture() then
        M.move(destination)
      else
        refresh()
      end
    end)
  end
  if type(name) == "string" and name ~= "" then
    create(name)
    return
  end
  vim.ui.input({ prompt = ("New %s name: "):format(para_type) }, function(value)
    if value and value ~= "" then
      create(value)
    end
  end)
end

function M.new_project(name)
  M.new_folder("projects", name)
end

function M.new_area(name)
  M.new_folder("areas", name)
end

function M.new_resource(name)
  M.new_folder("resources", name)
end

-- --- metadata editing (spec 07) -------------------------------------------

M._completions = {}

--- Completion source for `vim.ui.input`. Registered globally because
--- `completion = "customlist,…"` can only name a vimscript-visible function.
_G.__para_organize_meta_complete = function(arglead)
  local out = {}
  local lead = tostring(arglead or ""):lower()
  for _, value in ipairs(M._completions or {}) do
    local text = tostring(value)
    if lead == "" or text:lower():sub(1, #lead) == lead then
      table.insert(out, text)
    end
  end
  return out
end

--- Fetch the configured `metadata_fields` (with resolved completions) from
--- the core — spec 10 §3 puts them in core config so CLI and UI agree.
function M.load_meta_fields(done)
  local s = state()
  if s and s.meta_fields then
    if done then
      done(s.meta_fields)
    end
    return
  end
  rpc("meta.fields", {}, function(result, err)
    local fields = (not err and type(result) == "table" and result.fields) or {}
    if s and not err then
      s.meta_fields = fields
    end
    if done then
      done(fields)
    end
    refresh()
    M.rebind()
  end, { quiet = true })
end

function M.meta_field(key)
  local s = state()
  for _, field in ipairs((s and s.meta_fields) or {}) do
    if field.key == key then
      return field
    end
  end
  return nil
end

local function truthy(value)
  if type(value) == "boolean" then
    return value
  end
  if type(value) == "string" then
    local lowered = value:lower()
    return lowered == "true" or lowered == "yes" or lowered == "1"
  end
  if type(value) == "number" then
    return value ~= 0
  end
  return false
end

--- Prompt for one field's value, coerced to the field's declared type
--- (spec 07). The core re-coerces and normalises (kebab-casing, enum
--- membership) — this is the client half of a shared contract, never a
--- second source of truth.
function M._prompt_value(field, record, done)
  local kind = field.type or "string"
  local prompt = field.prompt or ("Set " .. field.key)

  if kind == "boolean" then
    done(not truthy(render.field_value(record, field.key)))
    return
  end

  if kind == "enum" then
    local values = field.values
    if type(values) ~= "table" or #values == 0 then
      values = field.completions
    end
    if type(values) ~= "table" or #values == 0 then
      fail(("%s is an enum with no configured values"):format(field.key))
      done(nil)
      return
    end
    vim.ui.select(values, { prompt = prompt }, function(choice)
      done(choice)
    end)
    return
  end

  local input_opts = { prompt = prompt .. ": " }
  local completions = field.completions
  if type(completions) == "table" and #completions > 0 then
    M._completions = completions
    input_opts.completion = "customlist,v:lua.__para_organize_meta_complete"
  end

  vim.ui.input(input_opts, function(input)
    M._completions = {}
    if input == nil then
      done(nil)
      return
    end
    input = vim.trim(input)
    if input == "" then
      done(nil)
      return
    end
    if kind == "list" then
      local out = {}
      for _, part in ipairs(vim.split(input, ",", { plain = true })) do
        part = vim.trim(part)
        if part ~= "" then
          table.insert(out, part)
        end
      end
      done(#out > 0 and out or nil)
      return
    end
    if kind == "number" then
      local number = tonumber(input)
      if not number then
        fail(("%s must be a number (got %q)"):format(field.key, input))
        done(nil)
        return
      end
      done(number)
      return
    end
    done(input)
  end)
end

--- Set one configured metadata field on the current capture via `meta.set`.
function M.set_meta_field(key)
  local s = state()
  local record = M.current_capture()
  if not (s and record) then
    warn("no capture open")
    return
  end
  M.load_meta_fields(function(fields)
    local field
    for _, candidate in ipairs(fields or {}) do
      if candidate.key == key then
        field = candidate
        break
      end
    end
    field = field or { key = key, type = "string" }
    M._prompt_value(field, record, function(value)
      if value == nil then
        return
      end
      if not capture_is_saved(record.path, "meta.set") then
        return
      end
      -- Annotating is not filing: the session is recorded (spec 12 §3 groups a
      -- session's records by `session_id`) but nothing was CHOSEN, so neither
      -- the counterfactual nor a rank belongs on this record.
      local params = vim.tbl_extend("force", {
        path = record.path,
        changes = { [field.key] = value },
      }, M.decision_context({ chosen_rank = M.NONE, suggestions_shown = M.NONE }))
      rpc("meta.set", params, function(result, err)
        if err or op_failed(result, "meta.set") then
          return
        end
        -- Re-read through the core so the metadata summary reflects what
        -- was actually written (the plugin never parses the file itself).
        rpc("note.get", { path = record.path }, function(fetched, fetch_err)
          if not fetch_err and fetched and fetched.record and s.captures then
            local merged = vim.tbl_extend("force", record, fetched.record)
            merged.frontmatter = fetched.frontmatter or {}
            s.captures[s.current or 1] = merged
          end
          refresh()
        end)
      end)
    end)
  end)
end

-- --- lifecycle -------------------------------------------------------------

--- `?`: help popup, generated from the live keymap table (spec 03 §2).
function M.help()
  ui().show_help(M.keymap_table())
end

--- `<Esc>` / `:ParaOrganize stop`: close the UI from either pane.
---
--- Also discards the session so no orphan state survives an `<Esc>`
--- (spec 09 §2 "UI teardown on every exit path"). `init.teardown()` is a
--- no-op when the integrator's state module holds no session, which is
--- exactly the case in the unit specs' injected-state world.
function M.quit()
  -- The review gate's `e` is a buffer-local keymap this module's sibling
  -- installed; teardown on EVERY exit path (spec 09 §2) includes it.
  local gate = integrate()
  if gate then
    pcall(gate.reset)
  end
  ui().unmount()
  pcall(function()
    require("para-organize").teardown()
  end)
end

M.cancel = M.quit

--- `:ParaOrganize stop` (spec 03 §2: "Close UI, discard session").
---
--- Routed to the composition root rather than aliased to `quit` so the
--- session-summary notification ("session closed — processed N, skipped M")
--- is actually reachable; as an alias to the deliberately-quiet `<Esc>` path
--- that branch was dead code. `<Esc>` keeps `M.quit`.
function M.stop()
  local ok, root = pcall(require, "para-organize")
  if ok and type(root) == "table" and type(root.stop) == "function" then
    ui().unmount()
    return root.stop()
  end
  return M.quit()
end

-- --- the `commands.action_names()` contract --------------------------------
--
-- `commands.lua` and `:checkhealth` resolve every subcommand and keymap to a
-- function on THIS module by name (health asserts it, so a rename surfaces at
-- health time and not at the first keypress). These are the names spec 03 §2
-- documents; the descriptive names above stay as the implementation.

--- `:ParaOrganize start [k=v …]` — delegated to the composition root, which
--- owns the core connection and the session state machine.
function M.start(filters)
  return require("para-organize").start(filters)
end

--- `:ParaOrganize reindex` (spec 03 §2).
function M.reindex()
  return require("para-organize").reindex()
end

--- `:ParaOrganize debug` (spec 03 §2).
function M.debug()
  return require("para-organize").debug()
end

M.next = M.next_capture
M.prev = M.prev_capture
M.previous = M.prev_capture
M.refresh = M.refresh_suggestions
M.set_meta = M.set_meta_field

function M.focus_capture()
  ui().focus("capture")
end

function M.focus_organize()
  ui().focus("organize")
end

-- --- the keymap table (spec 03 §3) ----------------------------------------

--- Every row of spec 03's keymap table, in the order it is documented.
--- `navigation` marks the pane/session keys kept when
--- `ui.capture_pane_keymaps = "navigation"`.
M.CORE_KEYS = {
  { name = "accept", default = "<CR>", desc = "Accept selection / open item", panes = { "capture", "organize" }, fn = function() M.accept() end },
  { name = "cancel", default = "<Esc>", desc = "Close UI", panes = { "capture", "organize" }, navigation = true, fn = function() M.quit() end },
  { name = "next", default = "<Tab>", desc = "Next capture", panes = { "capture", "organize" }, navigation = true, fn = function() M.next_capture() end },
  { name = "prev", default = "<S-Tab>", desc = "Previous capture", panes = { "capture", "organize" }, navigation = true, fn = function() M.prev_capture() end },
  { name = "skip", default = "s", desc = "Skip capture", panes = { "capture", "organize" }, fn = function() M.skip() end },
  { name = "sort_cycle", default = "S", desc = "Cycle sort mode", panes = { "capture", "organize" }, fn = function() M.sort_cycle() end },
  { name = "archive", default = "a", desc = "Archive capture now", panes = { "capture", "organize" }, fn = function() M.archive() end },
  { name = "merge", default = "m", desc = "Merge via pickers", panes = { "capture", "organize" }, fn = function() M.merge() end },
  { name = "search", default = "/", desc = "Inline destination search", panes = { "capture", "organize" }, fn = function() M.search() end },
  { name = "refresh", default = "r", desc = "Regenerate suggestions", panes = { "capture", "organize" }, fn = function() M.refresh_suggestions() end },
  { name = "toggle_preview", default = "p", desc = "Toggle preview of selection", panes = { "capture", "organize" }, fn = function() M.toggle_preview() end },
  { name = "help", default = "?", desc = "Help popup", panes = { "capture", "organize" }, navigation = true, fn = function() M.help() end },
  { name = "next_suggestion", default = "<A-j>", desc = "Next suggestion", panes = { "capture", "organize" }, navigation = true, fn = function() M.next_suggestion() end },
  { name = "prev_suggestion", default = "<A-k>", desc = "Previous suggestion", panes = { "capture", "organize" }, navigation = true, fn = function() M.prev_suggestion() end },
  { name = "focus_capture", default = "<C-h>", desc = "Focus capture pane", panes = { "capture", "organize" }, navigation = true, fn = function() M.focus_capture() end },
  { name = "focus_organize", default = "<C-l>", desc = "Focus organize pane", panes = { "capture", "organize" }, navigation = true, fn = function() M.focus_organize() end },
  { name = "back", default = "<BS>", desc = "Back to parent folder", panes = { "organize" }, fn = function() M.back_to_parent() end },
  { name = "new_project", default = "<leader>np", desc = "New project folder", panes = { "capture", "organize" }, fn = function() M.new_project() end },
  { name = "new_area", default = "<leader>na", desc = "New area folder", panes = { "capture", "organize" }, fn = function() M.new_area() end },
  { name = "new_resource", default = "<leader>nr", desc = "New resource folder", panes = { "capture", "organize" }, fn = function() M.new_resource() end },
  { name = "merge_complete", default = "<leader>mc", desc = "Complete merge / accept proposal", panes = { "organize" }, fn = function() M.merge_complete() end },
  { name = "merge_cancel", default = "<leader>mx", desc = "Cancel merge / reject proposal", panes = { "organize" }, fn = function() M.merge_cancel() end },
  -- Spec 12 §1's edit modes. Both are organize-pane only: they act on the
  -- DESTINATION under the cursor, which only exists in that pane.
  { name = "integrate", default = "<leader>mi", desc = "Integrate capture into this note", panes = { "organize" }, fn = function()
    local gate = integrate()
    if gate then gate.integrate_selected() end
  end },
  { name = "integrate_mode", default = "<leader>mm", desc = "Choose edit mode for this note", panes = { "organize" }, fn = function()
    local gate = integrate()
    if gate then gate.choose_mode_selected() end
  end },
  -- VIEW-SCOPED: bound by `integrate.bind_gate()` only while a proposal is on
  -- screen, and unbound when the gate closes. `e` in the organize pane is
  -- otherwise plain cursor motion, and shadowing it permanently would be a
  -- keymap change spec 03 never asked for. It stays in this table so the `?`
  -- overlay lists it — spec 03 §2 requires the help to be generated from the
  -- real table, and a key Matt can press that help never mentions is exactly
  -- the hand-maintained-docs failure that rule exists to prevent.
  { name = "integrate_edit", default = "e", desc = "Edit the proposed diff (review gate)", panes = { "organize" }, view = "integrate", fn = function()
    local gate = integrate()
    if gate then gate.edit() end
  end },
  -- Spec 15 §2. ⚠ `panes = { "organize" }` and `navigation = false` are
  -- ruling R7, and they are load-bearing rather than a preference: with `zi`
  -- bound in the organize pane ONLY, `z` is not a prefix in the capture pane
  -- at all, so `zo`/`za` there stay instant and 15 §3's fold-recovery claim
  -- is true as written. The action itself is pure presentation, so it lives
  -- in `ui`; this row is only the binding.
  { name = "cycle_fields", default = "zi", desc = "Cycle capture fields (compact/full/raw)", panes = { "organize" }, navigation = false, fn = function()
    ui().cycle_fields()
  end },
}

--- The resolved keymap table: core rows + one row per configured
--- `metadata_fields` entry (spec 07: "the `?` help overlay lists metadata
--- keymaps alongside core ones").
function M.keymap_table()
  local configured = ((cfg() or {}).keymaps or {}).buffer or {}
  local entries = {}
  for _, row in ipairs(M.CORE_KEYS) do
    local lhs = configured[row.name]
    if lhs == nil then
      lhs = row.default
    end
    if lhs and lhs ~= "" then
      table.insert(entries, {
        name = row.name,
        lhs = lhs,
        desc = row.desc,
        panes = row.panes,
        navigation = row.navigation,
        view = row.view,
        group = row.view and "Review gate" or "Organize",
        fn = row.fn,
      })
    end
  end
  local s = state()
  for _, field in ipairs((s and s.meta_fields) or {}) do
    if field.keymap and field.keymap ~= "" then
      local key = field.key
      table.insert(entries, {
        name = "meta:" .. key,
        lhs = field.keymap,
        desc = ("Set %s (%s)"):format(key, field.type or "string"),
        -- Spec 07 scopes these to "the organize UI"; keeping them off the
        -- capture buffer preserves `i`/`c`/`o` for real editing there.
        panes = { "organize" },
        group = "Metadata",
        fn = function()
          M.set_meta_field(key)
        end,
      })
    end
  end
  return entries
end

--- Collisions between core keys and spec-07 metadata keys (07 acceptance
--- test 4). Reported here; the integrator-owned config validator raises.
function M.detect_collisions()
  local seen, conflicts = {}, {}
  for _, entry in ipairs(M.keymap_table()) do
    for _, pane in ipairs(entry.panes) do
      local id = pane .. " " .. entry.lhs
      if seen[id] and seen[id] ~= entry.name then
        table.insert(conflicts, { lhs = entry.lhs, pane = pane, first = seen[id], second = entry.name })
      else
        seen[id] = entry.name
      end
    end
  end
  return conflicts
end

--- The only rows that stay bound while the organize pane holds EDITABLE text
--- (the merge editor, the review gate's edit mode).
---
--- Everything else shadows a normal-mode editing command in a buffer the user
--- is being asked to edit: `s` substitute, `a` append, `p` paste, `r` replace,
--- `S` change-line, `/` search, `?` search-backwards — and `<Esc>` closed the
--- whole session mid-edit. Both survivors are `<leader>`-prefixed sequences,
--- which shadow nothing, and `:w` (routed through `BufWriteCmd`) is the third
--- way out.
M.EDITABLE_KEEP = { merge_complete = true, merge_cancel = true }

--- Bind the buffer-local keymaps for one pane.
---@param opts table|nil { editable = boolean } — the organize pane is holding
---       editable text, so the action keymaps step aside (see EDITABLE_KEEP).
function M.bind(bufnr, pane, opts)
  if not (bufnr and vim.api.nvim_buf_is_valid(bufnr)) then
    return false
  end
  local editable = type(opts) == "table" and opts.editable == true
  -- Rebinding must be able to REMOVE what a previous view bound, or entering
  -- the merge editor would keep every shadowing map from the suggestions view.
  for _, entry in ipairs(M.keymap_table()) do
    pcall(vim.keymap.del, "n", entry.lhs, { buffer = bufnr })
  end
  local mode = ((cfg() or {}).ui or {}).capture_pane_keymaps or "core"
  for _, entry in ipairs(M.keymap_table()) do
    -- View-scoped rows (the spec 12 §1 review gate) are bound and unbound by
    -- the module that owns the view, not by the pane-wide binder.
    local wanted = entry.view == nil and vim.tbl_contains(entry.panes, pane)
    if wanted and pane == "capture" then
      if mode == "none" then
        wanted = false
      elseif mode == "navigation" then
        wanted = entry.navigation == true
      end
    end
    if wanted and editable then
      wanted = M.EDITABLE_KEEP[entry.name] == true
    end
    if wanted then
      pcall(vim.keymap.set, "n", entry.lhs, entry.fn, {
        buffer = bufnr,
        nowait = true,
        silent = true,
        desc = "para-organize: " .. entry.desc,
      })
    end
  end
  for _, conflict in ipairs(M.detect_collisions()) do
    warn(("keymap collision: %s is bound to both %s and %s in the %s pane"):format(conflict.lhs, conflict.first, conflict.second, conflict.pane))
  end
  return true
end

--- Bind both panes (called by `ui.mount`).
---@param bufs table|nil
---@param opts table|nil { editable = boolean } — applies to the ORGANIZE pane
---       only; the capture pane is always the real note and keeps its rules.
function M.attach(bufs, opts)
  bufs = bufs or ui().current_bufs()
  M.bind(bufs.capture, "capture")
  M.bind(bufs.organize, "organize", opts)
end

--- Re-bind after the metadata field list arrives from the core.
function M.rebind()
  local mod = ui()
  if type(mod.is_mounted) == "function" and mod.is_mounted() then
    M.attach(mod.current_bufs())
  end
end

return M
