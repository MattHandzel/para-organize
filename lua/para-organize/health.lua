--- `:checkhealth para-organize` (spec 03 §2, spec 10 §1).
---
--- Under the core+clients architecture the plugin owns almost nothing that can
--- be misconfigured: vault paths, PARA folders, index and learning state all
--- live in the core (spec 10 §3). So this check splits cleanly in two:
---
---   * the NVIM side — version, dependencies, modules, keymap/action wiring,
---     socket path sanity — which only the plugin can answer; and
---   * the CORE side — vault existence, index stats, note count — which is
---     asked of the core itself (`organize health --json`) instead of being
---     re-implemented here, so the two doors can never disagree.
---
--- Every failure names the offending value and carries an actionable hint
--- (spec 09 §1.5). Nothing here reads the vault; nothing here starts a core.
---
--- Owned by the pickers+health+commands seat.

local M = {}

--- The report sink. Resolved at CALL time, not load time, so `M.reporter` can
--- capture a run (`:checkhealth` still gets `vim.health` untouched).
local health = setmetatable({}, {
  __index = function(_, key)
    return (M.reporter or vim.health)[key]
  end,
})

--- The one cheap read-only RPC used to time a round trip: it resolves routes
--- for an empty tag list, so it touches no index and no disk.
M.PING_METHOD = "routes.resolve"
M.PING_PARAMS = { tags = {} }

--- Connect timeout / round-trip budget, ms.
M.timeout = 2000

--- AF_UNIX caps a socket path at ~104 bytes; CPython surfaces the overrun as a
--- bare OSError with no errno, so the core can only guess at the cause. The
--- client can measure it exactly, which makes this the right place to catch it.
M.MAX_SOCKET_PATH = 104

--- Test seams. `client_factory(socket_path, opts) -> client|nil, err`;
--- `config_override` replaces the resolved plugin config; `system` replaces
--- `vim.system` for the `organize health` call.
M.client_factory = nil
M.config_override = nil
M.system = nil

local REQUIRED_PLUGINS = {
  { name = "plenary.nvim", module = "plenary" },
  { name = "telescope.nvim", module = "telescope" },
  { name = "nui.nvim", module = "nui.popup" },
}

local OPTIONAL_PLUGINS = {
  { name = "which-key.nvim", module = "which-key" },
  { name = "nvim-web-devicons", module = "nvim-web-devicons" },
}

local PLUGIN_MODULES = {
  "init",
  "config",
  "state",
  "rpc",
  "core",
  "ui",
  "actions",
  "pickers",
  "commands",
}

--------------------------------------------------------------------------
-- helpers
--------------------------------------------------------------------------

--- One line of human text for whatever the rpc layer returned as an error —
--- a string, or the core's `{code, message, data={kind, hint}}` envelope.
function M.error_text(err)
  if type(err) == "table" then
    local message = err.message or err.msg
    local hint = type(err.data) == "table" and err.data.hint or err.hint
    if message and hint then
      return ("%s (%s)"):format(message, hint)
    end
    if message then
      return tostring(message)
    end
    return vim.inspect(err)
  end
  return tostring(err)
end

--- The error KIND behind whatever the rpc layer handed back.
---
--- `M.connect` flattens its error to a string (its documented contract), and
--- `rpc`'s `__tostring` renders the kind as a trailing `[Kind]` tag — so the
--- taxonomy survives in both shapes and health can branch on it.
function M.error_kind(err)
  if type(err) == "table" then
    return err.kind or (type(err.data) == "table" and err.data.kind) or nil
  end
  if type(err) == "string" then
    return err:match("%[(%a+)%]")
  end
  return nil
end

local function try_require(name)
  local ok, mod = pcall(require, name)
  if ok then
    return mod
  end
  return nil, tostring(mod)
end

--- The plugin's resolved config, however `para-organize.config` exposes it.
---@return table cfg, string source
function M.config()
  if M.config_override ~= nil then
    return M.config_override, "injected"
  end
  local config = try_require("para-organize.config")
  if type(config) ~= "table" then
    return {}, "missing"
  end
  if type(config.get) == "function" then
    local ok, cfg = pcall(config.get)
    if ok and type(cfg) == "table" then
      return cfg, "config.get()"
    end
  end
  if type(config.options) == "table" then
    return config.options, "config.options"
  end
  if type(config.defaults) == "table" then
    return config.defaults, "config.defaults (setup() has not run)"
  end
  return {}, "empty"
end

