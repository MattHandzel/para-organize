--- The ONE test-support module for `tests/plugin/*_spec.lua`.
---
--- Consolidates the two per-seat helpers (`rpc_core_fixture.lua` from the
--- rpc+core seat, `phc_support.lua` from the pickers+health+commands seat);
--- both are now shims onto this file, so every seat's specs keep running
--- verbatim against a single implementation.
---
--- SAFETY (spec 09 §1.4, CLAUDE.md): every path produced here lives under a
--- fresh temp directory. Nothing in this module reads or writes
--- `~/Obsidian/Main`, `~/notes`, `~/.local/share/organize-core` or
--- `~/.config/organize-core` — the core is confined to the sandbox via
--- `ORGANIZE_CORE_{CONFIG,STATE,RUNTIME}_DIR`.
---
--- What it provides:
---   * `sandbox()` / `build_vault()` — a throwaway fixture vault built by the
---     SAME `tests/conftest.py:build_fixture_vault` the python suite uses, so
---     the lua and python corpora can never drift.
---   * `start_core()` / `core_config()` — a real `organize serve` on a short
---     socket path, killed in teardown.
---   * `connect()` — a minimal REAL newline-framed JSON-RPC client, for specs
---     that must exercise the wire without depending on `para-organize.rpc`.
---   * `capture_notifications()` / `notified()` — assert on `vim.notify`.
---
--- Owned by the INTEGRATOR.

local uv = vim.uv or vim.loop

local M = {}

---------------------------------------------------------------------------
-- locations
---------------------------------------------------------------------------

--- Repo root (set by `minimal_init.lua`; recomputed if this file is loaded
--- some other way).
function M.root()
  local root = _G.PARA_ORGANIZE_REPO or vim.env.PARA_ORGANIZE_REPO_ROOT
  if root and root ~= "" then
    return root
  end
  return vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h:h:h")
end

M.repo = M.root()
M.python = M.root() .. "/.venv/bin/python"
M.organize = M.root() .. "/.venv/bin/organize"

function M.organize_bin()
  return M.organize
end

function M.python_bin()
  return M.python
end

--- A SHORT temp root.
---
--- AF_UNIX `sun_path` is ~104 bytes; the per-session scratch dir blows that on
--- its own (136 bytes) and the core correctly refuses to bind with "AF_UNIX
--- path too long". Everything a core-spawning test needs therefore lives under
--- a deliberately short root, and `sandbox()` asserts the result fits.
local function temp_root()
  local candidates = { "/tmp/claude-" .. tostring(uv.getuid()), uv.os_tmpdir() or "/tmp" }
  for _, dir in ipairs(candidates) do
    local st = uv.fs_stat(dir)
    if st and st.type == "directory" then
      return dir
    end
  end
  return "/tmp"
end

--- Where a unix socket may live: `$XDG_RUNTIME_DIR` when it exists.
function M.socket_dir()
  local base = uv.os_getenv("XDG_RUNTIME_DIR")
  if base and uv.fs_stat(base) then
    return base
  end
  return temp_root()
end

local counter = 0
local function unique(prefix)
  counter = counter + 1
  return ("%s%d-%d"):format(prefix, uv.os_getpid(), counter)
end

---------------------------------------------------------------------------
-- sandbox + fixture vault
---------------------------------------------------------------------------

