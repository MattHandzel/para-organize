--- JSON-RPC 2.0 client for organize-core (spec 10 §2).
---
--- THIN CLIENT LAW (spec 10 §1): this module is the ONLY way the plugin
--- changes state. It speaks newline-delimited JSON-RPC 2.0 over a unix
--- socket and never touches a vault file itself.
---
--- Wire contract (pinned against `src/organize_core/server.py`, which is
--- the authority — none of this is guessed):
---
---   * On connect the server sends ONE line `{"apiVersion": 1}` before
---     anything else. A MAJOR mismatch is a hard refusal here, with a
---     message that names both versions (10 §2).
---   * Every response line is `{"jsonrpc":"2.0","apiVersion":1,"id":…}`
---     carrying either `result` or `error`.
---   * Errors carry `error.data = {kind, hint}` for EVERY code — domain
---     errors (-32000) and protocol errors (-32600…-32700) alike — so one
---     client code path renders taxonomy + hint. We surface that as a
---     structured Lua error table (see `M.error`).
---   * Server→client pushes are JSON-RPC notifications with
---     `method = "event"` and `params = {event = <name>, data = {…}}`.
---     Subscribe with the `events.subscribe` method.
---
--- Concurrency notes:
---   * libuv read callbacks run in Neovim's FAST EVENT context, where
---     `vim.api`/`vim.wait` are forbidden. Everything this module does on
---     the read path is pure Lua; every USER callback (request callbacks,
---     event handlers, on_close) is registered through `vim.schedule_wrap`
---     so callers may freely call `vim.api`/`vim.notify` from them.
---   * Reconnect is therefore fully ASYNC (no `vim.wait` off the read
---     path). Only `M.connect` and `Client:request_sync`, which run in
---     normal context, use `vim.wait`.
---
--- @module para-organize.rpc

local uv = vim.uv or vim.loop

local M = {}

--- Major API version this client speaks (spec 10 §2).
M.API_VERSION = 1

--- Default per-request / connect timeout.
M.DEFAULT_TIMEOUT_MS = 5000

--- Refuse absurd lines rather than growing the reassembly buffer forever;
--- mirrors the server's own 8 MiB per-line cap.
M.MAX_LINE_BYTES = 8 * 1024 * 1024

--- Method name of a server→client push (server.py EVENT_METHOD).
M.EVENT_METHOD = 'event'

--- Event names the core pushes (server.py EVENT_TYPES).
M.EVENT_INDEX_UPDATED = 'index-updated'
M.EVENT_OP_PROGRESS = 'op-progress'

-- Client-side error kinds. Server-side kinds arrive in `error.data.kind`
-- (VaultError, ServerError, MethodNotFound, InvalidParams, …) and are used
-- verbatim, so callers branch on ONE field regardless of origin.
M.KIND_CONNECT = 'ConnectError'
M.KIND_VERSION = 'ApiVersionMismatch'
M.KIND_TIMEOUT = 'Timeout'
M.KIND_DISCONNECTED = 'Disconnected'
M.KIND_TRANSPORT = 'TransportError'
M.KIND_PROTOCOL = 'ProtocolError'
M.KIND_CLOSED = 'ClientClosed'

---------------------------------------------------------------------------
-- Errors
---------------------------------------------------------------------------

local error_mt = {}
error_mt.__index = error_mt
error_mt.__tostring = function(err)
  return M.format_error(err)
end

--- Build a structured error. Never raises; every failure path in this
--- module returns one of these as the second return value.
--- @param kind string taxonomy name (client kind or server `data.kind`)
--- @param message string human sentence
--- @param hint string|nil actionable next step
--- @param extra table|nil extra fields (code, data, method, path)
--- @return table
function M.error(kind, message, hint, extra)
  local err = setmetatable({}, error_mt)
  err.kind = kind
  err.message = message
  err.hint = hint
  for k, v in pairs(extra or {}) do
    err[k] = v
  end
  return err
end