--- Where the socket is, per config only. Deliberately does NOT consult the
--- environment: spec 10 §3 puts the socket path in `setup()`, and a health
--- check that silently reads `$XDG_RUNTIME_DIR` would report a different
--- socket than the plugin actually dials.
function M.socket_path(cfg)
  cfg = cfg or {}
  if type(cfg.socket_path) == "string" and cfg.socket_path ~= "" then
    return cfg.socket_path
  end
  if type(cfg.core) == "table" and type(cfg.core.socket_path) == "string" then
    return cfg.core.socket_path
  end
  return nil
end

function M.core_cmd(cfg)
  cfg = cfg or {}
  -- `para-organize.core` reads `config.core_cmd` and falls back to
  -- `config.core.core_cmd`; health must look in the same two places or it
  -- reports a different command than the one that would actually be spawned.
  local cmd = cfg.core_cmd
  if cmd == nil and type(cfg.core) == "table" then
    cmd = cfg.core.core_cmd
  end
  if type(cmd) == "string" then
    cmd = { cmd }
  end
  if type(cmd) ~= "table" or #cmd == 0 then
    cmd = { "organize", "serve" }
  end
  return cmd
end

--- `{organize, serve}` → `{organize, health, --json}`: the same binary, asked
--- for its own diagnosis (spec 10 §1 makes the CLI a first-class door).
function M.core_health_cmd(core_cmd)
  local out = {}
  for i, part in ipairs(core_cmd) do
    if not (i == #core_cmd and part == "serve") then
      out[#out + 1] = part
    end
  end
  out[#out + 1] = "health"
  out[#out + 1] = "--json"
  return out
end

--- Connect to a running core. Never spawns one — `:checkhealth` must not have
--- side effects, and a core that is merely not running is a warning, not a
--- broken install.
---@return table|nil client, string|nil err
function M.connect(socket_path)
  local factory = M.client_factory
  if not factory then
    local rpc, err = try_require("para-organize.rpc")
    if type(rpc) ~= "table" or type(rpc.connect) ~= "function" then
      return nil, "para-organize.rpc is unavailable (" .. tostring(err or "no connect()") .. ")"
    end
    factory = rpc.connect
  end
  -- `rpc.connect` reads `opts.timeout_ms`; `timeout` is carried too so a
  -- stand-in factory that spells it the short way still gets a bound.
  local ok, client, cerr = pcall(
    factory,
    socket_path,
    { timeout_ms = M.timeout, timeout = M.timeout }
  )
  if not ok then
    return nil, tostring(client)
  end
  if not client then
    return nil, tostring(cerr or "connection refused")
  end
  return client
end

local function api_version_of(client)
  if type(client.api_version) == "number" then
    return client.api_version
  end
  if type(client.apiVersion) == "number" then
    return client.apiVersion
  end
  if type(client.api_version) == "function" then
    local ok, value = pcall(client.api_version, client)
    if ok and type(value) == "number" then
      return value
    end
  end
  return nil
end

local function close(client)
  if type(client) == "table" and type(client.close) == "function" then
    pcall(client.close, client)
  end
end

--------------------------------------------------------------------------
-- the individual checks
--------------------------------------------------------------------------

function M.check_neovim()
  health.start("para-organize: Neovim")
  local v = vim.version()
  local version = ("%d.%d.%d"):format(v.major, v.minor, v.patch)
  if vim.fn.has("nvim-0.10") == 1 then
    health.ok("Neovim " .. version)
  elseif vim.fn.has("nvim-0.9") == 1 then
    health.warn("Neovim " .. version .. " — supported, but 0.10+ is the target", {
      "spec 03 §1: the rewrite targets vim.bo[buf], extmarks, vim.fs/vim.uv",
    })
  else
    health.error("Neovim " .. version .. " is too old", { "para-organize needs Neovim 0.9+ (0.10+ recommended)" })
  end
end

function M.check_dependencies()
  health.start("para-organize: dependencies")
  for _, dep in ipairs(REQUIRED_PLUGINS) do
    local mod, err = try_require(dep.module)
    if mod then
      health.ok(dep.name)
    else
      health.error(dep.name .. " is not installed or not on the runtimepath", {
        "add " .. dep.name .. " to your plugin manager (spec 03 §1: required dependency)",
        tostring(err),
      })
    end
  end
  for _, dep in ipairs(OPTIONAL_PLUGINS) do
    if try_require(dep.module) then
      health.ok(dep.name .. " (optional)")
    else
      health.info(dep.name .. " not installed (optional)")
    end
  end
end

--- Are the plugin's own modules present, and does every action a command or
--- keymap can fire actually exist? A missing action is otherwise invisible
--- until the keypress that needs it (spec 09 §1.5).
function M.check_modules()
  health.start("para-organize: modules")
  local missing = {}
  for _, name in ipairs(PLUGIN_MODULES) do
    if not try_require("para-organize." .. name) then
      missing[#missing + 1] = name
    end
  end
  if #missing == 0 then
    health.ok(("all %d plugin modules load"):format(#PLUGIN_MODULES))
  else
    health.error("plugin modules failed to load: " .. table.concat(missing, ", "), {
      "reinstall the plugin, or check for a syntax error with :luafile on the module",
    })
  end

  local commands = try_require("para-organize.commands")
  local actions = try_require("para-organize.actions")
  if type(commands) ~= "table" or type(commands.action_names) ~= "function" then
    return
  end
  if type(actions) ~= "table" then
    health.error("para-organize.actions is unavailable — every command would fail", {
      "reinstall the plugin",
    })
    return
  end
  local unimplemented = {}
  for _, name in ipairs(commands.action_names()) do
    if type(actions[name]) ~= "function" then
      unimplemented[#unimplemented + 1] = name
    end
  end
  if #unimplemented == 0 then
    health.ok("every command and keymap resolves to an action")
  else
    health.error("actions missing: " .. table.concat(unimplemented, ", "), {
      "these subcommands/keymaps would notify 'not implemented' when pressed",
    })
  end
end

function M.check_config()
  health.start("para-organize: configuration")
  local cfg, source = M.config()
  if source == "missing" then
    health.error("para-organize.config is unavailable — setup() has not run", {
      'call require("para-organize").setup({ ... }) in your config',
    })
    return cfg
  end
  health.info("config source: " .. source)

  local socket = M.socket_path(cfg)
  if not socket then
    health.error("no socket_path configured", {
      'set it in setup({ socket_path = vim.env.XDG_RUNTIME_DIR .. "/organize-core.sock" })',
      "spec 10 §3: the socket path is the one non-UI key the plugin config still owns",
    })
  elseif #socket > M.MAX_SOCKET_PATH then
    health.error(("socket_path is %d bytes; AF_UNIX allows about %d"):format(#socket, M.MAX_SOCKET_PATH), {
      "shorten it — e.g. $XDG_RUNTIME_DIR/organize-core.sock",
      "the core cannot bind this path and reports only a bare OSError",
    })
  else
    health.ok(("socket_path = %s (%d bytes)"):format(socket, #socket))
    local dir = vim.fs.dirname(socket)
    if vim.uv.fs_stat(dir) == nil then
      health.warn("socket directory does not exist: " .. dir, {
        "the core creates the socket but not its parent directory",
      })
    end
  end

  local cmd = M.core_cmd(cfg)
  local exe = cmd[1]
  if vim.fn.executable(exe) == 1 then
    health.ok(("core command: %s (%s)"):format(table.concat(cmd, " "), vim.fn.exepath(exe)))
  else
    health.error("core binary not found on $PATH: " .. tostring(exe), {
      "install organize-core, or set setup({ core_cmd = { '/path/to/organize', 'serve' } })",
      "without it the plugin cannot auto-spawn the core (spec 10 §1)",
    })
  end
  return cfg
end

function M.check_core_connection(cfg)
  health.start("para-organize: core connection")
  local socket = M.socket_path(cfg)
  if not socket then
    health.warn("skipped: no socket_path configured")
    return
  end

  local spawn = ((cfg or {}).core or {}).spawn
  local stat = vim.uv.fs_stat(socket)
  if stat == nil then
    -- With `core.spawn = false` nothing will start the core on demand, so the
    -- "this is normal" advice was actively wrong in exactly the configuration
    -- where the user has to act.
    if spawn == false then
      health.error("no core is listening on " .. socket .. " and core.spawn is false", {
        "auto-spawn is disabled — start the core yourself: "
          .. table.concat(M.core_cmd(cfg), " ")
          .. " --socket "
          .. socket,
        "or allow the plugin to start it with core.spawn = true",
      })
      return
    end
    health.warn("no core is listening on " .. socket, {
      "this is normal before the first :ParaOrganize start — the plugin spawns the core on demand",
      "to start one by hand: " .. table.concat(M.core_cmd(cfg), " ") .. " --socket " .. socket,
    })
    return
  end
  if stat.type ~= "socket" then
    health.error(("%s exists but is a %s, not a socket"):format(socket, stat.type), {
      "remove it, or point socket_path somewhere else",
    })
    return
  end

  -- A dead core leaves its socket FILE behind (SIGKILL, or a crash), so the
  -- stat above proves nothing about liveness. Whether that surfaces as a
  -- refused connect or as a silent connect that never answers is a kernel/
  -- libuv detail the user should not have to know about, so both failures
  -- carry the same advice — starting with the stale-socket case.
  local dead_socket_advice = {
    "a killed core leaves its socket file behind — delete " .. socket .. " and retry",
    "check the core is running: " .. table.concat(M.core_cmd(cfg), " ") .. " --socket " .. socket,
  }

  local client, err = M.connect(socket)
  if not client then
    -- An apiVersion mismatch means a perfectly healthy core IS listening; the
    -- stale-socket advice would tell the user to delete a live core's socket.
    local kind = M.error_kind(err)
    local advice = dead_socket_advice
    if kind == "ApiVersionMismatch" then
      advice = {
        "upgrade the plugin or the core so both speak the same major API version",
        "the core on that socket is running and healthy — do NOT delete its socket file",
      }
    end
    health.error("cannot connect to " .. socket .. ": " .. tostring(err), advice)
    return
  end

  local started = vim.uv.hrtime()
  local ok, result, rpc_err = pcall(client.request_sync, client, M.PING_METHOD, M.PING_PARAMS, M.timeout)
  local elapsed_ms = (vim.uv.hrtime() - started) / 1e6

  if not ok then
    health.error(
      ("round trip to %s failed: %s"):format(socket, vim.split(tostring(result), "\n")[1]),
      dead_socket_advice
    )
    close(client)
    return
  end
  if result == nil then
    health.error(
      ("round trip to %s failed: %s"):format(socket, M.error_text(rpc_err)),
      dead_socket_advice
    )
    close(client)
    return
  end
  health.ok(("core reachable — %s round trip in %.1f ms"):format(M.PING_METHOD, elapsed_ms))
  if elapsed_ms > 100 then
    health.warn(("round trip %.1f ms exceeds the 100 ms interactive budget"):format(elapsed_ms), {
      "spec 09 §4: suggestion generation must stay under 100 ms",
    })
  end

  local api = api_version_of(client)
  if api == nil then
    health.info("the rpc client does not expose the negotiated apiVersion")
  elseif api == 1 then
    health.ok("apiVersion handshake: 1")
  else
    health.error(("apiVersion mismatch: core speaks %s, this plugin speaks 1"):format(tostring(api)), {
      "upgrade the plugin or the core so both share the major API version (spec 10 §2)",
    })
  end
  close(client)
end

--- Relay the core's own health report rather than re-deriving it here.
function M.check_core_health(cfg)
  health.start("para-organize: core health (organize health)")
  local cmd = M.core_health_cmd(M.core_cmd(cfg))
  if vim.fn.executable(cmd[1]) ~= 1 then
    health.warn("skipped: " .. tostring(cmd[1]) .. " is not executable")
    return
  end

  local runner = M.system or vim.system
  if type(runner) ~= "function" then
    health.info("skipped: vim.system is unavailable on this Neovim")
    return
  end

  local ok, proc = pcall(runner, cmd, { text = true })
  if not ok then
    health.error("could not run " .. table.concat(cmd, " ") .. ": " .. tostring(proc))
    return
  end
  local waited, res = pcall(proc.wait, proc, 10000)
  if not waited then
    health.error("`organize health` did not finish: " .. tostring(res))
    return
  end

  local decoded_ok, report = pcall(vim.json.decode, res.stdout or "")
  if not decoded_ok or type(report) ~= "table" then
    health.error("`organize health --json` returned no JSON (exit " .. tostring(res.code) .. ")", {
      vim.split(tostring(res.stderr or res.stdout or ""), "\n")[1],
      "run it by hand: " .. table.concat(cmd, " "),
    })
    return
  end

  health.info("vault: " .. tostring(report.vault_root))
  health.info("config: " .. tostring(report.config_file))

  for _, issue in ipairs(report.issues or {}) do
    local advice = issue.hint and { issue.hint } or nil
    if issue.severity == "error" then
      health.error(tostring(issue.message), advice)
    else
      health.warn(tostring(issue.message), advice)
    end
  end

  local index = type(report.index) == "table" and report.index or {}
  local total = tonumber(index.total)
  if total then
    health.info(("index: %d notes, %d capture backlog, %d parse errors"):format(
      total,
      tonumber(index.capture_backlog) or 0,
      tonumber(index.parse_errors) or 0
    ))
    -- Spec 03 §2: warn past 5000 notes.
    if total > 5000 then
      health.warn(("%d notes indexed — expect slower reindexes"):format(total), {
        "spec 09 §4 budgets a full 10k reindex at under 5 s; watch :ParaOrganize reindex timings",
      })
    end
  end

  if report.ok then
    health.ok("core reports healthy")
  else
    health.error("core reports problems (see above)", {
      "fix ~/.config/organize-core/config.toml, then re-run :checkhealth para-organize",
    })
  end
end

--------------------------------------------------------------------------
-- entry point
--------------------------------------------------------------------------

function M.check()
  M.check_neovim()
  M.check_dependencies()
  M.check_modules()
  local cfg = M.check_config()
  M.check_core_connection(cfg)
  M.check_core_health(cfg)
end

return M