--- Create an isolated sandbox:
--- `{ dir, vault, config_dir, state_dir, runtime_dir, socket, log }`.
function M.sandbox()
  counter = counter + 1
  local base = ("%s/po%d-%d"):format(temp_root(), uv.os_getpid(), counter)
  vim.fn.delete(base, "rf")
  vim.fn.mkdir(base, "p")
  local sb = {
    dir = base,
    vault = base .. "/v",
    config_dir = base .. "/c",
    state_dir = base .. "/s",
    runtime_dir = base .. "/r",
    socket = base .. "/o.sock",
    log = base .. "/core.log",
  }
  for _, d in ipairs({ sb.vault, sb.config_dir, sb.state_dir, sb.runtime_dir }) do
    vim.fn.mkdir(d, "p")
  end
  assert(#sb.socket < 100, "socket path too long for AF_UNIX: " .. sb.socket)
  return sb
end

--- Run one python snippet in the project venv.
local function python(script, what)
  local res = vim.system({ M.python, "-c", script }, { text = true }):wait(120000)
  assert(res.code == 0, (what or "python") .. " failed: " .. tostring(res.stderr or res.stdout))
  return vim.trim(res.stdout or "")
end

--- Build the fixture vault inside `sb` (via `tests/conftest.py`) and write a
--- minimal core `config.toml` pointing at it. Returns `sb`.
function M.build_vault(sb)
  sb = sb or M.sandbox()
  python(
    table.concat({
      "import sys, pathlib",
      ("sys.path.insert(0, %q)"):format(M.root() .. "/tests"),
      "from conftest import build_fixture_vault",
      ("root = pathlib.Path(%q)"):format(sb.vault),
      "root.mkdir(parents=True, exist_ok=True)",
      "build_fixture_vault(root)",
      ("cfg = pathlib.Path(%q)"):format(sb.config_dir),
      "cfg.mkdir(parents=True, exist_ok=True)",
      '(cfg / "config.toml").write_text("[vault]\\nroot = \\"%s\\"\\n" % root, encoding="utf-8")',
    }, "\n"),
    "fixture vault build"
  )
  return sb
end

--- The `phc_support` shape: a fixture in its own tmp tree, returned as
--- `{ root, vault, config_dir, state_dir }` (vault path as a STRING).
function M.fixture_vault()
  local root = vim.fn.tempname()
  vim.fn.mkdir(root, "p")
  local vault = python(
    table.concat({
      "import sys",
      ("sys.path.insert(0, %q)"):format(M.root() .. "/tests"),
      "from pathlib import Path",
      "from conftest import build_fixture_vault",
      ("root = Path(%q)"):format(root),
      "vault = build_fixture_vault(root / 'vault')",
      "cfg = root / 'config'",
      "cfg.mkdir(parents=True, exist_ok=True)",
      "(cfg / 'config.toml').write_text('[vault]\\nroot = \"%s\"\\n' % vault, encoding='utf-8')",
      "print(vault)",
    }, "\n"),
    "fixture vault build"
  )
  return {
    root = root,
    vault = vault,
    config_dir = root .. "/config",
    state_dir = root .. "/state",
  }
end

--- The env that confines the core to `sb`.
function M.env(sb)
  return {
    ORGANIZE_CORE_CONFIG_DIR = sb.config_dir,
    ORGANIZE_CORE_STATE_DIR = sb.state_dir,
    ORGANIZE_CORE_RUNTIME_DIR = sb.runtime_dir,
  }
end

--- The config table `core.ensure_running` (and `init.setup`) consume for this
--- sandbox. `--idle-timeout 0` disables the idle shutdown so a slow test
--- cannot race the server into exiting underneath it.
function M.core_config(sb, overrides)
  local conf = {
    socket_path = sb.socket,
    core_cmd = { M.organize, "serve", "--idle-timeout", "0" },
    core_env = M.env(sb),
    core_cwd = M.root(),
    core_log = sb.log,
    spawn_timeout_ms = 15000,
    timeout_ms = 10000,
  }
  return vim.tbl_extend("force", conf, overrides or {})
end

--- The same values in the shape `require("para-organize").setup{}` accepts
--- (spec 10 §3: UI keys at the top level, core-process knobs under `core`).
--- `ui.close_on_complete = false` keeps the completion notice on screen so a
--- test can assert on it instead of racing the unmount.
function M.plugin_config(sb, overrides)
  local conf = {
    socket_path = sb.socket,
    core = {
      core_cmd = { M.organize, "serve", "--idle-timeout", "0" },
      core_env = M.env(sb),
      core_cwd = M.root(),
      core_log = sb.log,
      spawn = true,
      spawn_timeout_ms = 20000,
      timeout_ms = 15000,
    },
    ui = { close_on_complete = false },
  }
  return vim.tbl_deep_extend("force", conf, overrides or {})
end

---------------------------------------------------------------------------
-- a real core process
---------------------------------------------------------------------------

--- Start a core for `sb` directly (bypassing `core.lua`) and return
--- `{ pid, socket, stop = fn }`.
function M.start_core(sb)
  local env = {}
  for k, v in pairs(uv.os_environ()) do
    env[k] = v
  end
  for k, v in pairs(M.env(sb)) do
    env[k] = v
  end
  local envlist = {}
  for k, v in pairs(env) do
    envlist[#envlist + 1] = k .. "=" .. v
  end

  local fd = uv.fs_open(sb.log, "a", tonumber("644", 8))
  local state = { exited = false, socket = sb.socket, sandbox = sb }
  local handle, pid = uv.spawn(M.organize, {
    args = { "serve", "--socket", sb.socket, "--idle-timeout", "0" },
    cwd = M.root(),
    env = envlist,
    stdio = { nil, fd, fd },
  }, function(code, signal)
    state.exited = true
    state.code = code
    state.signal = signal
    if state.handle and not state.handle:is_closing() then
      state.handle:close()
    end
  end)
  if fd then
    pcall(uv.fs_close, fd)
  end
  assert(handle, "could not spawn " .. M.organize .. ": " .. tostring(pid))
  state.handle = handle
  state.pid = pid

  local ready = vim.wait(20000, function()
    local st = uv.fs_stat(sb.socket)
    return state.exited or (st ~= nil and st.type == "socket")
  end, 25)
  assert(ready and not state.exited, "core never listened on " .. sb.socket .. "\n" .. M.read_log(sb))

  state.stop = function()
    if state.exited then
      return
    end
    pcall(function()
      handle:kill("sigterm")
    end)
    if not vim.wait(5000, function()
      return state.exited
    end, 20) then
      pcall(function()
        handle:kill("sigkill")
      end)
      vim.wait(2000, function()
        return state.exited
      end, 20)
    end
  end
  return state
end

--- The `phc_support` shape: build a fixture (if not given), serve it on a
--- short socket under `$XDG_RUNTIME_DIR`, and hand back
--- `{ socket, fixture, stop() }` — `stop()` also deletes both trees.
function M.serve(fixture)
  fixture = fixture or M.fixture_vault()
  local sockdir = M.socket_dir() .. "/" .. unique("porg-test-")
  vim.fn.mkdir(sockdir, "p")
  local socket = sockdir .. "/core.sock"
  assert(#socket < 100, "socket path too long for AF_UNIX: " .. socket)

  local handle = { fixture = fixture, socket = socket, sockdir = sockdir, log = {} }

  local proc = vim.system({
    M.organize,
    "serve",
    "--socket",
    socket,
    "--idle-timeout",
    "300",
  }, {
    text = true,
    env = {
      PATH = uv.os_getenv("PATH"),
      HOME = uv.os_getenv("HOME"),
      ORGANIZE_CORE_CONFIG_DIR = fixture.config_dir,
      ORGANIZE_CORE_STATE_DIR = fixture.state_dir,
      ORGANIZE_CORE_RUNTIME_DIR = sockdir,
    },
    stderr = function(_, data)
      if data then
        handle.log[#handle.log + 1] = data
      end
    end,
  })
  handle.proc = proc

  local ready = vim.wait(20000, function()
    local stat = uv.fs_stat(socket)
    return stat ~= nil and stat.type == "socket"
  end, 25)
  assert(ready, "core never bound " .. socket .. "\n" .. table.concat(handle.log, ""))

  function handle.stop()
    pcall(function()
      proc:kill(15)
    end)
    pcall(function()
      proc:wait(5000)
    end)
    pcall(vim.fn.delete, sockdir, "rf")
    pcall(vim.fn.delete, fixture.root, "rf")
  end

  return handle
end

function M.read_log(sb)
  local ok, lines = pcall(vim.fn.readfile, sb.log)
  if not ok then
    return "(no core log)"
  end
  return table.concat(lines, "\n")
end

function M.cleanup(sb)
  if not (sb and sb.dir) then
    return
  end
  pcall(vim.fn.delete, sb.socket)
  vim.fn.delete(sb.dir, "rf")
  if uv.fs_stat(sb.dir) then
    -- a handle closing on the loop can still hold the socket for a tick
    vim.wait(300, function()
      return uv.fs_stat(sb.dir) == nil
    end, 25)
    vim.fn.delete(sb.dir, "rf")
  end
end

--- Wait until `fn()` is truthy; returns its value, or nil on timeout.
function M.wait_for(timeout_ms, fn)
  local value
  vim.wait(timeout_ms, function()
    value = fn()
    return value ~= nil and value ~= false
  end, 10)
  return value
end

---------------------------------------------------------------------------
-- a real newline-framed JSON-RPC client (stand-in for para-organize.rpc)
---------------------------------------------------------------------------

local Client = {}
Client.__index = Client

function Client:_pump(timeout)
  local ok = vim.wait(timeout or 2000, function()
    return #self.lines > 0 or self.closed
  end, 5)
  if not ok or #self.lines == 0 then
    return nil
  end
  return table.remove(self.lines, 1)
end

--- Blocking request. Skips server-push event notifications (they carry no
--- `id`) until the matching response arrives.
---@return any result, any err
function Client:request_sync(method, params, timeout)
  timeout = timeout or 5000
  self.id = self.id + 1
  local id = self.id
  local line = vim.json.encode({
    jsonrpc = "2.0",
    apiVersion = 1,
    id = id,
    method = method,
    -- An EMPTY Lua table is not enough: `vim.json.encode({})` emits `[]`, and
    -- the server rejects positional params with -32602 BEFORE it looks the
    -- method up — so a no-argument call would fail with a misleading error.
    params = (params == nil or (type(params) == "table" and next(params) == nil))
        and vim.empty_dict()
      or params,
  })
  uv.write(self.pipe, line .. "\n")

  local deadline = uv.now() + timeout
  while uv.now() <= deadline do
    local raw = self:_pump(math.max(50, deadline - uv.now()))
    if raw == nil then
      break
    end
    local ok, msg = pcall(vim.json.decode, raw)
    if ok and type(msg) == "table" and msg.id == id then
      if msg.error ~= nil then
        return nil, msg.error
      end
      return msg.result
    end
    if ok and type(msg) == "table" and msg.method == "event" then
      self.events[#self.events + 1] = msg.params
    end
  end
  return nil, ("timed out waiting for %s"):format(method)
end

--- Async form — `cb(err, result)`, ERR FIRST, mirroring
--- `para-organize.rpc`'s `Client:request` exactly (its `request_sync` returns
--- `result, err`, the opposite order; that asymmetry is the module's
--- documented contract and any stand-in must reproduce it or it hides bugs).
function Client:request(method, params, cb)
  vim.schedule(function()
    local result, err = self:request_sync(method, params, 5000)
    cb(err, result)
  end)
end

function Client:close()
  if self.closed then
    return
  end
  self.closed = true
  pcall(uv.read_stop, self.pipe)
  pcall(uv.close, self.pipe)
end

--- Connect and read the core's handshake line (`{"apiVersion": 1}`).
---@return table|nil client, string|nil err
function M.connect(socket_path, opts)
  opts = opts or {}
  local timeout = opts.timeout or 2000
  local pipe = uv.new_pipe(false)
  local outcome = nil

  uv.pipe_connect(pipe, socket_path, function(err)
    outcome = err or true
  end)
  vim.wait(timeout, function()
    return outcome ~= nil
  end, 5)

  if outcome ~= true then
    pcall(uv.close, pipe)
    return nil, tostring(outcome or ("connect timed out: " .. socket_path))
  end

  local self = setmetatable({
    pipe = pipe,
    buffer = "",
    lines = {},
    events = {},
    id = 0,
    closed = false,
  }, Client)

  uv.read_start(pipe, function(err, chunk)
    if err or not chunk then
      self.closed = true
      return
    end
    self.buffer = self.buffer .. chunk
    while true do
      local nl = self.buffer:find("\n", 1, true)
      if not nl then
        break
      end
      self.lines[#self.lines + 1] = self.buffer:sub(1, nl - 1)
      self.buffer = self.buffer:sub(nl + 1)
    end
  end)

  local handshake = self:_pump(timeout)
  if handshake then
    local ok, decoded = pcall(vim.json.decode, handshake)
    if ok and type(decoded) == "table" then
      self.api_version = decoded.apiVersion
    end
  end
  return self
end

---------------------------------------------------------------------------
-- notifications
---------------------------------------------------------------------------

--- Collect every `vim.notify` call made while `fn` runs.
---@return table[] notifications `{ {msg=..., level=...}, ... }`
function M.capture_notifications(fn)
  local seen = {}
  local original = vim.notify
  vim.notify = function(msg, level)
    seen[#seen + 1] = { msg = msg, level = level }
  end
  local ok, err = pcall(fn)
  vim.notify = original
  if not ok then
    error(err)
  end
  return seen
end

--- Did any captured notification contain `needle`?
function M.notified(seen, needle)
  for _, entry in ipairs(seen) do
    if type(entry.msg) == "string" and entry.msg:find(needle, 1, true) then
      return true, entry
    end
  end
  return false
end

return M
