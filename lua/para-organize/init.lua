--- para-organize.nvim — the thin UI client for organize-core (spec 10).
---
--- This module is the composition root: it validates configuration, owns the
--- session lifecycle (`start` → `stop`) and wires the five independent
--- modules together. It performs NO vault work of its own — spec 10 §1:
--- "every state change is an RPC call; it never touches vault files itself".
---
--- Load order law (spec 09 §2): "Load persisted state at `setup()`, not at
--- `require()` time; no side effects on require." Requiring this module
--- defines functions and nothing else — no sockets, no processes, no reads.
---
--- Owned by the INTEGRATOR.

local M = {}

--- Spec 03 §1: "version string `0.1.0` in `init.lua`".
M.VERSION = "0.1.0"

--- The API major version this client speaks (spec 10 §2). `rpc.lua` refuses
--- a core whose major differs; this constant is what health reports.
M.API_VERSION = 1

local function config()
  return require("para-organize.config")
end

local function state()
  return require("para-organize.state")
end

local function notify(message, level)
  vim.notify("para-organize: " .. tostring(message), level or vim.log.levels.INFO)
end

local function fail(message)
  notify(message, vim.log.levels.ERROR)
end

--- `vim.json.encode({})` emits `[]` and the core rejects an array where an
--- object is required (-32602) BEFORE method lookup. `rpc.lua` normalises the
--- TOP-LEVEL params table only, so every NESTED empty object must be built
--- here. (Recorded in ARCHITECTURE.md "Wire fact".)
local function obj(t)
  if type(t) ~= "table" or next(t) == nil then
    return vim.empty_dict()
  end
  return t
end

---------------------------------------------------------------------------
-- setup
---------------------------------------------------------------------------

--- Configure the plugin.
---
--- `opts` is the UI-ONLY table of spec 10 §3 — layout, icons, highlights,
--- keymap overrides, plus `socket_path`/`core_cmd`. Anything behavioral
--- (vault paths, suggestion weights, metadata fields, routes) belongs in
--- `~/.config/organize-core/config.toml`; passing it here RAISES, naming the
--- key and its new home (`doc/MIGRATION-from-old-setup.md`).
---@param opts table|nil
---@return table resolved config
function M.setup(opts)
  if vim.fn.has("nvim-0.9") == 0 then
    error("para-organize.nvim requires Neovim 0.9 or newer (spec 03 §1)", 0)
  end

  -- Applied first so `ui`/`actions` resolve against the new table; rolled
  -- back below if a later gate rejects it, so a failed setup() never leaves
  -- a half-applied config behind.
  local previous = config().user()
  local cfg = config().setup(opts)

  local function reject(message)
    pcall(config().setup, previous)
    local ok_ui_rollback, ui_rollback = pcall(require, "para-organize.ui")
    if ok_ui_rollback then
      pcall(ui_rollback.setup, config().get())
    end
    error(message, 0)
  end

  -- The UI caches its own merged view; refresh it so a second setup() call
  -- cannot leave stale layout/keymaps behind.
  local ok_ui, ui = pcall(require, "para-organize.ui")
  if ok_ui then
    ui.setup(cfg)
  end

  -- Spec 07 acceptance test 4: a keymap collision is a SETUP-TIME error, not
  -- a surprise at the first keypress. `actions.detect_collisions()` reads the
  -- live resolved table (core rows + any known metadata rows) and only warns;
  -- raising is this module's job.
  local ok_actions, actions = pcall(require, "para-organize.actions")
  if ok_actions and type(actions.detect_collisions) == "function" then
    local conflicts = actions.detect_collisions()
    if #conflicts > 0 then
      local lines = {}
      for _, conflict in ipairs(conflicts) do
        lines[#lines + 1] = ("  %q is bound to both `%s` and `%s` in the %s pane"):format(
          conflict.lhs,
          conflict.first,
          conflict.second,
          conflict.pane
        )
      end
      reject(
        ("para-organize: invalid setup(): keymap collision\n%s\n  rebind one of them via keymaps.buffer, or change the field's keymap in ~/.config/organize-core/config.toml (spec 07)"):format(
          table.concat(lines, "\n")
        )
      )
    end
  end

  return cfg
end

--- The resolved config (defaults before `setup()` runs).
function M.config()
  return config().get()
end

---------------------------------------------------------------------------
-- the core connection
---------------------------------------------------------------------------

--- Connect to organize-core, spawning it if needed (spec 10 §1).
---@return table|nil client, table|string|nil err
function M.client()
  local ok, core = pcall(require, "para-organize.core")
  if not ok or type(core) ~= "table" then
    return nil, "para-organize.core is unavailable"
  end
  return core.ensure_running(config().core_options())
end

