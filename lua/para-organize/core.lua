--- Lifecycle of the organize-core process (spec 10 §1).
---
--- "Auto-spawned by the Neovim client if not running" — that sentence is
--- this module. It connects to a live socket when there is one, otherwise
--- spawns `organize serve`, polls until the socket answers, and hands back
--- an `rpc.Client`.
---
--- Everything here degrades gracefully (10 §1): a missing binary, an
--- unreachable socket or an incompatible core comes back as `nil, err` —
--- never a raised error, never a stack trace. `M.notify_error` renders one
--- clear `vim.notify` line for callers that want it.
---
--- SAFETY: `M.stop()` only ever signals a process THIS Neovim spawned. A
--- core that was already running (Matt's own, or another editor's) is left
--- alone; we just drop our socket.
---
--- @module para-organize.core

local uv = vim.uv or vim.loop
local rpc = require('para-organize.rpc')

local M = {}

--- Default command; overridden by `config.core_cmd`.
M.DEFAULT_CORE_CMD = { 'organize', 'serve' }

--- How long a cold spawn may take before we give up (spec 10 §1: the UI
--- must not hang on a core that will never come up).
M.DEFAULT_SPAWN_TIMEOUT_MS = 2000

-- Module-local process/client state. One core per Neovim instance.
M._client = nil
M._proc = nil -- { handle, pid, cmd, exited, code, signal, log }
M._config = nil

---------------------------------------------------------------------------
-- helpers
---------------------------------------------------------------------------

local function socket_path_of(config)
  local p = config and (config.socket_path or (config.core and config.core.socket_path))
  if type(p) ~= 'string' or p == '' then
    return nil
  end
  return vim.fn.expand(p)
end

local function cfg(config, key, fallback)
  if config == nil then
    return fallback
  end
  local v = config[key]
  if v == nil and type(config.core) == 'table' then
    v = config.core[key]
  end
  if v == nil then
    return fallback
  end
  return v
end