--- True when `value` is an error table produced by this module.
function M.is_error(value)
  return type(value) == 'table' and getmetatable(value) == error_mt
end

--- One-line-plus-hint rendering, suitable for `vim.notify` (10 §1: one
--- clear error, never a stack trace).
function M.format_error(err)
  if err == nil then
    return 'para-organize: unknown error'
  end
  if type(err) ~= 'table' then
    return 'para-organize: ' .. tostring(err)
  end
  local out = 'para-organize: ' .. tostring(err.message or 'unknown error')
  if err.kind then
    out = out .. ' [' .. tostring(err.kind) .. ']'
  end
  if err.hint and err.hint ~= '' then
    out = out .. '\nhint: ' .. tostring(err.hint)
  end
  return out
end

---------------------------------------------------------------------------
-- Newline framing
---------------------------------------------------------------------------

local Decoder = {}
Decoder.__index = Decoder

--- Stream decoder for newline-delimited JSON.
---
--- Exposed (and unit-tested) on its own because chunk boundaries from
--- libuv fall wherever the kernel put them: a single `read` may deliver
--- half a request, three whole ones, or one byte at a time.
--- @return table decoder with `:feed(chunk)`
function M.new_decoder()
  return setmetatable({ buf = '' }, Decoder)
end

--- Feed one chunk; returns an array of `{ok = true, value = <table>}` and
--- `{ok = false, err = <error>}` entries, in arrival order. A malformed
--- line yields an error entry rather than raising, so one bad line cannot
--- kill the connection.
--- @param chunk string
--- @return table[]
function Decoder:feed(chunk)
  local out = {}
  if chunk == nil or chunk == '' then
    return out
  end
  self.buf = self.buf .. chunk
  while true do
    local nl = self.buf:find('\n', 1, true)
    if not nl then
      break
    end
    local line = self.buf:sub(1, nl - 1)
    self.buf = self.buf:sub(nl + 1)
    if line:match('^%s*$') == nil then
      local ok, decoded = pcall(vim.json.decode, line, { luanil = { object = true, array = false } })
      if ok and type(decoded) == 'table' then
        out[#out + 1] = { ok = true, value = decoded }
      else
        out[#out + 1] = {
          ok = false,
          err = M.error(
            M.KIND_PROTOCOL,
            'organize-core sent a line that is not a JSON object',
            'this is a core bug or a socket collision — check that ' .. 'nothing else is listening on the socket',
            { line = line:sub(1, 200) }
          ),
        }
      end
    end
  end
  if #self.buf > M.MAX_LINE_BYTES then
    self.buf = ''
    out[#out + 1] = {
      ok = false,
      err = M.error(
        M.KIND_PROTOCOL,
        'organize-core sent more than ' .. M.MAX_LINE_BYTES .. ' bytes without a newline',
        'the stream is not newline-delimited JSON-RPC — reconnect'
      ),
    }
  end
  return out
end

---------------------------------------------------------------------------
-- Client
---------------------------------------------------------------------------

local Client = {}
Client.__index = Client
M.Client = Client

--- `vim.json.encode({})` emits `[]`, but every core method takes an OBJECT
--- of params; an array param is an INVALID_PARAMS refusal. Normalise the
--- empty case so callers can pass `nil` or `{}` freely.
local function normalize_params(params)
  if params == nil then
    return vim.empty_dict()
  end
  if type(params) ~= 'table' then
    return params
  end
  if next(params) == nil then
    return vim.empty_dict()
  end
  return params
end

local function server_error(method, err_obj)
  local data = type(err_obj.data) == 'table' and err_obj.data or {}
  local kind = data.kind
  if type(kind) ~= 'string' or kind == '' then
    kind = 'RpcError'
  end
  return M.error(kind, tostring(err_obj.message or 'organize-core returned an error'), data.hint, {
    code = err_obj.code,
    data = data,
    method = method,
  })
end

--- @return table client
local function new_client(socket_path, opts)
  local self = setmetatable({}, Client)
  self.socket_path = socket_path
  self.opts = opts or {}
  self.timeout_ms = self.opts.timeout_ms or M.DEFAULT_TIMEOUT_MS
  self.pending = {}
  self.next_id = 1
  self.handlers = {}
  self.subscriptions = nil
  self.closed = false
  self.connected = false
  self.api_version = nil
  self._reconnect_used = false
  if type(self.opts.on_event) == 'function' then
    self:on('*', self.opts.on_event)
  end
  if type(self.opts.on_close) == 'function' then
    self._on_close = vim.schedule_wrap(self.opts.on_close)
  end
  return self
end

-- --- dialing ------------------------------------------------------------

function Client:_finish_dial(err)
  local cb = self._dial_cb
  self._dial_cb = nil
  if self._dial_timer then
    pcall(function()
      self._dial_timer:stop()
      self._dial_timer:close()
    end)
    self._dial_timer = nil
  end
  if cb then
    cb(err)
  end
end

function Client:_close_pipe()
  local pipe = self.pipe
  self.pipe = nil
  self.connected = false
  if pipe then
    pcall(function()
      pipe:read_stop()
    end)
    pcall(function()
      if not pipe:is_closing() then
        pipe:close()
      end
    end)
  end
end

--- Open the socket and wait (asynchronously) for the handshake line.
--- `cb(err)` fires exactly once: nil on a completed handshake.
function Client:_dial(timeout_ms, cb)
  self._dial_cb = cb
  self.decoder = M.new_decoder()

  local pipe = uv.new_pipe(false)
  if not pipe then
    return self:_finish_dial(M.error(M.KIND_CONNECT, 'could not allocate a socket handle', 'this is a Neovim/libuv failure, not a core one'))
  end
  self.pipe = pipe

  local timer = uv.new_timer()
  self._dial_timer = timer
  if timer then
    timer:start(timeout_ms, 0, function()
      if self._dial_cb then
        self:_close_pipe()
        self:_finish_dial(M.error(
          M.KIND_TIMEOUT,
          ('organize-core did not complete the handshake within %dms (%s)'):format(timeout_ms, self.socket_path),
          'is another program listening on that socket? check `:checkhealth para-organize`'
        ))
      end
    end)
  end

  local ok, perr = pcall(function()
    pipe:connect(self.socket_path, function(cerr)
      if cerr then
        self:_close_pipe()
        self:_finish_dial(M.error(
          M.KIND_CONNECT,
          ('cannot reach organize-core at %s (%s)'):format(self.socket_path, tostring(cerr)),
          'start it with `organize serve`, or check `core.socket_path` in setup()'
        ))
        return
      end
      self.connected = true
      local rok, rerr = pcall(function()
        pipe:read_start(function(rderr, chunk)
          self:_on_read(rderr, chunk)
        end)
      end)
      if not rok then
        self:_close_pipe()
        self:_finish_dial(M.error(M.KIND_TRANSPORT, 'cannot read from organize-core: ' .. tostring(rerr), 'the socket closed immediately after connecting'))
      end
    end)
  end)
  if not ok then
    self:_close_pipe()
    self:_finish_dial(M.error(
      M.KIND_CONNECT,
      ('cannot reach organize-core at %s (%s)'):format(self.socket_path, tostring(perr)),
      'start it with `organize serve`, or check `core.socket_path` in setup()'
    ))
  end
end

-- --- read path (FAST EVENT context — pure Lua only) ---------------------

--- libuv errnos that mean "the peer went away". A core that is SIGKILLed
--- (or a socket yanked out from under us) surfaces as ECONNRESET, not as a
--- clean EOF — callers must not need two branches for one situation, so
--- both become KIND_DISCONNECTED.
local PEER_CLOSED = {
  ECONNRESET = true,
  ECONNABORTED = true,
  EPIPE = true,
  ENOTCONN = true,
  ESHUTDOWN = true,
  EOF = true,
}

local function is_peer_close(err)
  local code = tostring(err):match('^(%u+)')
  return code ~= nil and PEER_CLOSED[code] == true
end

function Client:_on_read(err, chunk)
  if err then
    if is_peer_close(err) then
      return self:_on_disconnect(M.error(
        M.KIND_DISCONNECTED,
        'organize-core closed the connection (' .. tostring(err) .. ')',
        'it may have crashed, idled out or been stopped; the next call will re-spawn it'
      ))
    end
    return self:_on_disconnect(M.error(M.KIND_TRANSPORT, 'organize-core connection error: ' .. tostring(err), 'the core may have crashed — check its log'))
  end
  if chunk == nil then
    return self:_on_disconnect(M.error(M.KIND_DISCONNECTED, 'organize-core closed the connection', 'it may have idled out or been stopped; the next call will re-spawn it'))
  end
  for _, item in ipairs(self.decoder:feed(chunk)) do
    if item.ok then
      self:_handle(item.value)
    else
      self:_report(item.err)
    end
  end
end

--- Out-of-band protocol noise (a line that is not a JSON object). Reported
--- to the optional `on_protocol_error` hook and otherwise dropped: one bad
--- line must not take the connection down.
function Client:_report(err)
  if type(self.opts.on_protocol_error) == 'function' then
    vim.schedule(function()
      self.opts.on_protocol_error(err)
    end)
  end
end

function Client:_handshake(msg)
  local raw = msg.apiVersion
  local version = tonumber(raw)
  if version == nil then
    self:_close_pipe()
    return self:_finish_dial(M.error(
      M.KIND_PROTOCOL,
      'organize-core sent a handshake without a usable apiVersion (' .. tostring(raw) .. ')',
      'the process on this socket is not an organize-core server'
    ))
  end
  local major = math.floor(version)
  self.api_version = major
  if major ~= M.API_VERSION then
    self:_close_pipe()
    return self:_finish_dial(M.error(
      M.KIND_VERSION,
      ('organize-core speaks API version %d, this plugin speaks %d — refusing to continue'):format(major, M.API_VERSION),
      'upgrade the plugin or the core so both share the major API version'
    ))
  end
  self:_finish_dial(nil)
end

function Client:_handle(msg)
  -- The handshake is the first line of every connection and is the only
  -- line with neither `id` nor `method`.
  if self._dial_cb ~= nil and msg.jsonrpc == nil and msg.method == nil and msg.id == nil then
    return self:_handshake(msg)
  end

  if msg.method == M.EVENT_METHOD then
    local params = type(msg.params) == 'table' and msg.params or {}
    return self:_emit(params.event, params.data)
  end

  local id = msg.id
  if id == nil then
    return
  end
  local entry = self.pending[id]
  if entry == nil then
    return
  end
  self.pending[id] = nil
  if msg.error ~= nil then
    if entry.cb then
      entry.cb(server_error(entry.method, msg.error), nil)
    end
  else
    if entry.cb then
      entry.cb(nil, msg.result)
    end
  end
end

function Client:_emit(event, data)
  if type(event) ~= 'string' then
    return
  end
  for _, fn in ipairs(self.handlers[event] or {}) do
    fn(event, data)
  end
  for _, fn in ipairs(self.handlers['*'] or {}) do
    fn(event, data)
  end
end

-- --- disconnect + reconnect-once ---------------------------------------

--- Fail every pending request whose bytes already reached the server.
---
--- Deliberate: a request that WAS written may have been applied before the
--- core died (`op.move` renames a file, `meta.set` rewrites frontmatter).
--- Silently replaying it on the new connection could apply it twice, which
--- spec 05's safety invariants forbid. Requests whose write never landed
--- are safe, and those alone are resent.
function Client:_fail_sent(err)
  local resend = {}
  for id, entry in pairs(self.pending) do
    if entry.sent then
      self.pending[id] = nil
      if entry.cb then
        entry.cb(err, nil)
      end
    else
      resend[#resend + 1] = id
    end
  end
  return resend
end

function Client:_fail_all(err)
  for id, entry in pairs(self.pending) do
    self.pending[id] = nil
    if entry.cb then
      entry.cb(err, nil)
    end
  end
end

function Client:_on_disconnect(err)
  if self.closed then
    return
  end
  self:_close_pipe()
  if self._dial_cb then
    return self:_finish_dial(err)
  end

  local resend = self:_fail_sent(err)

  local may_retry = self.opts.reconnect ~= false and not self._reconnect_used
  if not may_retry then
    self.closed = true
    self:_fail_all(err)
    if self._on_close then
      self._on_close(err)
    end
    return
  end

  self._reconnect_used = true
  self:_dial(self.opts.reconnect_timeout_ms or self.timeout_ms, function(derr)
    if derr then
      self.closed = true
      self:_fail_all(derr)
      if self._on_close then
        self._on_close(derr)
      end
      return
    end
    -- Restore subscriptions, then replay only the never-written requests.
    if self.subscriptions then
      self:_send('events.subscribe', { events = self.subscriptions }, nil)
    end
    for _, id in ipairs(resend) do
      if self.pending[id] then
        self:_flush(id)
      end
    end
  end)
end

-- --- writing ------------------------------------------------------------

function Client:_flush(id)
  local entry = self.pending[id]
  if not entry then
    return
  end
  local pipe = self.pipe
  if not pipe or not self.connected then
    return
  end
  local ok, werr = pcall(function()
    pipe:write(entry.line, function(err)
      if err then
        local kind = is_peer_close(err) and M.KIND_DISCONNECTED or M.KIND_TRANSPORT
        local e = M.error(kind, 'could not send ' .. entry.method .. ' to organize-core: ' .. tostring(err), 'the core connection dropped mid-write')
        self:_on_disconnect(e)
      else
        local cur = self.pending[id]
        if cur then
          cur.sent = true
        end
      end
    end)
  end)
  if not ok then
    self:_on_disconnect(M.error(M.KIND_TRANSPORT, 'could not send ' .. entry.method .. ' to organize-core: ' .. tostring(werr), 'the core connection is gone'))
  end
end

function Client:_send(method, params, cb)
  if self.closed then
    local err = M.error(M.KIND_CLOSED, 'the organize-core connection is closed', 'call core.ensure_running() to reconnect')
    if cb then
      cb(err, nil)
    end
    return nil, err
  end
  local id = self.next_id
  self.next_id = id + 1
  local payload = {
    jsonrpc = '2.0',
    apiVersion = M.API_VERSION,
    id = id,
    method = method,
    params = normalize_params(params),
  }
  local ok, line = pcall(vim.json.encode, payload)
  if not ok then
    local err = M.error(M.KIND_PROTOCOL, 'cannot encode params for ' .. tostring(method) .. ': ' .. tostring(line), 'params must be JSON-encodable (no functions or cycles)')
    if cb then
      cb(err, nil)
    end
    return nil, err
  end
  self.pending[id] = { method = method, cb = cb, line = line .. '\n', sent = false }
  self:_flush(id)
  return id
end

---------------------------------------------------------------------------
-- Public client API
---------------------------------------------------------------------------

--- Fire a request; `cb(err, result)` runs on the main loop (safe for
--- `vim.api`). Returns the request id, or nil + error when the client is
--- already closed.
--- @param method string
--- @param params table|nil
--- @param cb fun(err: table|nil, result: any)|nil
--- @return integer|nil id, table|nil err
function Client:request(method, params, cb)
  return self:_send(method, params, cb and vim.schedule_wrap(cb) or nil)
end

--- Blocking request, for tests and for setup-time handshakes.
---
--- MUST NOT be called from a fast event context (libuv callback); use
--- `Client:request` there.
--- @param method string
--- @param params table|nil
--- @param timeout_ms integer|nil
--- @return any result, table|nil err
function Client:request_sync(method, params, timeout_ms)
  local done, result, err = false, nil, nil
  local id, serr = self:_send(method, params, function(e, r)
    err, result, done = e, r, true
  end)
  if not id then
    return nil, serr or err
  end
  local ok = vim.wait(timeout_ms or self.timeout_ms, function()
    return done
  end, 5)
  if not ok then
    self.pending[id] = nil
    return nil,
      M.error(
        M.KIND_TIMEOUT,
        ('organize-core did not answer %s within %dms'):format(method, timeout_ms or self.timeout_ms),
        'the core may be busy reindexing a large vault — retry, or raise core.timeout_ms'
      )
  end
  if err then
    return nil, err
  end
  return result
end

--- Register a push handler. `event` is an event name or `'*'` for all.
--- Handlers run on the main loop.
--- @param event string
--- @param fn fun(event: string, data: table)
function Client:on(event, fn)
  local list = self.handlers[event]
  if not list then
    list = {}
    self.handlers[event] = list
  end
  list[#list + 1] = vim.schedule_wrap(fn)
  return self
end

--- Subscribe to server pushes (spec 10 §2). `events` defaults to every
--- event the core publishes. Remembered so a reconnect restores it.
--- @param events string[]|nil
--- @param cb fun(err: table|nil, result: any)|nil
function Client:subscribe(events, cb)
  self.subscriptions = events or { M.EVENT_INDEX_UPDATED, M.EVENT_OP_PROGRESS }
  return self:request('events.subscribe', { events = self.subscriptions }, cb)
end

--- Blocking form of `subscribe`.
function Client:subscribe_sync(events, timeout_ms)
  self.subscriptions = events or { M.EVENT_INDEX_UPDATED, M.EVENT_OP_PROGRESS }
  return self:request_sync('events.subscribe', { events = self.subscriptions }, timeout_ms)
end

--- True while the socket is usable.
function Client:is_alive()
  return not self.closed and self.connected and self.pipe ~= nil
end

--- Close the socket and fail anything still in flight. Idempotent.
function Client:close()
  if self.closed then
    return
  end
  self.closed = true
  self._dial_cb = nil
  if self._dial_timer then
    pcall(function()
      self._dial_timer:stop()
      self._dial_timer:close()
    end)
    self._dial_timer = nil
  end
  self:_close_pipe()
  self:_fail_all(M.error(M.KIND_CLOSED, 'the organize-core connection was closed by the client', 'call core.ensure_running() to reconnect'))
end

---------------------------------------------------------------------------
-- Connecting
---------------------------------------------------------------------------

--- Connect to a running core and complete the apiVersion handshake.
---
--- Never raises: a missing/unreachable/incompatible core comes back as
--- `nil, err` for the caller to `vim.notify` (spec 10 §1).
--- @param socket_path string
--- @param opts table|nil { timeout_ms, reconnect, on_event, on_close, on_protocol_error }
--- @return table|nil client, table|nil err
function M.connect(socket_path, opts)
  opts = opts or {}
  if type(socket_path) ~= 'string' or socket_path == '' then
    return nil,
      M.error(M.KIND_CONNECT, 'no organize-core socket path configured', "set `core.socket_path` in require('para-organize').setup{}")
  end
  local timeout_ms = opts.timeout_ms or M.DEFAULT_TIMEOUT_MS
  local client = new_client(socket_path, opts)

  local done, derr = false, nil
  client:_dial(timeout_ms, function(err)
    derr, done = err, true
  end)
  local ok = vim.wait(timeout_ms + 100, function()
    return done
  end, 5)
  if not ok then
    client:close()
    return nil,
      M.error(
        M.KIND_TIMEOUT,
        ('organize-core did not answer on %s within %dms'):format(socket_path, timeout_ms),
        'the socket exists but nothing is serving it — remove it and retry'
      )
  end
  if derr then
    client.closed = true
    client:_close_pipe()
    return nil, derr
  end
  return client
end

return M