--- `M.client()` + one clear notification on failure (never a traceback).
---@return table|nil client
function M.require_client()
  local client, err = M.client()
  if not client then
    local ok, rpc = pcall(require, "para-organize.rpc")
    if ok and type(rpc.format_error) == "function" then
      vim.notify(rpc.format_error(err), vim.log.levels.ERROR)
    else
      fail(tostring(err))
    end
    return nil
  end
  return client
end

---------------------------------------------------------------------------
-- session lifecycle
---------------------------------------------------------------------------

--- Begin a session over the captures matching `filters` (spec 03 §2).
---
--- Zero matches ⇒ notify "No captures found matching filters" and DO NOT
--- open the UI (spec 03 §2, verbatim).
---@param filters table|nil canonicalized `k=v` filters from `commands.parse`
---@return boolean started
function M.start(filters)
  local client = M.require_client()
  if not client then
    return false
  end

  local st = state()
  if st.has_session() then
    -- Restarting is legitimate (different filters); tear the old one down
    -- first so no window/autocmd survives (spec 09 §2).
    M.stop({ quiet = true })
  end

  local params = {}
  if type(filters) == "table" and next(filters) ~= nil then
    params.filters = filters
  end

  client:request("session.start", params, function(err, result)
    if err then
      local ok, rpc = pcall(require, "para-organize.rpc")
      vim.notify(
        ok and rpc.format_error(err) or ("para-organize: session.start failed: " .. tostring(err)),
        vim.log.levels.ERROR
      )
      return
    end

    local captures = (result or {}).captures or {}
    if #captures == 0 then
      notify("No captures found matching filters", vim.log.levels.WARN)
      return
    end

    local session = st.start({
      captures = captures,
      session_id = (result or {}).session_id,
      filters = filters or {},
      counts = (result or {}).counts,
    })

    M.open(session, client)
  end)

  return true
end

--- Mount the UI over `session` and load capture 1. Split out so a test (and
--- the E2E gate) can drive the render half without a second `session.start`.
---@param session table
---@param client table
function M.open(session, client)
  local cfg = config().get()
  local ui = require("para-organize.ui")
  local actions = require("para-organize.actions")

  ui.setup(cfg)
  actions.setup({ client = client, state = session, config = cfg })

  local handles = ui.mount(session)
  if not handles then
    -- ui.mount already notified (missing nui, no room for the layout…).
    state().stop()
    return false
  end

  actions.load_current()
  return true
end

--- Close the UI and discard the session. "Files already processed stay
--- processed" (spec 03 §2) — nothing is rolled back, and the CORE keeps
--- running (other clients and the automation timer share it).
---@param opts table|nil { quiet = boolean }
---@return boolean stopped
function M.stop(opts)
  opts = opts or {}
  local st = state()
  local had_session = st.has_session()

  local ok_ui, ui = pcall(require, "para-organize.ui")
  if ok_ui and type(ui.is_mounted) == "function" and ui.is_mounted() then
    ui.unmount()
  end

  local ok_actions, actions = pcall(require, "para-organize.actions")
  if ok_actions and type(actions.set_state) == "function" then
    actions.set_state(nil)
  end

  st.stop()

  if had_session and not opts.quiet then
    local counts = st.counts(nil)
    notify(("session closed — processed %d, skipped %d"):format(counts.processed, counts.skipped))
  end
  return had_session
end

--- Called by `actions.quit()` (the `<Esc>` / WinClosed path) so every exit
--- route lands in the same teardown. Quiet: the UI is already gone.
function M.teardown()
  local st = state()
  if not st.has_session() then
    return false
  end
  st.stop()
  local ok, actions = pcall(require, "para-organize.actions")
  if ok and type(actions.set_state) == "function" then
    actions.set_state(nil)
  end
  return true
end

---------------------------------------------------------------------------
-- standalone commands
---------------------------------------------------------------------------

--- `:ParaOrganize reindex` — full rebuild, reporting `{total, duration}`
--- (spec 03 §2).
function M.reindex()
  local client = M.require_client()
  if not client then
    return false
  end
  notify("reindexing…")
  client:request("index.reindex", {}, function(err, result)
    if err then
      local ok, rpc = pcall(require, "para-organize.rpc")
      vim.notify(
        ok and rpc.format_error(err) or ("para-organize: reindex failed: " .. tostring(err)),
        vim.log.levels.ERROR
      )
      return
    end
    local total = tonumber((result or {}).total) or 0
    local duration = tonumber((result or {}).duration)
    notify(("reindexed %d notes%s"):format(total, duration and (" in %.2fs"):format(duration) or ""))
    local ok_pickers, pickers = pcall(require, "para-organize.pickers")
    if ok_pickers and type(pickers.invalidate_cache) == "function" then
      pickers.invalidate_cache()
    end
  end)
  return true