--- Merge `extra` over the inherited environment.
---
--- libuv REPLACES the environment when `env` is given, so a naive
--- `{ 'ORGANIZE_CORE_STATE_DIR=…' }` would launch the core without PATH or
--- HOME. Always start from the real environment.
local function build_env(extra)
  if extra == nil or next(extra) == nil then
    return nil
  end
  local merged = {}
  for k, v in pairs(uv.os_environ()) do
    merged[k] = v
  end
  for k, v in pairs(extra) do
    merged[k] = tostring(v)
  end
  local out = {}
  for k, v in pairs(merged) do
    out[#out + 1] = k .. '=' .. v
  end
  return out
end

--- Build argv, guaranteeing the core serves on the socket we will dial.
--- A `--socket` already present in `core_cmd` wins (the user meant it).
local function build_args(cmd, socket_path)
  local args = {}
  local has_socket = false
  for i = 2, #cmd do
    args[#args + 1] = cmd[i]
    if cmd[i] == '--socket' then
      has_socket = true
    end
  end
  if not has_socket then
    args[#args + 1] = '--socket'
    args[#args + 1] = socket_path
  end
  return args, has_socket
end

--- True when the path exists and is a unix socket.
function M.socket_exists(socket_path)
  if type(socket_path) ~= 'string' or socket_path == '' then
    return false
  end
  local stat = uv.fs_stat(socket_path)
  return stat ~= nil and stat.type == 'socket'
end

---------------------------------------------------------------------------
-- spawning
---------------------------------------------------------------------------

local function open_log(path)
  if type(path) ~= 'string' or path == '' then
    return nil
  end
  vim.fn.mkdir(vim.fn.fnamemodify(path, ':h'), 'p')
  local fd = uv.fs_open(path, 'a', tonumber('644', 8))
  return fd
end

--- Spawn the core. Returns true, or nil + error.
function M._spawn(config, socket_path)
  local cmd = cfg(config, 'core_cmd', M.DEFAULT_CORE_CMD)
  if type(cmd) == 'string' then
    cmd = { cmd }
  end
  if type(cmd) ~= 'table' or type(cmd[1]) ~= 'string' or cmd[1] == '' then
    return nil,
      rpc.error(
        'ConfigError',
        'core_cmd must be a non-empty command list, e.g. { "organize", "serve" }',
        "set `core.core_cmd` in require('para-organize').setup{}"
      )
  end
  local args = build_args(cmd, socket_path)
  local log_fd = open_log(cfg(config, 'core_log', nil))

  local proc = { cmd = cmd, args = args, exited = false, log = cfg(config, 'core_log', nil) }
  local handle, pid = uv.spawn(cmd[1], {
    args = args,
    cwd = cfg(config, 'core_cwd', nil),
    env = build_env(cfg(config, 'core_env', nil)),
    stdio = { nil, log_fd, log_fd },
    detached = false,
  }, function(code, signal)
    proc.exited = true
    proc.code = code
    proc.signal = signal
    -- Closing the handle is what lets libuv reap the child; without it the
    -- core lingers as a zombie for the life of the Neovim process.
    if proc.handle and not proc.handle:is_closing() then
      proc.handle:close()
    end
  end)

  if log_fd then
    -- The child owns its copies of the descriptors now.
    pcall(uv.fs_close, log_fd)
  end

  if not handle then
    local reason = tostring(pid)
    local hint = 'install organize-core, or point `core.core_cmd` at the binary (e.g. "<venv>/bin/organize")'
    return nil, rpc.error('CoreNotFound', ("cannot start organize-core: %s (%s)"):format(cmd[1], reason), hint, { cmd = cmd })
  end

  proc.handle = handle
  proc.pid = pid
  M._proc = proc
  return true
end

---------------------------------------------------------------------------
-- public API
---------------------------------------------------------------------------

--- Connect to organize-core, spawning it if it is not already listening.
---
--- @param config table `{ socket_path, core_cmd?, core_env?, core_cwd?, core_log?, spawn?, spawn_timeout_ms?, timeout_ms?, reconnect? }`
--- @return table|nil client, table|nil err
function M.ensure_running(config)
  config = config or M._config or {}
  M._config = config

  local socket_path = socket_path_of(config)
  if not socket_path then
    return nil,
      rpc.error(
        'ConfigError',
        'no organize-core socket path configured',
        "set `core.socket_path` in require('para-organize').setup{} (default: $XDG_RUNTIME_DIR/organize-core.sock)"
      )
  end

  if M._client and M._client:is_alive() then
    return M._client
  end
  if M._client then
    M._client:close()
    M._client = nil
  end

  local connect_opts = {
    timeout_ms = cfg(config, 'timeout_ms', rpc.DEFAULT_TIMEOUT_MS),
    reconnect = cfg(config, 'reconnect', true),
    on_event = cfg(config, 'on_event', nil),
    on_close = cfg(config, 'on_close', nil),
  }

  -- 1. Already listening? Use it.
  local dial_err
  if M.socket_exists(socket_path) then
    local client, err = rpc.connect(socket_path, connect_opts)
    if client then
      M._client = client
      return client
    end
    dial_err = err
    -- A version mismatch is never fixed by spawning another core.
    if err and err.kind == rpc.KIND_VERSION then
      return nil, err
    end
  end

  if cfg(config, 'spawn', true) == false then
    -- Report what ACTUALLY failed. Minting a fresh generic ConnectError here
    -- threw away a real diagnosis — a handshake Timeout or a ProtocolError
    -- from a process that IS listening was reported as "organize-core is not
    -- running", sending the user to start a second one. The generic message
    -- is correct only when nothing was listening, so no dial was attempted.
    if dial_err then
      return nil, dial_err
    end
    return nil,
      rpc.error(
        rpc.KIND_CONNECT,
        ('organize-core is not running on %s'):format(socket_path),
        'start it with `organize serve`, or allow auto-spawn with core.spawn = true'
      )
  end

  -- 2. Cold spawn.
  if M._proc and not M._proc.exited then
    -- A core we spawned is coming up already; fall through to polling.
  else
    local ok, err = M._spawn(config, socket_path)
    if not ok then
      return nil, err
    end
  end

  -- 3. Poll until it answers, or until it dies / we run out of time.
  local budget = tonumber(cfg(config, 'spawn_timeout_ms', M.DEFAULT_SPAWN_TIMEOUT_MS)) or M.DEFAULT_SPAWN_TIMEOUT_MS
  -- hrtime, not uv.now(): the loop clock is cached and would not advance
  -- across our polling sleeps, turning the budget into an infinite loop.
  local deadline = uv.hrtime() + budget * 1e6
  local last_err
  while uv.hrtime() < deadline do
    if M._proc and M._proc.exited then
      return nil,
        rpc.error(
          'CoreExited',
          ('organize-core exited immediately (code %s, signal %s)'):format(tostring(M._proc.code), tostring(M._proc.signal)),
          M._proc.log and ('see ' .. M._proc.log) or 'run `organize serve` by hand to see why',
          { code = M._proc.code, signal = M._proc.signal }
        )
    end
    if M.socket_exists(socket_path) then
      local client, err = rpc.connect(socket_path, vim.tbl_extend('force', connect_opts, { timeout_ms = 750 }))
      if client then
        M._client = client
        return client
      end
      last_err = err
      if err and err.kind == rpc.KIND_VERSION then
        return nil, err
      end
    end
    vim.wait(40, function()
      return M._proc ~= nil and M._proc.exited
    end, 10)
  end

  return nil,
    last_err
      or rpc.error(
        rpc.KIND_TIMEOUT,
        ('organize-core did not start listening on %s within %dms'):format(socket_path, budget),
        M._proc and M._proc.log and ('see ' .. M._proc.log) or 'run `organize serve` by hand to see why'
      )
end

--- Is a usable core reachable right now?
---
--- Cheap when we already hold a live client; otherwise it actually dials
--- the socket, because a stale socket file left by a crashed core is
--- exactly the case a bare `fs_stat` gets wrong.
--- @param config table|nil defaults to the last config passed to `ensure_running`
--- @return boolean
function M.is_running(config)
  if M._client and M._client:is_alive() then
    return true
  end
  local socket_path = socket_path_of(config or M._config or {})
  if not socket_path or not M.socket_exists(socket_path) then
    return false
  end
  local client = rpc.connect(socket_path, { timeout_ms = 750, reconnect = false })
  if client then
    client:close()
    return true
  end
  return false
end

--- The current client, or nil. Never spawns.
function M.client()
  if M._client and M._client:is_alive() then
    return M._client
  end
  return nil
end

--- PID of the core THIS Neovim spawned, or nil.
function M.spawned_pid()
  if M._proc and not M._proc.exited then
    return M._proc.pid
  end
  return nil
end

--- Close the client and, if we spawned the core, terminate it.
---
--- SIGTERM first (the server unlinks its socket + lock on a clean stop),
--- SIGKILL only if it will not go. Waits for the exit callback so libuv
--- reaps the child — no zombies.
--- @param opts table|nil `{ timeout_ms = 3000 }`
--- @return boolean stopped_process true when we signalled a core we spawned
function M.stop(opts)
  opts = opts or {}
  local timeout_ms = opts.timeout_ms or 3000

  if M._client then
    M._client:close()
    M._client = nil
  end

  local proc = M._proc
  if not proc or proc.exited or not proc.handle then
    M._proc = nil
    return false
  end

  pcall(function()
    proc.handle:kill('sigterm')
  end)
  local gone = vim.wait(timeout_ms, function()
    return proc.exited
  end, 20)
  if not gone then
    pcall(function()
      proc.handle:kill('sigkill')
    end)
    vim.wait(1000, function()
      return proc.exited
    end, 20)
  end
  M._proc = nil
  return true
end

--- One clear `vim.notify` for an unreachable/failed core (10 §1).
--- @param err table|string
function M.notify_error(err)
  vim.notify(rpc.format_error(err), vim.log.levels.ERROR, { title = 'para-organize' })
end

--- `ensure_running` + notify. Returns nil (already reported) on failure —
--- the shape action handlers want.
--- @param config table|nil
--- @return table|nil client
function M.require_client(config)
  local client, err = M.ensure_running(config)
  if not client then
    M.notify_error(err)
    return nil
  end
  return client
end

return M
