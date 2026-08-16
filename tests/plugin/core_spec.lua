-- Specs for para-organize.core — organize-core process lifecycle (spec 10 §1).
--
-- Run:
--   nvim --headless --noplugin -u tests/plugin/minimal_init_rpc.lua \
--        -c "PlenaryBustedFile tests/plugin/core_spec.lua"
--
-- Every case spawns the REAL core against a throwaway fixture vault and
-- leaves no process behind (after_each stops whatever it started).

local uv = vim.uv or vim.loop
local here = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')
local fixture = dofile(here .. '/rpc_core_fixture.lua')

local core = require('para-organize.core')
local rpc = require('para-organize.rpc')

local function alive(pid)
  if not pid then
    return false
  end
  -- signal 0 probes without delivering anything
  local called, res = pcall(uv.kill, pid, 0)
  return called and (res == 0 or res == true)
end

describe('core.ensure_running', function()
  local sb

  before_each(function()
    sb = fixture.build_vault(fixture.sandbox())
  end)

  after_each(function()
    core.stop()
    fixture.cleanup(sb)
  end)

  it('cold-spawns organize serve and hands back a working client', function()
    assert.is_false(core.is_running(fixture.core_config(sb)))

    local client, err = core.ensure_running(fixture.core_config(sb))
    assert(client, 'ensure_running failed: ' .. tostring(err and err.message) .. '\n' .. fixture.read_log(sb))
    assert.equals(1, client.api_version)
    assert.is_number(core.spawned_pid())
    assert.is_true(core.is_running())

    local res, rerr = client:request_sync('session.start', {})
    assert.is_nil(rerr)
    assert.is_true(#res.captures > 0)
  end)

  it('passes cwd and env through to the spawned core', function()
    -- The sandbox is reachable ONLY via ORGANIZE_CORE_CONFIG_DIR; if the env
    -- did not reach the child it would load Matt's real config instead, and
    -- the vault root would not be our fixture.
    local client = assert(core.ensure_running(fixture.core_config(sb)))
    local res = assert(client:request_sync('session.start', {}))
    assert.is_truthy(res.captures[1].path:find(sb.vault, 1, true))
  end)

  it('reuses a core that is already listening instead of spawning another', function()
    local existing = fixture.start_core(sb)
    local client, err = core.ensure_running(fixture.core_config(sb))
    assert(client, tostring(err and err.message))
    assert.is_nil(core.spawned_pid()) -- we attached, we did not spawn
    assert.is_true(core.is_running())
    assert.is_true(alive(existing.pid))

    core.stop() -- must NOT kill a core it did not start
    assert.is_true(alive(existing.pid))
    existing.stop()
  end)

  it('returns the same live client on a second call', function()
    local a = assert(core.ensure_running(fixture.core_config(sb)))
    local b = assert(core.ensure_running(fixture.core_config(sb)))
    assert.equals(a, b)
  end)

  it('stop() terminates the core it spawned and leaves no zombie', function()
    assert(core.ensure_running(fixture.core_config(sb)))
    local pid = core.spawned_pid()
    assert.is_number(pid)

    assert.is_true(core.stop())
    assert.is_truthy(fixture.wait_for(5000, function()
      return (not alive(pid)) or nil
    end))
    assert.is_nil(core.spawned_pid())
    assert.is_nil(core.client())
    assert.is_false(core.is_running(fixture.core_config(sb)))
    -- a clean SIGTERM shutdown unlinks the socket
    assert.is_false(core.socket_exists(sb.socket))
  end)

  it('recovers from the stale socket a crashed core leaves behind', function()
    local dead = fixture.start_core(sb)
    -- SIGKILL, so the server never runs its unlink-on-shutdown path
    pcall(uv.kill, dead.pid, 9)
    assert.is_truthy(fixture.wait_for(8000, function()
      return (not alive(dead.pid)) or nil
    end))
    assert.is_true(core.socket_exists(sb.socket)) -- the dead inode is still there

    local client, err = core.ensure_running(fixture.core_config(sb))
    assert(client, 'ensure_running failed: ' .. tostring(err and err.message) .. '\n' .. fixture.read_log(sb))
    assert.is_number(core.spawned_pid())
    local res = assert(client:request_sync('session.start', {}))
    assert.is_true(#res.captures > 0)
  end)

  it('stop() is safe to call when nothing was started', function()
    assert.is_false(core.stop())
    assert.is_false(core.stop())
  end)
end)

describe('core degradation (spec 10 §1)', function()
  local sb

  before_each(function()
    sb = fixture.sandbox()
  end)

  after_each(function()
    core.stop()
    fixture.cleanup(sb)
  end)

  it('reports a missing binary as one clear error, no crash', function()
    local client, err = core.ensure_running(fixture.core_config(sb, {
      core_cmd = { '/nonexistent/organize', 'serve' },
      spawn_timeout_ms = 3000,
    }))
    assert.is_nil(client)
    assert.is_true(rpc.is_error(err))
    assert.equals('CoreNotFound', err.kind)
    local text = rpc.format_error(err)
    assert.is_truthy(text:find('/nonexistent/organize', 1, true))
    assert.is_truthy(text:find('hint:', 1, true))
    assert.is_nil(text:find('stack traceback', 1, true))
  end)

  it('reports a core that exits immediately', function()
    local client, err = core.ensure_running(fixture.core_config(sb, {
      -- valid binary, invalid subcommand ⇒ argparse exits non-zero at once
      core_cmd = { fixture.organize_bin(), 'definitely-not-a-subcommand' },
      spawn_timeout_ms = 8000,
    }))
    assert.is_nil(client)
    assert.is_true(rpc.is_error(err))
    assert.equals('CoreExited', err.kind)
    assert.is_string(err.hint)
  end)

  it('refuses to spawn when spawn = false and nothing is listening', function()
    local client, err = core.ensure_running(fixture.core_config(sb, { spawn = false }))
    assert.is_nil(client)
    assert.equals(rpc.KIND_CONNECT, err.kind)
    assert.is_truthy(err.message:find('not running', 1, true))
    assert.is_nil(core.spawned_pid())
  end)

  it('refuses a missing socket path with a configuration hint', function()
    local client, err = core.ensure_running({ socket_path = nil })
    assert.is_nil(client)
    assert.equals('ConfigError', err.kind)
    assert.is_truthy(rpc.format_error(err):find('setup', 1, true))
  end)

  it('notify_error emits exactly one ERROR notification', function()
    local seen = {}
    local original = vim.notify
    vim.notify = function(msg, level)
      seen[#seen + 1] = { msg = msg, level = level }
    end
    local ok = pcall(core.notify_error, rpc.error('CoreNotFound', 'organize-core is not installed', 'install it'))
    vim.notify = original
    assert.is_true(ok)
    assert.equals(1, #seen)
    assert.equals(vim.log.levels.ERROR, seen[1].level)
    assert.is_truthy(seen[1].msg:find('not installed', 1, true))
  end)
end)
