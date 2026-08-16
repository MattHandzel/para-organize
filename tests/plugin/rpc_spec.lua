-- Specs for para-organize.rpc — the JSON-RPC 2.0 client (spec 10 §2).
--
-- Run:
--   nvim --headless --noplugin -u tests/plugin/minimal_init_rpc.lua \
--        -c "PlenaryBustedFile tests/plugin/rpc_spec.lua"
--
-- Most of these run against the REAL core (`.venv/bin/organize serve`) over a
-- throwaway fixture vault, because the server IS the contract. The handful
-- that need a misbehaving peer (wrong apiVersion, mid-request death) use a
-- tiny libuv fake so they stay deterministic.

local uv = vim.uv or vim.loop
local here = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ':p:h')
local fixture = dofile(here .. '/rpc_core_fixture.lua')

local rpc = require('para-organize.rpc')

---------------------------------------------------------------------------
-- a deliberately misbehaving peer
---------------------------------------------------------------------------

--- Minimal unix-socket server that greets and then does whatever the test
--- needs (usually: nothing).
local function fake_core(path, opts)
  opts = opts or {}
  local srv = uv.new_pipe(false)
  local conns = {}
  assert(srv:bind(path))
  assert(srv:listen(16, function()
    local sock = uv.new_pipe(false)
    srv:accept(sock)
    conns[#conns + 1] = sock
    if opts.handshake ~= false then
      sock:write(opts.handshake or '{"apiVersion": 1}\n')
    end
  end))
  local fake = { conns = conns }
  function fake.drop()
    for _, sock in ipairs(conns) do
      pcall(function()
        if not sock:is_closing() then
          sock:close()
        end
      end)
    end
    for i = #conns, 1, -1 do
      conns[i] = nil
    end
  end
  function fake.close()
    fake.drop()
    pcall(function()
      if not srv:is_closing() then
        srv:close()
      end
    end)
    vim.fn.delete(path)
  end
  return fake
end

---------------------------------------------------------------------------

describe('rpc newline framing', function()
  it('reassembles a message split across chunks', function()
    local dec = rpc.new_decoder()
    assert.same({}, dec:feed('{"jsonrpc":"2.0","id":1,"res'))
    assert.same({}, dec:feed('ult":{"ok":true}'))
    local out = dec:feed('}\n')
    assert.equals(1, #out)
    assert.is_true(out[1].ok)
    assert.equals(1, out[1].value.id)
    assert.is_true(out[1].value.result.ok)
  end)

  it('yields several messages from one chunk and keeps the remainder', function()
    local dec = rpc.new_decoder()
    local out = dec:feed('{"id":1}\n{"id":2}\n{"id":3')
    assert.equals(2, #out)
    assert.equals(1, out[1].value.id)
    assert.equals(2, out[2].value.id)
    local rest = dec:feed('}\n')
    assert.equals(1, #rest)
    assert.equals(3, rest[1].value.id)
  end)

  it('delivers one byte at a time without losing anything', function()
    local dec = rpc.new_decoder()
    local line = '{"jsonrpc":"2.0","apiVersion":1,"id":7,"result":[1,2,3]}\n'
    local got = {}
    for i = 1, #line do
      for _, item in ipairs(dec:feed(line:sub(i, i))) do
        got[#got + 1] = item
      end
    end
    assert.equals(1, #got)
    assert.equals(7, got[1].value.id)
    assert.same({ 1, 2, 3 }, got[1].value.result)
  end)

  it('reports a malformed line as an error instead of raising', function()
    local dec = rpc.new_decoder()
    local out = dec:feed('this is not json\n{"id":2}\n')
    assert.equals(2, #out)
    assert.is_false(out[1].ok)
    assert.equals(rpc.KIND_PROTOCOL, out[1].err.kind)
    assert.is_true(out[2].ok)
    assert.equals(2, out[2].value.id)
  end)

  it('ignores blank lines', function()
    local dec = rpc.new_decoder()
    assert.same({}, dec:feed('\n\n'))
  end)
end)

describe('rpc errors', function()
  it('renders one clear line plus a hint, never a stack trace', function()
    local err = rpc.error('VaultError', 'note not found: x.md', 'check the path')
    assert.is_true(rpc.is_error(err))
    local text = tostring(err)
    assert.is_truthy(text:find('note not found', 1, true))
    assert.is_truthy(text:find('VaultError', 1, true))
    assert.is_truthy(text:find('hint: check the path', 1, true))
    assert.is_nil(text:find('stack traceback', 1, true))
  end)
end)

describe('rpc against a live organize-core', function()
  local sb, core, client

  before_each(function()
    sb = fixture.build_vault(fixture.sandbox())
    core = fixture.start_core(sb)
    local err
    client, err = rpc.connect(sb.socket, { timeout_ms = 10000 })
    assert(client, 'connect failed: ' .. tostring(err and err.message) .. '\n' .. fixture.read_log(sb))
  end)

  after_each(function()
    if client then
      client:close()
      client = nil
    end
    if core then
      core.stop()
      core = nil
    end
    fixture.cleanup(sb)
  end)

  it('completes the apiVersion handshake', function()
    assert.equals(1, client.api_version)
    assert.is_true(client:is_alive())
  end)

  it('session.start returns the capture backlog', function()
    local res, err = client:request_sync('session.start', {})
    assert.is_nil(err)
    assert.is_string(res.session_id)
    assert.is_true(#res.captures > 0)
    assert.is_string(res.captures[1].path)
    assert.equals('capture', res.captures[1].para_type)
  end)

  it('suggest.for_note returns ranked destinations', function()
    local res, err = client:request_sync('suggest.for_note', { path = 'capture/raw_capture/scalar-tags.md' })
    assert.is_nil(err)
    assert.is_true(#res > 0)
    assert.is_string(res[1].path)
    assert.is_number(res[1].score)
  end)

  it('note.get returns record + frontmatter + body', function()
    local res, err = client:request_sync('note.get', { path = 'capture/raw_capture/scalar-tags.md' })
    assert.is_nil(err)
    assert.is_false(res.parse_error)
    assert.is_string(res.body)
    assert.equals('Older manual note', res.frontmatter.title)
  end)

  it('decodes JSON null inside objects as Lua nil, not a truthy vim.NIL', function()
    -- Every NoteRecord carries nullable fields (capture_id, location…).
    -- If those arrived as vim.NIL the UI's `if record.capture_id` checks
    -- would all be true — a silent wrong answer.
    local res = client:request_sync('note.get', { path = 'capture/raw_capture/scalar-tags.md' })
    assert.is_nil(res.record.capture_id)
    assert.is_nil(res.record.location)
  end)

  it('op.move moves the file end-to-end', function()
    local src = sb.vault .. '/capture/raw_capture/scalar-tags.md'
    local dst = sb.vault .. '/projects/blog/scalar-tags.md'
    assert.is_truthy(uv.fs_stat(src))
    assert.is_nil(uv.fs_stat(dst))

    local res, err = client:request_sync('op.move', {
      path = 'capture/raw_capture/scalar-tags.md',
      destination = 'projects/blog',
    })
    assert.is_nil(err)
    assert.is_true(res.ok)
    assert.equals('move', res.operation)
    assert.is_truthy(uv.fs_stat(dst))
    assert.is_nil(uv.fs_stat(src)) -- original archived by the core
  end)

  it('maps a domain error to {kind, hint} (nonexistent note ⇒ VaultError)', function()
    local res, err = client:request_sync('note.get', { path = 'capture/raw_capture/nope.md' })
    assert.is_nil(res)
    assert.is_true(rpc.is_error(err))
    assert.equals('VaultError', err.kind)
    assert.equals(-32000, err.code)
    assert.is_string(err.hint)
    assert.is_true(#err.hint > 0)
    assert.equals('note.get', err.method)
    assert.is_true(client:is_alive()) -- a domain error never drops the socket
  end)

  it('maps a protocol error to the same {kind, hint} shape', function()
    local _, err = client:request_sync('does.not.exist', {})
    assert.equals('MethodNotFound', err.kind)
    assert.equals(-32601, err.code)
    assert.is_string(err.hint)
    assert.is_table(err.data.known_methods)

    local _, perr = client:request_sync('note.get', {})
    assert.equals(-32602, perr.code)
    assert.equals('InvalidParams', perr.kind)
  end)

  it('serves async requests through a callback', function()
    local got
    local id = client:request('index.reindex', {}, function(err, result)
      got = { err = err, result = result }
    end)
    assert.is_number(id)
    assert.is_truthy(fixture.wait_for(10000, function()
      return got
    end))
    assert.is_nil(got.err)
    assert.is_number(got.result.total)
  end)

  it('keeps concurrent in-flight requests correlated by id', function()
    local results = {}
    client:request('note.get', { path = 'capture/raw_capture/scalar-tags.md' }, function(_, r)
      results.a = r
    end)
    client:request('note.get', { path = 'capture/raw_capture/metadata-map.md' }, function(_, r)
      results.b = r
    end)
    client:request('does.not.exist', {}, function(e)
      results.c = e
    end)
    assert.is_truthy(fixture.wait_for(10000, function()
      return results.a and results.b and results.c
    end))
    assert.equals('Older manual note', results.a.frontmatter.title)
    assert.equals('meta-map', results.b.frontmatter.id)
    assert.equals('MethodNotFound', results.c.kind)
  end)

  it('routes server pushes to event callbacks after events.subscribe', function()
    local seen = {}
    client:on('*', function(event, data)
      seen[#seen + 1] = { event = event, data = data }
    end)
    local res, err = client:subscribe_sync()
    assert.is_nil(err)
    assert.is_true(vim.tbl_contains(res.subscribed, rpc.EVENT_INDEX_UPDATED))

    client:request_sync('index.reindex', {})
    assert.is_truthy(fixture.wait_for(10000, function()
      for _, e in ipairs(seen) do
        if e.event == rpc.EVENT_INDEX_UPDATED then
          return e
        end
      end
      return nil
    end))

    local kinds = {}
    for _, e in ipairs(seen) do
      kinds[e.event] = e.data
    end
    assert.is_truthy(kinds[rpc.EVENT_INDEX_UPDATED])
    assert.is_number(kinds[rpc.EVENT_INDEX_UPDATED].stats.total)
    assert.is_truthy(kinds[rpc.EVENT_OP_PROGRESS])
    assert.equals('index.reindex', kinds[rpc.EVENT_OP_PROGRESS].method)
  end)

  it('fails cleanly once the client is closed', function()
    client:close()
    assert.is_false(client:is_alive())
    local res, err = client:request_sync('session.start', {})
    assert.is_nil(res)
    assert.equals(rpc.KIND_CLOSED, err.kind)
    client = nil
  end)
end)

describe('rpc degradation', function()
  -- Sandbox/fake/client teardown lives in after_each so a FAILING assertion
  -- still leaves no socket, no temp dir and no open handle behind.
  local sb, fake, client

  local function start_fake(opts)
    fake = fake_core(sb.socket, opts)
    return fake
  end

  before_each(function()
    sb = fixture.sandbox()
    fake, client = nil, nil
  end)

  after_each(function()
    if client then
      client:close()
      client = nil
    end
    if fake then
      fake.close()
      fake = nil
    end
    fixture.cleanup(sb)
  end)

  it('returns a clear error (not a crash) when nothing is listening', function()
    local c, err = rpc.connect(sb.socket .. '.absent', { timeout_ms = 1000 })
    assert.is_nil(c)
    assert.is_true(rpc.is_error(err))
    assert.equals(rpc.KIND_CONNECT, err.kind)
    assert.is_truthy(tostring(err):find('organize serve', 1, true))
  end)

  it('refuses an empty socket path with a configuration hint', function()
    local c, err = rpc.connect('', {})
    assert.is_nil(c)
    assert.equals(rpc.KIND_CONNECT, err.kind)
    assert.is_truthy(tostring(err):find('setup', 1, true))
  end)

  it('refuses a MAJOR apiVersion mismatch, naming both versions', function()
    start_fake({ handshake = '{"apiVersion": 99}\n' })
    local c, err = rpc.connect(sb.socket, { timeout_ms = 3000 })
    assert.is_nil(c)
    assert.equals(rpc.KIND_VERSION, err.kind)
    assert.is_truthy(err.message:find('99', 1, true))
    assert.is_truthy(err.message:find('1', 1, true))
  end)

  it('times out rather than hanging when the peer never greets', function()
    start_fake({ handshake = false })
    local c, err = rpc.connect(sb.socket, { timeout_ms = 300 })
    assert.is_nil(c)
    assert.equals(rpc.KIND_TIMEOUT, err.kind)
  end)

  it('reconnects once when the peer drops, failing the in-flight request', function()
    start_fake({})
    client = assert(rpc.connect(sb.socket, { timeout_ms = 3000 }))

    local got
    client:request('note.get', { path = 'x.md' }, function(err)
      got = err
    end)
    -- let the request reach the peer, then yank the connection
    vim.wait(200, function()
      return false
    end, 20)
    fake.drop()

    assert.is_truthy(fixture.wait_for(5000, function()
      return got
    end))
    assert.equals(rpc.KIND_DISCONNECTED, got.kind)
    assert.is_string(got.hint)
    -- the peer is still listening, so the one permitted reconnect succeeded
    assert.is_truthy(fixture.wait_for(5000, function()
      return client:is_alive() or nil
    end))
    assert.is_false(client.closed)
  end)

  it('degrades after the single reconnect attempt fails', function()
    start_fake({})
    local closed
    client = assert(rpc.connect(sb.socket, {
      timeout_ms = 500,
      on_close = function(err)
        closed = err
      end,
    }))

    local got
    client:request('note.get', { path = 'x.md' }, function(err)
      got = err
    end)
    vim.wait(200, function()
      return false
    end, 20)
    fake.close() -- drop the connection AND stop listening

    assert.is_truthy(fixture.wait_for(5000, function()
      return got
    end))
    assert.is_truthy(fixture.wait_for(5000, function()
      return client.closed or nil
    end))
    assert.is_false(client:is_alive())
    assert.is_truthy(fixture.wait_for(5000, function()
      return closed
    end))
    assert.is_true(rpc.is_error(closed))
  end)

  it('surfaces a request timeout without wedging the client', function()
    start_fake({})
    client = assert(rpc.connect(sb.socket, { timeout_ms = 3000 }))
    local res, err = client:request_sync('note.get', { path = 'x.md' }, 200)
    assert.is_nil(res)
    assert.equals(rpc.KIND_TIMEOUT, err.kind)
    assert.is_true(client:is_alive())
  end)
end)
