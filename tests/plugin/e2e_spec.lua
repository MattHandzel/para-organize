--- THE spec 09 §3 END-TO-END GATE.
---
--- > "End-to-end session test in headless Neovim: build fixture vault →
--- >  `start` → accept top suggestion → assert file moved, archived original,
--- >  tag added, `processing_status: organized`, learning.json updated,
--- >  operations.log line written → `next` auto-loaded."  — spec 09 §3
---
--- Nothing is mocked below the plugin's own surface. A real `organize serve`
--- is spawned BY THE PLUGIN (`core.ensure_running`) against a real fixture
--- vault built by the same `tests/conftest.py:build_fixture_vault` the python
--- suite uses; every assertion after the accept is made against BYTES ON
--- DISK, never against an RPC reply — a green result here means the whole
--- stack agreed, not that one layer reported success.
---
--- SAFETY: the vault, the core's config/state/runtime dirs and its socket all
--- live inside one throwaway sandbox; `after_each` kills the core and deletes
--- the tree whether the test passed or failed.
---
--- Owned by the INTEGRATOR.

local helpers = require("helpers")

local uv = vim.uv or vim.loop

---------------------------------------------------------------------------
-- small utilities
---------------------------------------------------------------------------

--- Pump the event loop until `fn()` is truthy. Returns its value or nil.
local function until_true(fn, timeout_ms, what)
  local value
  local ok = vim.wait(timeout_ms or 20000, function()
    value = fn()
    return value ~= nil and value ~= false
  end, 20)
  if not ok then
    error("timed out waiting for " .. (what or "condition"), 2)
  end
  return value
end

local function read(path)
  local fd = assert(uv.fs_open(path, "r", 438), "cannot open " .. path)
  local stat = uv.fs_fstat(fd)
  local data = uv.fs_read(fd, stat.size, 0)
  uv.fs_close(fd)
  return data or ""
end

local function exists(path)
  return uv.fs_stat(path) ~= nil
end

local function basename(path)
  return path:match("([^/]+)$")
end