end

--- `:ParaOrganize debug` — diagnostics (spec 03 §2): config paths and their
--- existence, index stats, capture detection counts, session state.
---
--- Everything vault-side is ASKED OF THE CORE rather than re-derived, for the
--- same reason `:checkhealth` relays `organize health` (spec 10 §1).
---@return string[] the lines it reported (so tests can assert on them)
function M.debug()
  local cfg = config().get()
  local core_opts = config().core_options(cfg)
  local socket = config().socket_path(cfg)
  local lines = {
    "para-organize " .. M.VERSION .. " (apiVersion " .. M.API_VERSION .. ")",
    "config source: " .. (config().is_configured() and "setup()" or "defaults (setup() has not run)"),
  }

  if socket then
    local stat = vim.uv.fs_stat(socket)
    lines[#lines + 1] = ("socket_path: %s (%d bytes, %s)"):format(
      socket,
      #socket,
      stat and stat.type or "absent"
    )
  else
    lines[#lines + 1] = "socket_path: (unset)"
  end
  local cmd = core_opts.core_cmd or { "organize", "serve" }
  if type(cmd) == "string" then
    cmd = { cmd }
  end
  lines[#lines + 1] = ("core_cmd: %s (%s)"):format(
    table.concat(cmd, " "),
    vim.fn.executable(cmd[1]) == 1 and vim.fn.exepath(cmd[1]) or "NOT on $PATH"
  )

  local snapshot = state().describe()
  if snapshot.active then
    lines[#lines + 1] = ("session: %s — capture %d/%d, view %s, sort %s, %d suggestions"):format(
      tostring(snapshot.session_id),
      snapshot.current,
      snapshot.total,
      tostring(snapshot.view),
      tostring(snapshot.sort),
      snapshot.suggestions
    )
    lines[#lines + 1] = ("processed %d, skipped %d, remaining %d"):format(
      snapshot.processed,
      snapshot.skipped,
      snapshot.remaining
    )
    lines[#lines + 1] = "current: " .. tostring(snapshot.current_path)
  else
    lines[#lines + 1] = "session: none (state " .. tostring(snapshot.state) .. ")"
  end

  -- Spec 15 §4/§5: a formatter or a `ui.capture.render` override that failed
  -- degrades silently ON SCREEN (that is the point), so the failure has to be
  -- reachable HERE or it is unreportable.
  local ok_ui, ui_mod = pcall(require, "para-organize.ui")
  if ok_ui and type(ui_mod.render_diagnostics) == "function" then
    for _, row in ipairs(ui_mod.render_diagnostics()) do
      lines[#lines + 1] = row
    end
    local mode = type(ui_mod.field_mode) == "function" and ui_mod.field_mode() or nil
    if mode then
      lines[#lines + 1] = "field_mode: " .. tostring(mode)
    end
  end

  local client = select(1, M.client())
  if not client then
    lines[#lines + 1] = "core: not reachable — run :checkhealth para-organize"
    notify(table.concat(lines, "\n"))
    return lines
  end

  lines[#lines + 1] = "core: connected, apiVersion " .. tostring(client.api_version)

  -- One query answers both "index stats" and "capture detection counts"
  -- (spec 03 §2) — fired ASYNCHRONOUSLY.
  --
  -- `request_sync` here pumped the whole vault across the socket with the
  -- Neovim main loop blocked: 153 ms measured over 5,021 notes, ~230 ms at
  -- Matt's real 7.5k, and up to the 5 s timeout if the core was mid-reindex.
  -- That is the ONE user-facing interactive command that broke spec 09 §4's
  -- 50 ms UI budget; every other op path was already async.
  client:request("search.query", { criteria = obj({}) }, function(err, records)
    if err or type(records) ~= "table" then
      lines[#lines + 1] = "index: unavailable ("
        .. tostring(err and (err.message or err) or "no result")
        .. ")"
    else
      local captures, raw, parse_errors = 0, 0, 0
      for _, record in ipairs(records) do
        if record.para_type == "capture" then
          captures = captures + 1
          if record.processing_status == "raw" then
            raw = raw + 1
          end
        end
        if record.parse_error then
          parse_errors = parse_errors + 1
        end
      end
      lines[#lines + 1] = ("index: %d notes, %d parse errors"):format(#records, parse_errors)
      lines[#lines + 1] = ("captures: %d in the capture folder, %d still raw"):format(captures, raw)
    end
    notify(table.concat(lines, "\n"))
  end)

  -- The same table the callback appends to, so a caller can wait for the
  -- index lines to land rather than re-deriving them.
  return lines
end

--- `:ParaOrganizeHealth` / `:checkhealth para-organize`.
function M.health()
  vim.cmd("checkhealth para-organize")
end

return M
