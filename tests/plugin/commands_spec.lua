--- `:ParaOrganize` — argument parsing, completion and dispatch (spec 03 §2).
---
--- No core, no vault: this suite proves the command surface routes correctly
--- and fails clearly. Dispatch targets are injected mocks, so a real
--- `actions.lua` is not needed and the assertions are exact.

local commands = require("para-organize.commands")
local support = require("phc_support")

local REPO = support.repo

--- An `actions` stub that records every call.
local function recording_actions(names)
  local calls = {}
  local actions = { _calls = calls }
  for _, name in ipairs(names) do
    actions[name] = function(arg)
      calls[#calls + 1] = { name = name, arg = arg }
      return true
    end
  end
  return actions
end

local function all_action_names()
  return commands.action_names()
end

describe("commands: parsing", function()
  after_each(function()
    commands.reset_injections()
  end)

  it("defaults to `start` when no subcommand is given (03 §2)", function()
    local parsed, err = commands.parse({})
    assert.is_nil(err)
    assert.equals("start", parsed.subcommand)
    assert.equals("start", parsed.action)
    assert.same({}, parsed.filters)
    assert.is_false(parsed.requires_session)
  end)

  it("parses comma lists into lists and scalars into strings", function()
    local parsed = commands.parse({ "start", "tags=impro,creativity", "sources=me", "since=2026-06-01" })
    assert.same({ "impro", "creativity" }, parsed.filters.tags)
    assert.same({ "me" }, parsed.filters.sources)
    assert.equals("2026-06-01", parsed.filters.since)
  end)

  it("trims filter values and drops empty list members", function()
    local parsed = commands.parse({ "start", "tags= a , ,b " })
    assert.same({ "a", "b" }, parsed.filters.tags)
  end)

  it("treats a leading k=v as an implicit `start` (no subcommand ⇒ start)", function()
    local parsed, err = commands.parse({ "tags=health" })
    assert.is_nil(err)
    assert.equals("start", parsed.subcommand)
    assert.same({ "health" }, parsed.filters.tags)
  end)

  it("rejects a `start` argument that is not a k=v pair, naming it", function()
    local parsed, err = commands.parse({ "start", "notafilter" })
    assert.is_nil(parsed)
    assert.is_truthy(err:find("notafilter", 1, true))
    assert.is_truthy(err:find("tags", 1, true))
  end)

  it("rejects an unknown subcommand and lists the valid ones", function()
    local parsed, err = commands.parse({ "frobnicate" })
    assert.is_nil(parsed)
    assert.is_truthy(err:find("frobnicate", 1, true))
    assert.is_truthy(err:find("new-resource", 1, true))
  end)

  it("accepts `previous` as an alias for `prev`", function()
    local parsed = commands.parse({ "previous" })
    assert.equals("prev", parsed.subcommand)
    assert.equals("prev", parsed.action)
  end)

  it("joins the remaining argv for move / search / new-*", function()
    assert.equals("projects/my blog", commands.parse({ "move", "projects/my", "blog" }).path)
    assert.equals("improv warmups", commands.parse({ "search", "improv", "warmups" }).query)
    assert.equals("My Great Project", commands.parse({ "new-project", "My", "Great", "Project" }).name)
  end)

  it("leaves an omitted move destination / folder name nil so the action can prompt", function()
    assert.is_nil(commands.parse({ "move" }).path)
    assert.is_nil(commands.parse({ "new-area" }).name)
    assert.equals("", commands.parse({ "search" }).query)
  end)

  it("marks exactly the session-scoped subcommands as requiring a session", function()
    local needs = {}
    for _, name in ipairs(commands.subcommand_names()) do
      if commands.parse({ name }).requires_session then
        needs[#needs + 1] = name
      end
    end
    table.sort(needs)
    assert.same({ "archive", "merge", "move", "next", "prev", "skip", "stop" }, needs)
  end)

  it("covers every subcommand spec 03 §2 lists", function()
    local names = commands.subcommand_names()
    for _, expected in ipairs({
      "start", "stop", "next", "prev", "skip", "move", "merge", "archive",
      "reindex", "search", "debug", "help", "new-project", "new-area", "new-resource",
    }) do
      assert.is_truthy(vim.tbl_contains(names, expected), "missing subcommand: " .. expected)
    end
    assert.equals(15, #names)
  end)
end)

describe("commands: completion", function()
  it("completes subcommand names in the first position", function()
    local all = commands.complete("", "ParaOrganize ", 13)
    assert.is_truthy(vim.tbl_contains(all, "start"))
    assert.is_truthy(vim.tbl_contains(all, "new-resource"))
    assert.equals(#commands.subcommand_names(), #all)
  end)

  it("filters subcommand candidates by the arglead", function()
    local out = commands.complete("s", "ParaOrganize s", 14)
    table.sort(out)
    assert.same({ "search", "skip", "start", "stop" }, out)

    assert.same({ "new-area", "new-project", "new-resource" }, (function()
      local n = commands.complete("new-", "ParaOrganize new-", 17)
      table.sort(n)
      return n
    end)())
  end)

  it("completes filter keys inside `start`", function()
    local out = commands.complete("", "ParaOrganize start ", 19)
    assert.is_truthy(vim.tbl_contains(out, "tags="))
    assert.is_truthy(vim.tbl_contains(out, "sources="))
    for _, candidate in ipairs(out) do
      assert.is_truthy(candidate:sub(-1) == "=", "filter candidate must end in '=': " .. candidate)
    end
  end)

  it("completes filter keys for an implicit start too", function()
    local out = commands.complete("ta", "ParaOrganize tags=health ta", 27)
    assert.is_truthy(vim.tbl_contains(out, "tags="))
  end)

  it("completes known values after `key=`", function()
    local out = commands.complete("status=", "ParaOrganize start status=", 26)
    assert.is_truthy(vim.tbl_contains(out, "status=raw"))
  end)

  it("offers nothing for subcommands that take free text", function()
    assert.same({}, commands.complete("", "ParaOrganize move ", 18))
    assert.same({}, commands.complete("", "ParaOrganize search ", 20))
  end)
end)

describe("commands: dispatch", function()
  after_each(function()
    commands.reset_injections()
  end)

  it("calls the matching function on para-organize.actions", function()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    commands.inject("state", { has_session = function() return true end })

    commands.execute({ fargs = { "start", "tags=impro" } })
    commands.execute({ fargs = { "skip" } })
    commands.execute({ fargs = { "new-area", "Health" } })
    commands.execute({ fargs = { "search", "improv warmups" } })
    commands.execute({ fargs = { "move", "projects/blog" } })

    assert.equals(5, #actions._calls)
    assert.equals("start", actions._calls[1].name)
    assert.same({ "impro" }, actions._calls[1].arg.tags)
    assert.equals("skip", actions._calls[2].name)
    assert.equals("new_area", actions._calls[3].name)
    assert.equals("Health", actions._calls[3].arg)
    -- `search_picker`, not `search`: spec 03 §2's subcommand opens the
    -- Telescope picker over the index, while `actions.search` is the `/`
    -- INLINE prompt of §3 state 3 — it takes no argument, so routing the
    -- subcommand there discarded the query and did nothing at all.
    assert.equals("search_picker", actions._calls[4].name)
    assert.equals("improv warmups", actions._calls[4].arg)
    assert.equals("move", actions._calls[5].name)
    assert.equals("projects/blog", actions._calls[5].arg)
  end)

  it("maps every subcommand to an action the actions module actually exposes", function()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    commands.inject("state", { has_session = function() return true end })
    commands.inject("pickers", { open_folder_picker = function() end })

    for _, name in ipairs(commands.subcommand_names()) do
      local parsed = commands.parse({ name })
      local seen = support.capture_notifications(function()
        assert.is_true(commands.dispatch(parsed) == true, "dispatch failed for " .. name)
      end)
      assert.equals(0, #seen, "dispatching " .. name .. " notified: " .. vim.inspect(seen))
    end
  end)

  it("refuses a session command with no session, and does not call the action", function()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    commands.inject("state", { has_session = function() return false end })

    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "skip" } }))
    end)
    assert.equals(0, #actions._calls)
    assert.is_true(support.notified(seen, "no active session"))
    assert.equals(vim.log.levels.WARN, seen[1].level)
  end)

  it("still runs sessionless commands with no session", function()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    commands.inject("state", { has_session = function() return false end })

    local seen = support.capture_notifications(function()
      assert.is_true(commands.execute({ fargs = { "reindex" } }))
    end)
    assert.equals(0, #seen)
    assert.equals("reindex", actions._calls[1].name)
  end)

  it("does not block when the state module cannot answer", function()
    commands.inject("actions", recording_actions(all_action_names()))
    commands.inject("state", {})
    assert.is_true(commands.execute({ fargs = { "skip" } }))
  end)

  it("notifies once — no stack trace — when the actions module is missing", function()
    commands.inject("actions", false)
    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "reindex" } }))
    end)
    assert.equals(1, #seen)
    assert.is_true(support.notified(seen, "para-organize.actions is unavailable"))
    assert.is_falsy(seen[1].msg:find("stack traceback", 1, true))
  end)

  it("notifies when an action is declared but not implemented", function()
    commands.inject("actions", {})
    commands.inject("state", { has_session = function() return true end })
    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "archive" } }))
    end)
    assert.is_true(support.notified(seen, "not implemented"))
    assert.is_true(support.notified(seen, "actions.archive"))
  end)

  it("turns an error thrown by an action into one notification", function()
    commands.inject("actions", {
      reindex = function()
        error("core unreachable")
      end,
    })
    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "reindex" } }))
    end)
    assert.equals(1, #seen)
    assert.is_true(support.notified(seen, "core unreachable"))
  end)

  it("reports a parse error instead of dispatching", function()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "nope" } }))
    end)
    assert.equals(0, #actions._calls)
    assert.is_true(support.notified(seen, "unknown subcommand"))
  end)

  it("opens the destination picker when `move` has no argument (03 §4)", function()
    local chosen = {}
    commands.inject("actions", {
      move = function(dest)
        chosen[#chosen + 1] = dest
      end,
    })
    commands.inject("state", { has_session = function() return true end })
    commands.inject("pickers", {
      open_folder_picker = function(on_select)
        on_select({ path = "/vault/projects/blog" })
      end,
    })
    assert.is_true(commands.execute({ fargs = { "move" } }))
    assert.same({ "/vault/projects/blog" }, chosen)
  end)

  it("says so when `move` has neither an argument nor a picker", function()
    commands.inject("actions", { move = function() end })
    commands.inject("state", { has_session = function() return true end })
    commands.inject("pickers", false)
    local seen = support.capture_notifications(function()
      assert.is_false(commands.execute({ fargs = { "move" } }))
    end)
    assert.is_true(support.notified(seen, "move needs a destination"))
  end)
end)