--- Every line of every file in `dir` (non-recursive), concatenated.
local function read_dir_lines(dir)
  local out = {}
  local handle = uv.fs_scandir(dir)
  if not handle then
    return out
  end
  while true do
    local name = uv.fs_scandir_next(handle)
    if not name then
      break
    end
    for line in (read(dir .. "/" .. name) .. "\n"):gmatch("([^\n]*)\n") do
      if line ~= "" then
        out[#out + 1] = line
      end
    end
  end
  return out
end

---------------------------------------------------------------------------

describe("E2E: a real organize session in headless Neovim (spec 09 §3)", function()
  local sb, config, state, actions, ui, commands, core, init

  before_each(function()
    -- Fresh module state for every test: these modules hold session state and
    -- a spawned process between calls.
    for _, name in ipairs({
      "para-organize",
      "para-organize.config",
      "para-organize.state",
      "para-organize.actions",
      "para-organize.ui",
      "para-organize.ui.render",
      "para-organize.commands",
      "para-organize.core",
      "para-organize.rpc",
      "para-organize.pickers",
    }) do
      package.loaded[name] = nil
    end

    init = require("para-organize")
    config = require("para-organize.config")
    state = require("para-organize.state")
    actions = require("para-organize.actions")
    ui = require("para-organize.ui")
    commands = require("para-organize.commands")
    core = require("para-organize.core")

    sb = helpers.sandbox()
    helpers.build_vault(sb)
    init.setup(helpers.plugin_config(sb))
  end)

  after_each(function()
    pcall(function()
      init.stop({ quiet = true })
    end)
    -- Kill the core THIS test spawned (core.stop only ever signals a process
    -- it started itself) and drop the sandbox, pass or fail.
    pcall(core.stop)
    pcall(state.reset)
    pcall(actions.reset)
    helpers.cleanup(sb)
  end)

  --- `:ParaOrganize start`, waited out to the point where the UI is mounted
  --- and the first capture's suggestions have arrived.
  local function start_session()
    commands.execute({ fargs = { "start" } })
    until_true(function()
      return state.has_session()
    end, 30000, "session.start to return captures")
    local session = state.get()
    until_true(function()
      return ui.is_mounted() and session.view == "suggestions" and #(session.suggestions or {}) > 0
    end, 30000, "the organize pane to render suggestions")
    return session
  end

  local function organize_lines()
    return vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false)
  end

  ---------------------------------------------------------------------------

  it("starts a session, spawns the core, and renders two panes with the top suggestion", function()
    local session = start_session()

    -- The plugin spawned the core itself (spec 10 §1 "auto-spawned by the
    -- Neovim client if not running") and is talking to it over the socket.
    assert.is_truthy(core.spawned_pid())
    assert.is_true(core.socket_exists(sb.socket))

    -- Two panes, both real windows (spec 03 §3).
    local wins = ui.current_wins()
    assert.is_true(vim.api.nvim_win_is_valid(wins.capture))
    assert.is_true(vim.api.nvim_win_is_valid(wins.organize))
    assert.are_not.equal(wins.capture, wins.organize)

    -- LEFT: the REAL capture file, not a scratch copy (spec 10 §4).
    local record = session.captures[session.current]
    local capture_buf = ui.current_bufs().capture
    assert.are.equal(record.path, vim.api.nvim_buf_get_name(capture_buf))
    assert.is_false(vim.bo[capture_buf].modified)

    -- RIGHT: the suggestion list, with the top suggestion's folder on screen.
    local top = session.suggestions[1]
    assert.is_truthy(top and top.path)
    local rendered = table.concat(organize_lines(), "\n")
    assert.is_truthy(rendered:find("Suggestions", 1, true))
    assert.is_truthy(rendered:find(basename(top.path), 1, true))
  end)

  it("accepts the top suggestion and every effect lands on disk (spec 03 §6, 05 §2)", function()
    local session = start_session()

    local record = session.captures[session.current]
    local source = record.path
    local filename = basename(source)
    local top = session.suggestions[1]
    local destination = top.path
    local dest_file = destination .. "/" .. filename
    local folder_name = basename(destination)

    assert.is_true(exists(source))
    assert.is_false(exists(dest_file))

    -- <CR> on the top suggestion.
    actions.accept()

    until_true(function()
      return exists(dest_file)
    end, 30000, "the moved copy to appear at " .. dest_file)

    ------------------------------------------------------------------
    -- 1. the file moved
    ------------------------------------------------------------------
    local moved = read(dest_file)
    assert.is_falsy(exists(source), "the original must not remain in the capture folder")

    ------------------------------------------------------------------
    -- 2. the original is archived UNDER ITS OWN FILENAME (spec 05 §3)
    ------------------------------------------------------------------
    local archive_root = sb.vault .. "/archive/capture/raw_capture"
    local archived = archive_root .. "/" .. filename
    assert.is_true(exists(archived), "expected the original archived at " .. archived)

    ------------------------------------------------------------------
    -- 3. tag added, singular type + folder name (spec 03 §6)
    ------------------------------------------------------------------
    local singular = ({
      projects = "project",
      areas = "area",
      resources = "resource",
      archives = "archive",
    })[top.type] or top.type
    local expected_tag = ("%s/%s"):format(singular, folder_name)
    assert.is_truthy(
      moved:find(expected_tag, 1, true),
      ("expected tag %q in the moved copy:\n%s"):format(expected_tag, moved)
    )

    ------------------------------------------------------------------
    -- 4. processing_status: organized ON THE MOVED COPY (03 §6 design note)
    ------------------------------------------------------------------
    assert.is_truthy(moved:find("processing_status: organized", 1, true))
    -- and the archived original is NOT the organized copy
    assert.is_falsy(read(archived):find("processing_status: organized", 1, true))

    ------------------------------------------------------------------
    -- 5. learning.json updated (spec 04 §3 — `record_move` fired)
    ------------------------------------------------------------------
    local learning_path = sb.state_dir .. "/learning.json"
    until_true(function()
      return exists(learning_path)
    end, 10000, "learning.json to be written")
    local learning = vim.json.decode(read(learning_path))
    assert.is_table(learning)
    local learning_text = read(learning_path)
    assert.is_truthy(
      learning_text:find(folder_name, 1, true),
      "learning.json should mention the destination folder:\n" .. learning_text
    )

    ------------------------------------------------------------------
    -- 6. operations.log line (spec 05 §1.5)
    ------------------------------------------------------------------
    local oplog = sb.state_dir .. "/operations.log"
    assert.is_true(exists(oplog), "expected an operations log at " .. oplog)
    local log_text = read(oplog)
    assert.is_truthy(log_text:find("move", 1, true), "no move line in operations.log:\n" .. log_text)
    assert.is_truthy(log_text:find(filename, 1, true))

    ------------------------------------------------------------------
    -- 7. ActionRecord written (spec 12 §2 — YYYY-MM.jsonl)
    ------------------------------------------------------------------
    local action_lines = read_dir_lines(sb.state_dir .. "/actions")
    assert.is_true(#action_lines > 0, "no ActionRecord was written to " .. sb.state_dir .. "/actions")
    local found_move = false
    for _, line in ipairs(action_lines) do
      local ok, decoded = pcall(vim.json.decode, line)
      if ok and type(decoded) == "table" and decoded.operation == "move" then
        found_move = true
      end
    end
    assert.is_true(found_move, "no ActionRecord with operation=move:\n" .. table.concat(action_lines, "\n"))

    ------------------------------------------------------------------
    -- 8. the next capture auto-loaded (spec 03 §6)
    ------------------------------------------------------------------
    until_true(function()
      return session.current == 2 and session.view == "suggestions"
    end, 30000, "the next capture to auto-load")
    local next_record = session.captures[2]
    assert.are_not.equal(source, next_record.path)
    assert.are.equal(1, #session.processed)
    assert.are.equal(source, session.processed[1])
    assert.are.equal(next_record.path, vim.api.nvim_buf_get_name(ui.current_bufs().capture))
  end)

  it("`:ParaOrganize stop` leaves zero orphan state (spec 09 §2)", function()
    start_session()
    local bufs = ui.current_bufs()
    local wins = ui.current_wins()

    commands.execute({ fargs = { "stop" } })

    assert.is_false(ui.is_mounted())
    assert.is_false(state.has_session())
    assert.is_nil(actions.context().state)

    -- no windows left
    assert.is_false(vim.api.nvim_win_is_valid(wins.organize))
    assert.is_false(vim.api.nvim_win_is_valid(wins.capture))

    -- no scratch buffers left
    assert.is_false(vim.api.nvim_buf_is_valid(bufs.organize))
    for _, buf in ipairs(vim.api.nvim_list_bufs()) do
      local name = vim.api.nvim_buf_get_name(buf)
      assert.is_falsy(
        name:find("para-organize://", 1, true),
        "orphan plugin buffer left behind: " .. name
      )
    end

    -- no autocommands left
    assert.has_error(function()
      vim.api.nvim_get_autocmds({ group = "ParaOrganizeUI" })
    end)

    -- The CORE keeps running: `stop` closes the SESSION, not the shared
    -- daemon (spec 03 §2 "Close UI, discard session").
    assert.is_true(core.socket_exists(sb.socket))
  end)

  it("reports 'No captures found matching filters' and opens no UI (spec 03 §2)", function()
    local seen = helpers.capture_notifications(function()
      commands.execute({ fargs = { "start", "tags=definitely-not-a-tag-in-this-vault" } })
      vim.wait(20000, function()
        return state.has_session() or ui.is_mounted()
      end, 25)
      -- give the notification a scheduled tick to land
      vim.wait(500, function()
        return false
      end, 25)
    end)

    assert.is_false(ui.is_mounted())
    assert.is_false(state.has_session())
    assert.is_true(
      (helpers.notified(seen, "No captures found matching filters")),
      "expected the spec-03 §2 message, got: " .. vim.inspect(seen)
    )
  end)

  it("archives the current capture and advances (spec 03 §6)", function()
    local session = start_session()
    local source = session.captures[session.current].path
    local filename = basename(source)
    local archived = sb.vault .. "/archive/capture/raw_capture/" .. filename

    actions.archive()

    until_true(function()
      return exists(archived)
    end, 30000, "the capture to be archived")
    assert.is_falsy(exists(source))
    -- Archive keeps the file un-organized (spec 03 §6: status unchanged).
    assert.is_falsy(read(archived):find("processing_status: organized", 1, true))
    until_true(function()
      return session.current == 2
    end, 30000, "advance after archive")
  end)

  it("`:ParaOrganize reindex` and `:ParaOrganize debug` talk to the real core", function()
    -- Both are session-free (spec 03 §2), so they must work before `start`.
    local seen = {}
    local original = vim.notify
    vim.notify = function(msg, level)
      seen[#seen + 1] = { msg = msg, level = level }
    end
    local ok, err = pcall(function()
      commands.execute({ fargs = { "reindex" } })
      until_true(function()
        return helpers.notified(seen, "reindexed")
      end, 60000, "the reindex report")
    end)
    vim.notify = original
    assert.is_true(ok, tostring(err) .. "\nnotifications: " .. vim.inspect(seen))

    -- `debug` reports the connection synchronously but streams the index
    -- stats in ASYNCHRONOUSLY (spec 09 §4: no interactive command may block
    -- the UI on a vault-sized query). The returned table is live — the async
    -- callback appends to it — so wait for the capture counts to land.
    local line_table = init.debug()
    local lines = table.concat(line_table, "\n")
    assert.is_truthy(lines:find("apiVersion 1", 1, true), lines)
    assert.is_truthy(lines:find(sb.socket, 1, true), lines)
    assert.is_truthy(lines:find("session: none", 1, true), lines)
    until_true(function()
      return table.concat(line_table, "\n"):find("captures:", 1, true) ~= nil
    end, 30000, "the async index stats in :ParaOrganize debug")
  end)

  it("writes spec-07 metadata straight through to the file on disk", function()
    local session = start_session()
    local record = session.captures[session.current]

    -- `meta.set` is the only write path a thin client has (spec 10 §1).
    local client = actions.context().client
    local result, err = client:request_sync(
      "meta.set",
      { path = record.path, changes = { importance = "high" } },
      15000
    )
    assert.is_nil(err, vim.inspect(err))
    assert.is_true(result.ok, vim.inspect(result))

    local text = read(record.path)
    assert.is_truthy(text:find("importance: high", 1, true), text)
    -- Annotating is not filing: the note stays in the capture folder with its
    -- status untouched (spec 07 acceptance test 5).
    assert.is_truthy(text:find("processing_status: raw", 1, true))
  end)
end)
