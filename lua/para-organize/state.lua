--- para-organize.state — the explicit session state machine (spec 09 §2).
---
--- > "Explicit session state machine (idle → session{captures, index,
--- >  current, processed, skipped} → closed) with UI teardown on every exit
--- >  path (WinClosed, `<Esc>`, `stop`)."  — spec 09 §2
---
--- The session table is a PLAIN table, deliberately: `ui`, `ui/render` and
--- `actions` read and write its fields directly, and every rendering rule in
--- spec 03 §3 is a pure function of it. This module owns its SHAPE, its
--- lifecycle and its change notification — not its mutation.
---
--- Nothing here touches the vault, the index or the core. `captures` arrives
--- from `session.start` over RPC; `suggestions` from `suggest.for_note`.
---
--- Owned by the INTEGRATOR.

local M = {}

--- The four states of the machine. `browsing`/`merging` are VIEWS of the
--- `session` state, not states of their own — see `session.view`.
M.IDLE = "idle"
M.SESSION = "session"
M.CLOSED = "closed"

--- Every value `session.view` may hold (spec 03 §3's four right-pane states,
--- plus the two transient ones the async load needs).
M.VIEWS = { "loading", "suggestions", "browse", "search", "merge", "empty" }

--- Spec 03 §3 state 2: "Sort cycles with S between Alphabetical, Last
--- Modified, Intelligent Suggestions".
M.SORT_MODES = { "alphabetical", "modified", "intelligent" }

---------------------------------------------------------------------------
-- listeners
---------------------------------------------------------------------------

M._listeners = {}

--- Subscribe to session mutations. `actions` calls `session.emit(session)`
--- after every mutation, which lands here.
---@param fn fun(session:table)
---@return fun() unsubscribe
function M.on(fn)
  assert(type(fn) == "function", "state.on expects a function")
  table.insert(M._listeners, fn)
  return function()
    for i, listener in ipairs(M._listeners) do
      if listener == fn then
        table.remove(M._listeners, i)
        return
      end
    end
  end
end

--- Notify every listener. Never raises: a broken listener must not be able
--- to break an organize action mid-flight.
function M.emit(session)
  session = session or M.session
  for _, listener in ipairs(M._listeners) do
    pcall(listener, session)
  end
end

function M.clear_listeners()
  M._listeners = {}
end

---------------------------------------------------------------------------
-- the session
---------------------------------------------------------------------------

--- The current session, or nil when idle. `commands.has_session()` reads
--- this field directly as its fallback, so it must stay a real field.
M.session = nil

M.state = M.IDLE

--- Build a session table. Pure — does not install it.
---
--- Field contract (the seam ui+actions read and write):
---   captures     NoteRecord-ish list; every entry needs at least `.path`
---   current      1-based index into `captures`
---   processed    list of paths that reached a terminal outcome
---   skipped      list of paths skipped this session
---   suggestions  core `Suggestion` dicts for the current capture
---   view         one of `M.VIEWS`
---   selected     1-based index into whichever list the view renders
---   sort         one of `M.SORT_MODES`
---   preview      bool — `p` toggles the right pane's preview
---   browse       { path, label, stack[], entries[] } | nil
---   search       { query, scope, results[] } | nil
---   merge        { target, content, snapshot, previous_view } | nil
---   meta_fields  cached `meta.fields` payload (spec 07)
---@param opts table|nil { captures, session_id, filters, counts }
---@return table session
function M.new(opts)
  opts = opts or {}
  local session = {
    session_id = opts.session_id,
    filters = opts.filters or {},
    captures = opts.captures or {},
    current = 1,
    processed = {},
    skipped = {},
    suggestions = {},
    view = "loading",
    selected = 1,
    -- "Intelligent Suggestions" — the core's own ranking, which is the order
    -- `suggest.for_note` hands the list back in. Starting at "alphabetical"
    -- rendered "sort: Alphabetical" over a score-ordered list, and made the
    -- first `S` skip alphabetical entirely.
    sort = "intelligent",
    preview = false,
    browse = nil,
    search = nil,
    merge = nil,
    meta_fields = nil,
    started_at = os.time(),
    counts = opts.counts,
  }
  -- `actions.refresh()` calls this after every mutation when present. Bound
  -- to the module (not to `session`) so a session detached from the module
  -- still notifies the same listeners.
  session.emit = function(s)
    M.emit(s or session)
  end
  return session
end

--- Install a session and enter the `session` state.
---@param opts table|nil either a session built by `M.new` or `M.new`'s opts
---@return table session
function M.start(opts)
  local session = opts
  if type(session) ~= "table" or session.captures == nil or session.emit == nil then
    session = M.new(opts)
  end
  M.session = session
  M.state = M.SESSION
  M.emit(session)
  return session
end

--- Alias: install an already-built session table.
function M.set(session)
  return M.start(session)
end

---@return table|nil
function M.get()
  return M.session
end

--- The predicate `commands.lua` prefers (its seam request). Never raises and
--- never guesses: no session table ⇒ false, full stop.
---@return boolean
function M.has_session()
  return M.session ~= nil
end

--- Leave the session. Idempotent; safe to call from every teardown path
--- (`stop`, `<Esc>`, WinClosed) — spec 09 §2.
---@return table|nil the session that was discarded
function M.stop()
  local previous = M.session
  M.session = nil
  M.state = M.CLOSED
  if previous then
    M.emit(nil)
  end
  return previous
end

M.close = M.stop

--- Full reset, including listeners. Tests and `setup()` re-runs.
function M.reset()
  M.session = nil
  M.state = M.IDLE
  M._listeners = {}
end

---------------------------------------------------------------------------
-- derived reads (no mutation)
---------------------------------------------------------------------------

--- The capture the UI is showing, or nil.
function M.current_capture(session)
  session = session or M.session
  if not session then
    return nil
  end
  return (session.captures or {})[session.current or 1]
end

--- `processed`/`skipped` are lists here, but `actions.mark` tolerates a
--- counter too — so every reader must. One helper, both shapes.
function M.count(value)
  if type(value) == "number" then
    return value
  end
  if type(value) == "table" then
    return #value
  end
  return 0
end

--- `{ total, current, processed, skipped, remaining }` for the completion
--- notice of spec 03 §6.
function M.counts(session)
  session = session or M.session
  if not session then
    return { total = 0, current = 0, processed = 0, skipped = 0, remaining = 0 }
  end
  local total = #(session.captures or {})
  local processed = M.count(session.processed)
  local skipped = M.count(session.skipped)
  return {
    total = total,
    current = session.current or 1,
    processed = processed,
    skipped = skipped,
    remaining = math.max(total - processed - skipped, 0),
  }
end

--- A flat, printable snapshot for `:ParaOrganize debug` (spec 03 §2).
function M.describe(session)
  session = session or M.session
  if not session then
    return { state = M.state, active = false }
  end
  local counts = M.counts(session)
  local record = M.current_capture(session)
  return {
    state = M.state,
    active = true,
    session_id = session.session_id,
    filters = session.filters,
    view = session.view,
    sort = session.sort,
    selected = session.selected,
    suggestions = #(session.suggestions or {}),
    meta_fields = #(session.meta_fields or {}),
    current_path = record and record.path or nil,
    total = counts.total,
    current = counts.current,
    processed = counts.processed,
    skipped = counts.skipped,
    remaining = counts.remaining,
  }
end

return M