describe("commands: registration (plugin/para-organize.lua)", function()
  local sourced = false

  local function source_plugin()
    if sourced then
      return
    end
    vim.g.loaded_para_organize = nil
    vim.cmd("source " .. vim.fn.fnameescape(REPO .. "/plugin/para-organize.lua"))
    sourced = true
  end

  it("registers :ParaOrganize with completion and :ParaOrganizeHealth", function()
    source_plugin()
    local cmds = vim.api.nvim_get_commands({})
    assert.is_truthy(cmds.ParaOrganize, ":ParaOrganize was not created")
    assert.equals("*", cmds.ParaOrganize.nargs)
    assert.is_truthy(cmds.ParaOrganizeHealth, ":ParaOrganizeHealth was not created")
  end)

  it("registers the 13 <Plug> mappings of spec 03 §2 and no global keymaps", function()
    source_plugin()
    local found = {}
    for _, map in ipairs(vim.api.nvim_get_keymap("n")) do
      if map.lhs:find("ParaOrganize", 1, true) then
        found[#found + 1] = map.lhs
        assert.is_truthy(map.lhs:find("<Plug>", 1, true), "non-<Plug> global keymap: " .. map.lhs)
      end
    end
    assert.equals(13, #found, "expected 13 <Plug> mappings, got " .. vim.inspect(found))
    for _, name in ipairs({
      "Start", "Stop", "Reindex", "Search", "Accept", "Merge", "Archive",
      "Next", "Prev", "Skip", "NewProject", "NewArea", "NewResource",
    }) do
      local want = "ParaOrganize" .. name
      local hit = false
      for _, lhs in ipairs(found) do
        if lhs:find(want .. ")", 1, true) or lhs:sub(-#want) == want then
          hit = true
        end
      end
      assert.is_truthy(hit, "missing <Plug> mapping for " .. want)
    end
  end)

  it("is idempotent — a second source does not double-register", function()
    source_plugin()
    vim.cmd("source " .. vim.fn.fnameescape(REPO .. "/plugin/para-organize.lua"))
    local n = 0
    for _, map in ipairs(vim.api.nvim_get_keymap("n")) do
      if map.lhs:find("ParaOrganize", 1, true) then
        n = n + 1
      end
    end
    assert.equals(13, n)
  end)

  it("dispatches a <Plug> press through the same action table", function()
    source_plugin()
    local actions = recording_actions(all_action_names())
    commands.inject("actions", actions)
    commands.inject("state", { has_session = function() return true end })
    -- The <Plug> rhs is a lua callback, so it is invoked directly: feeding the
    -- keys through `:normal` needs a real screen and is not headless-safe
    -- (`vim.cmd("normal \n")` raised E471: Argument required).
    local rhs
    for _, map in ipairs(vim.api.nvim_get_keymap("n")) do
      if map.lhs:find("ParaOrganizeArchive", 1, true) then
        rhs = map.callback
      end
    end
    assert.is_truthy(rhs, "<Plug> mapping has no lua callback")
    rhs()
    commands.reset_injections()
    assert.equals("archive", actions._calls[1].name)
  end)
end)
