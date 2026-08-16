--- `:checkhealth para-organize` (spec 03 §2, spec 10 §1).
---
--- The green path runs against a REAL spawned `organize serve` over a REAL
--- unix socket, and the degradation paths against a genuinely dead one — a
--- core killed with SIGKILL, which leaves its socket file behind exactly the
--- way a crash does. Everything is under a throwaway fixture vault.

local health = require("para-organize.health")
local support = require("phc_support")

--- Capture one health run as a list of `{kind, msg, advice}`.
local function report(fn)
  local entries = {}
  local function push(kind)
    return function(msg, advice)
      entries[#entries + 1] = { kind = kind, msg = tostring(msg), advice = advice }
    end
  end
  health.reporter = {
    start = push("start"),
    ok = push("ok"),
    warn = push("warn"),
    error = push("error"),
    info = push("info"),
  }
  local ok, err = pcall(fn)
  health.reporter = nil
  if not ok then
    error(err)
  end
  return entries
end

--- Does any entry of `kind` contain `needle`?
local function found(entries, kind, needle)
  for _, entry in ipairs(entries) do
    if (kind == nil or entry.kind == kind) and entry.msg:find(needle, 1, true) then
      return true, entry
    end
  end
  return false
end

local function kinds(entries, kind)
  return vim.tbl_filter(function(e)
    return e.kind == kind
  end, entries)
end

--------------------------------------------------------------------------

describe("health: environment", function()
  after_each(function()
    health.config_override = nil
    health.client_factory = nil
    health.system = nil
  end)

  it("passes the Neovim version check on a supported build", function()
    local entries = report(health.check_neovim)
    assert.equals(0, #kinds(entries, "error"))
    assert.is_true(found(entries, "ok", "Neovim "))
  end)

  it("finds the three required plugins", function()
    local entries = report(health.check_dependencies)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "ok", "plenary.nvim"))
    assert.is_true(found(entries, "ok", "telescope.nvim"))
    assert.is_true(found(entries, "ok", "nui.nvim"))
  end)
end)

describe("health: modules and actions", function()
  local MODULES = { "init", "config", "state", "rpc", "core", "ui", "actions", "pickers", "commands" }
  local saved = {}

  -- A Lua table cannot HOLD nil, so "this module was not loaded" needs a
  -- sentinel. Without one, `restore()` never visits the keys it stubbed and
  -- the empty table stays in `package.loaded` for the rest of the process —
  -- every later `require("para-organize.rpc")` then gets {} instead of the
  -- real module, and the next test blames the wrong thing.
  local ABSENT = {}

  local function preload_siblings(actions)
    for _, name in ipairs(MODULES) do
      local key = "para-organize." .. name
      if saved[key] == nil then
        saved[key] = package.loaded[key] == nil and ABSENT or package.loaded[key]
      end
      if package.loaded[key] == nil then
        package.loaded[key] = {}
      end
    end
    package.loaded["para-organize.actions"] = actions
  end

  local function restore()
    for key, value in pairs(saved) do
      package.loaded[key] = value ~= ABSENT and value or nil
    end
    saved = {}
  end

  after_each(restore)

  it("is green when every module loads and every action exists", function()
    local commands = require("para-organize.commands")
    local actions = {}
    for _, name in ipairs(commands.action_names()) do
      actions[name] = function() end
    end
    preload_siblings(actions)

    local entries = report(health.check_modules)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "ok", "plugin modules load"))
    assert.is_true(found(entries, "ok", "every command and keymap resolves to an action"))
  end)

  it("names the actions a command or keymap would dispatch to in vain", function()
    local commands = require("para-organize.commands")
    local actions = {}
    for _, name in ipairs(commands.action_names()) do
      actions[name] = function() end
    end
    actions.archive = nil
    actions.set_meta = nil
    preload_siblings(actions)

    local entries = report(health.check_modules)
    assert.is_true(found(entries, "error", "actions missing"))
    assert.is_true(found(entries, "error", "archive"))
    assert.is_true(found(entries, "error", "set_meta"))
  end)
end)

describe("health: configuration sanity", function()
  after_each(function()
    health.config_override = nil
  end)

  it("errors when setup() never provided a socket path", function()
    health.config_override = {}
    local entries = report(health.check_config)
    assert.is_true(found(entries, "error", "no socket_path configured"))
  end)

  it("catches a socket path AF_UNIX cannot bind, before the core does", function()
    -- The scratchpad dir alone is 136 bytes; CPython reports the overrun as a
    -- bare OSError with no errno, so the core can only guess. The client can
    -- measure it exactly.
    local long = "/tmp/" .. string.rep("x", 120) .. "/core.sock"
    health.config_override = { socket_path = long, core_cmd = { support.organize, "serve" } }
    local entries = report(health.check_config)
    local ok, entry = found(entries, "error", "AF_UNIX allows about")
    assert.is_true(ok)
    assert.is_true(entry.msg:find(tostring(#long), 1, true) ~= nil)
    assert.is_truthy(entry.advice)
  end)

  it("errors when the core binary is not on $PATH, and names the override", function()
    health.config_override = {
      socket_path = support.socket_dir() .. "/porg-health.sock",
      core_cmd = { "definitely-not-a-real-binary", "serve" },
    }
    local entries = report(health.check_config)
    local ok, entry = found(entries, "error", "core binary not found")
    assert.is_true(ok)
    assert.is_true(entry.msg:find("definitely-not-a-real-binary", 1, true) ~= nil)
    assert.is_true(vim.iter(entry.advice):any(function(line)
      return line:find("core_cmd", 1, true) ~= nil
    end))
  end)

  it("is green on a sane config", function()
    health.config_override = {
      socket_path = support.socket_dir() .. "/porg-health.sock",
      core_cmd = { support.organize, "serve" },
    }
    local entries = report(health.check_config)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "ok", "socket_path ="))
    assert.is_true(found(entries, "ok", "core command:"))
  end)

  it("maps the serve command to the health command", function()
    assert.same(
      { "organize", "health", "--json" },
      health.core_health_cmd({ "organize", "serve" })
    )
    assert.same(
      { "/opt/organize", "--debug", "health", "--json" },
      health.core_health_cmd({ "/opt/organize", "--debug", "serve" })
    )
  end)
end)

describe("health: core connection", function()
  after_each(function()
    health.config_override = nil
    health.client_factory = nil
  end)

  it("warns (does not error) when no core is listening yet", function()
    health.config_override = {
      socket_path = support.socket_dir() .. "/porg-health-absent.sock",
      core_cmd = { support.organize, "serve" },
    }
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    assert.equals(0, #kinds(entries, "error"))
    local ok, entry = found(entries, "warn", "no core is listening")
    assert.is_true(ok)
    assert.is_true(vim.iter(entry.advice):any(function(line)
      return line:find("spawns the core on demand", 1, true) ~= nil
    end))
  end)

  it("errors when the socket path is occupied by something that is not a socket", function()
    local decoy = vim.fn.tempname()
    vim.fn.writefile({ "not a socket" }, decoy)
    health.config_override = { socket_path = decoy, core_cmd = { support.organize, "serve" } }
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    vim.fn.delete(decoy)
    assert.is_true(found(entries, "error", "not a socket"))
  end)

  it("errors on an apiVersion the plugin cannot speak", function()
    local decoy = vim.fn.tempname()
    health.config_override = { socket_path = decoy, core_cmd = { support.organize, "serve" } }
    health.client_factory = function()
      return {
        api_version = 2,
        request_sync = function()
          return {}
        end,
        close = function() end,
      }
    end
    -- The connection check needs the path to look like a live socket, so point
    -- it at a real one and let the injected client answer.
    local core = support.start_core()
    health.config_override.socket_path = core.socket
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    core.stop()
    vim.fn.delete(decoy)

    local ok, entry = found(entries, "error", "apiVersion mismatch")
    assert.is_true(ok)
    assert.is_true(entry.msg:find("core speaks 2", 1, true) ~= nil)
  end)
end)

describe("health: against a real organize core", function()
  local core = support.start_core()

  vim.api.nvim_create_autocmd("VimLeavePre", {
    callback = function()
      pcall(core.stop)
    end,
  })

  local function with_core_env(fn)
    local saved = {
      ORGANIZE_CORE_CONFIG_DIR = vim.env.ORGANIZE_CORE_CONFIG_DIR,
      ORGANIZE_CORE_STATE_DIR = vim.env.ORGANIZE_CORE_STATE_DIR,
      ORGANIZE_CORE_RUNTIME_DIR = vim.env.ORGANIZE_CORE_RUNTIME_DIR,
    }
    vim.env.ORGANIZE_CORE_CONFIG_DIR = core.fixture.config_dir
    vim.env.ORGANIZE_CORE_STATE_DIR = core.fixture.state_dir
    vim.env.ORGANIZE_CORE_RUNTIME_DIR = core.sockdir
    local ok, result = pcall(fn)
    for key, value in pairs(saved) do
      vim.env[key] = value
    end
    if not ok then
      error(result)
    end
    return result
  end

  before_each(function()
    health.config_override = {
      socket_path = core.socket,
      core_cmd = { support.organize, "serve" },
    }
    -- Stand in for `para-organize.rpc` until it lands: a REAL client on the
    -- REAL socket, with the contracted Client surface.
    health.client_factory = function(socket_path, opts)
      return support.connect(socket_path, opts)
    end
  end)

  after_each(function()
    health.config_override = nil
    health.client_factory = nil
    health.system = nil
  end)

  it("reports the core reachable, with a round-trip time and the handshake", function()
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "ok", "core reachable"))
    assert.is_true(found(entries, "ok", "round trip in"))
    assert.is_true(found(entries, "ok", "apiVersion handshake: 1"))
  end)

  it("reaches the core through the REAL para-organize.rpc module", function()
    -- No injected factory: this is the production path, including
    -- `rpc.connect(socket, {timeout_ms=…})` and `Client.api_version`.
    health.client_factory = nil
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "ok", "core reachable"))
    assert.is_true(found(entries, "ok", "apiVersion handshake: 1"))
  end)

  it("reads core_cmd from the nested [core] table too", function()
    -- `para-organize.core` accepts both spellings; health must report the
    -- command that would actually be spawned.
    assert.same(
      { "/opt/organize", "serve" },
      health.core_cmd({ core = { core_cmd = { "/opt/organize", "serve" } } })
    )
    assert.same({ "organize", "serve" }, health.core_cmd({}))
  end)

  it("relays the core's own health report", function()
    local entries = with_core_env(function()
      return report(function()
        health.check_core_health(health.config())
      end)
    end)
    assert.equals(0, #kinds(entries, "error"), vim.inspect(kinds(entries, "error")))
    assert.is_true(found(entries, "info", core.fixture.vault))
    assert.is_true(found(entries, "info", "index:"))
    assert.is_true(found(entries, "ok", "core reports healthy"))
  end)

  it("surfaces the core's issues with their hints", function()
    local bad = vim.fn.tempname()
    vim.fn.mkdir(bad .. "/config", "p")
    vim.fn.writefile({ "[vault]", ('root = "%s/nope-vault"'):format(bad) }, bad .. "/config/config.toml")

    local saved = vim.env.ORGANIZE_CORE_CONFIG_DIR
    vim.env.ORGANIZE_CORE_CONFIG_DIR = bad .. "/config"
    local entries = report(function()
      health.check_core_health(health.config())
    end)
    vim.env.ORGANIZE_CORE_CONFIG_DIR = saved
    vim.fn.delete(bad, "rf")

    assert.is_true(found(entries, "error", "vault.root does not exist"))
    assert.is_true(found(entries, "error", "core reports problems"))
    local _, entry = found(entries, "error", "vault.root does not exist")
    assert.is_truthy(entry.advice, "the core's hint was dropped")
  end)

  it("warns past the 5000-note performance threshold (03 §2)", function()
    health.system = function()
      return {
        wait = function()
          return {
            code = 0,
            stdout = vim.json.encode({
              ok = true,
              vault_root = "/fixture",
              config_file = "/fixture/config.toml",
              issues = {},
              index = { total = 6000, capture_backlog = 3, parse_errors = 0 },
            }),
            stderr = "",
          }
        end,
      }
    end
    local entries = report(function()
      health.check_core_health(health.config())
    end)
    assert.is_true(found(entries, "warn", "6000 notes indexed"))
    assert.is_true(found(entries, "ok", "core reports healthy"))
  end)

  it("errors clearly when `organize health` returns something that is not JSON", function()
    health.system = function()
      return {
        wait = function()
          return { code = 3, stdout = "Traceback (most recent call last):", stderr = "boom" }
        end,
      }
    end
    local entries = report(function()
      health.check_core_health(health.config())
    end)
    assert.is_true(found(entries, "error", "returned no JSON"))
  end)

  it("degrades on a DEAD socket: one clear error, no stack trace", function()
    -- SIGKILL leaves the socket FILE behind exactly the way a crash does, so
    -- `fs_stat` still says "socket" — the case a liveness check that trusts
    -- the filesystem would call healthy. Whether the kernel then refuses the
    -- connect or accepts one nobody answers is not something the user should
    -- have to know, so both must land on the same actionable error.
    local doomed = support.start_core()
    local socket = doomed.socket
    doomed.proc:kill(9)
    doomed.proc:wait(5000)
    assert.is_truthy(vim.uv.fs_stat(socket), "SIGKILL should have left the socket file behind")

    health.config_override = { socket_path = socket, core_cmd = { support.organize, "serve" } }
    local entries = report(function()
      health.check_core_connection(health.config())
    end)
    doomed.stop()

    local errors = kinds(entries, "error")
    assert.equals(1, #errors, vim.inspect(entries))
    local entry = errors[1]
    assert.is_truthy(entry.msg:find(socket, 1, true), entry.msg)
    assert.is_falsy(entry.msg:find("stack traceback", 1, true))
    assert.is_truthy(entry.advice, "a dead socket must come with advice")
    assert.is_true(
      vim.iter(entry.advice):any(function(line)
        return line:find("delete", 1, true) ~= nil
      end),
      vim.inspect(entry.advice)
    )
    -- Bounded: the check must not hang waiting on a corpse.
    assert.is_false(found(entries, "ok", "core reachable"))
  end)

  it("runs every section without raising", function()
    local entries = with_core_env(function()
      return report(health.check)
    end)
    local sections = vim.tbl_map(function(e)
      return e.msg
    end, kinds(entries, "start"))
    assert.same({
      "para-organize: Neovim",
      "para-organize: dependencies",
      "para-organize: modules",
      "para-organize: configuration",
      "para-organize: core connection",
      "para-organize: core health (organize health)",
    }, sections)
  end)

  it("is discoverable as :checkhealth para-organize", function()
    -- The real reporter, the real command: proves the module is found where
    -- Neovim looks for it, not just that M.check() exists.
    health.client_factory = nil
    with_core_env(function()
      vim.cmd("checkhealth para-organize")
    end)
    local lines = table.concat(vim.api.nvim_buf_get_lines(0, 0, -1, false), "\n")
    vim.cmd("bwipeout!")
    assert.is_truthy(lines:find("para-organize", 1, true), lines:sub(1, 500))
    assert.is_truthy(lines:find("Neovim", 1, true))
    assert.is_falsy(lines:find("stack traceback", 1, true), lines)
  end)

  it("stops the core and leaves no socket behind", function()
    core.stop()
    assert.is_nil(vim.uv.fs_stat(core.socket))
  end)
end)
