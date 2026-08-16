-- actions_spec — every row of spec 03's keymap table, dispatched against a
-- MOCK RPC client. Proves the THIN CLIENT LAW (spec 10 §1) by construction:
-- if an action needed the filesystem it could not pass here, because there
-- is no vault and no core process in this test.

local actions = require("para-organize.actions")
local ui = require("para-organize.ui")

-- --- mock core -------------------------------------------------------------

local NOT_FOUND = { code = -32601, message = "method not found", data = { kind = "MethodNotFound" } }

--- Scripted client matching the rpc seat's contract:
--- `Client:request(method, params, cb)` with `cb(err, result)`.
local function mock_client(responses)
  local client = { calls = {}, responses = responses or {} }

  function client:request(method, params, cb)
    table.insert(self.calls, { method = method, params = params })
    local entry = self.responses[method]
    if entry == nil then
      if cb then
        cb(NOT_FOUND, nil)
      end
      return
    end
    local result, err
    if type(entry) == "function" then
      result, err = entry(params)
    else
      result = entry
    end
    if cb then
      cb(err, result)
    end
  end

  function client:request_sync(method, params)
    local result
    self:request(method, params, function(_, value)
      result = value
    end)
    return result
  end

  function client:close() end

  function client:methods()
    local out = {}
    for _, call in ipairs(self.calls) do
      table.insert(out, call.method)
    end
    return out
  end

  function client:first(method)
    for _, call in ipairs(self.calls) do
      if call.method == method then
        return call
      end
    end
    return nil
  end

  function client:count(method)
    local n = 0
    for _, call in ipairs(self.calls) do
      if call.method == method then
        n = n + 1
      end
    end
    return n
  end

  return client
end

local DEFAULT_RESPONSES = {
  ["note.get"] = function(params)
    return { record = { path = params.path, title = "Capture", tags = { "alpha" } }, frontmatter = { tags = { "alpha" } }, parse_error = false }
  end,
  ["suggest.for_note"] = {
    { path = "/vault/projects/alpha", name = "alpha", type = "projects", score = 2.4, reasons = { "tag match" } },
    { path = "/vault/areas/health", name = "health", type = "areas", score = 1.1 },
  },
  ["meta.fields"] = {
    fields = {
      { key = "tags", type = "list", keymap = "t", prompt = "Add tag(s)", append = true, complete = "existing", completions = { "alpha", "beta" } },
      { key = "importance", type = "enum", keymap = "i", values = { "high", "medium", "low" }, completions = { "high", "medium", "low" } },
      { key = "remember", type = "boolean", keymap = "v" },
      { key = "energy", type = "number", keymap = "E" },
    },
  },
  ["op.move"] = function(params)
    return { ok = true, operation = "move", source = params.path, destination = params.destination }
  end,
  ["op.archive"] = function(params)
    return { ok = true, operation = "archive", source = params.path }
  end,
  ["op.merge_preview"] = function(params)
    return { content = "target body\n\n## Merged from capture on 2026-08-15 06:31\n\ncapture body", snapshot = { mtime = 42, hash = "deadbeef", path = params.target } }
  end,
  ["op.merge_commit"] = function(params)
    return { ok = true, operation = "merge", source = params.path, destination = params.target }
  end,
  ["meta.set"] = function(params)
    return { ok = true, operation = "update_frontmatter", source = params.path }
  end,
  ["folder.create"] = function(params)
    return { ok = true, operation = "new_folder", source = params.name, destination = "/vault/" .. params.para_type .. "/" .. params.name }
  end,
  ["search.query"] = {
    { path = "/vault/projects/alpha/one.md", title = "One", filename = "one.md", folder = "alpha", aliases = { "One alias" } },
    { path = "/vault/projects/beta/two.md", title = "Two", filename = "two.md", folder = "beta" },
  },
}

local function responses(overrides)
  local out = vim.deepcopy(DEFAULT_RESPONSES)
  for key, value in pairs(overrides or {}) do
    if value == false then
      out[key] = nil
    else
      out[key] = value
    end
  end
  return out
end

-- --- fake ui ---------------------------------------------------------------

local function fake_ui()
  local f = { refreshes = 0, unmounts = 0, help = nil, focused = nil, cursor_item = nil, mounted = false }
  function f.refresh()
    f.refreshes = f.refreshes + 1
  end
  function f.unmount()
    f.unmounts = f.unmounts + 1
    return true
  end
  function f.show_help(entries)
    f.help = entries
  end
  function f.focus(pane)
    f.focused = pane
  end
  function f.current_bufs()
    return {}
  end
  function f.current_wins()
    return {}
  end
  function f.item_at_cursor()
    return f.cursor_item
  end
  function f.is_mounted()
    return f.mounted
  end
  function f.merge_content(state)
    return f.merge_content_value or ((state or {}).merge or {}).content
  end
  function f.config()
    return ui.config()
  end
  return f
end

-- --- harness ---------------------------------------------------------------

describe("para-organize.actions", function()
  local client, state, fui, notices
  local real_notify, real_input, real_select

  local function setup_with(overrides, state_overrides)
    client = mock_client(responses(overrides))
    state = vim.tbl_extend("force", {
      captures = {
        { path = "/vault/capture/one.md", filename = "one.md", title = "One", tags = { "alpha" } },
        { path = "/vault/capture/two.md", filename = "two.md", title = "Two", tags = {} },
        { path = "/vault/capture/three.md", filename = "three.md", title = "Three", tags = {} },
      },
      current = 1,
      processed = {},
      skipped = {},
      selected = 1,
      sort = "intelligent",
      view = "suggestions",
      suggestions = {
        { path = "/vault/projects/alpha", name = "alpha", type = "projects", score = 2.4 },
        { path = "/vault/areas/health", name = "health", type = "areas", score = 1.1 },
      },
    }, state_overrides or {})
    fui = fake_ui()
    actions.setup({
      client = client,
      state = state,
      config = ui.setup({ ui = { close_on_complete = false } }),
      ui = fui,
    })
    return client, state
  end

  before_each(function()
    notices = {}
    real_notify, real_input, real_select = vim.notify, vim.ui.input, vim.ui.select
    vim.notify = function(msg, level)
      table.insert(notices, { msg = msg, level = level })
    end
    setup_with({})
  end)

  after_each(function()
    vim.notify, vim.ui.input, vim.ui.select = real_notify, real_input, real_select
    actions.reset()
    ui.unmount()
  end)

  local function notified(pattern)
    for _, notice in ipairs(notices) do
      if notice.msg:match(pattern) then
        return true, notice
      end
    end
    return false
  end

  -- --- session navigation --------------------------------------------------

  describe("loading a capture", function()
    it("fetches the record and the suggestions, never the file", function()
      actions.load_current()
      assert.is_truthy(client:first("note.get"))
      assert.are.equal("/vault/capture/one.md", client:first("note.get").params.path)
      assert.are.equal("/vault/capture/one.md", client:first("suggest.for_note").params.path)
      assert.are.equal("suggestions", state.view)
      assert.are.equal(2, #state.suggestions)
      assert.are.equal(1, state.selected)
    end)

    it("caches the spec-07 metadata field definitions from the core", function()
      actions.load_current()
      assert.are.equal(4, #state.meta_fields)
      assert.are.equal("tags", state.meta_fields[1].key)
      assert.are.equal(1, client:count("meta.fields"))
      actions.load_current()
      assert.are.equal(1, client:count("meta.fields"), "meta.fields is fetched once per session")
    end)
  end)

  describe("<Tab> / <S-Tab>", function()
    it("advances and reloads", function()
      actions.next_capture()
      assert.are.equal(2, state.current)
      assert.are.equal("/vault/capture/two.md", client:first("suggest.for_note").params.path)
    end)

    it("clamps at both ends with a notice (spec 03 §2)", function()
      actions.prev_capture()
      assert.are.equal(1, state.current)
      assert.is_true((notified("first capture")))
      state.current = 3
      actions.next_capture()
      assert.are.equal(3, state.current)
      assert.is_true((notified("last capture")))
    end)
  end)

  describe("s = skip", function()
    it("has no file effect, counts, advances — and emits ONE op.skip signal", function()
      -- Architect's op.skip ruling (spec 12 §2: "a skip is signal too"): the
      -- one RPC skip may fire is `op.skip`, carrying the decision context.
      state.session_id = "ses_skip"
      actions.skip()
      assert.are.same({ "op.skip" }, vim.tbl_filter(function(method)
        return method:match("^op%.") ~= nil
      end, client:methods()))
      assert.are.same({ "/vault/capture/one.md" }, state.skipped)
      assert.are.equal(2, state.current)
    end)

    it("sends NO op.skip when the session has no core-issued id", function()
      -- `session_id` is REQUIRED on the wire; without one (injected state, or
      -- an id a core restart invalidated) the request would only earn a
      -- -32602, so none is sent — and the skip itself is unaffected.
      actions.skip()
      assert.are.equal(0, client:count("op.skip"))
      assert.are.same({ "/vault/capture/one.md" }, state.skipped)
      assert.are.equal(2, state.current)
    end)

    it("sends the counterfactual with NO chosen rank", function()
      state.session_id = "ses_skip"
      state.shown_at = (vim.uv or vim.loop).now()
      actions.skip()
      local call = client:first("op.skip")
      -- The one op.* asymmetry: the capture travels as `note`, never `path`.
      assert.are.equal("/vault/capture/one.md", call.params.note)
      assert.is_nil(call.params.path)
      -- Trimmed to the wire contract: no filters key on op.skip.
      assert.is_nil(call.params.filters)
      assert.are.equal("ses_skip", call.params.session_id)
      -- Both rendered suggestions, with their on-screen ranks…
      assert.are.equal(2, #call.params.suggestions_shown)
      assert.are.equal(1, call.params.suggestions_shown[1].rank)
      assert.are.equal("/vault/projects/alpha", call.params.suggestions_shown[1].path)
      -- …the decision duration only the client can measure…
      assert.are.equal("number", type(call.params.durations_ms.decision))
      -- …and by definition nothing chosen.
      assert.is_nil(call.params.chosen_rank)
    end)

    it("still advances instantly on an older core with no op.skip", function()
      setup_with({ ["op.skip"] = false })
      state.session_id = "ses_skip"
      local seen = {}
      local original = vim.notify
      vim.notify = function(msg, level)
        seen[#seen + 1] = { msg = msg, level = level }
      end
      actions.skip()
      vim.notify = original
      assert.are.equal(2, state.current)
      assert.are.same({ "/vault/capture/one.md" }, state.skipped)
      -- MethodNotFound stays quiet: the learning record is a bonus, never a
      -- scold (spec 09 §1.5 reserves loud failure for real misconfiguration).
      for _, n in ipairs(seen) do
        assert.is_nil(n.msg:match("op%.skip"), n.msg)
      end
    end)

    it("counts into a numeric counter too", function()
      state.skipped = 0
      actions.skip()
      assert.are.equal(1, state.skipped)
    end)
  end)

  -- --- mutating actions ----------------------------------------------------

  describe("<CR> = accept", function()
    it("issues op.move to the selected suggestion and advances", function()
      state.selected = 2
      actions.accept()
      local call = client:first("op.move")
      assert.is_truthy(call)
      assert.are.equal("/vault/capture/one.md", call.params.path)
      assert.are.equal("/vault/areas/health", call.params.destination)
      assert.are.same({ "/vault/capture/one.md" }, state.processed)
      assert.are.equal(2, state.current)
    end)

    it("reports a failed operation loudly and does NOT advance", function()
      setup_with({ ["op.move"] = { ok = false, operation = "move", source = "x", error = "destination unwritable" } })
      actions.accept()
      assert.is_true((notified("destination unwritable")))
      assert.are.equal(1, state.current)
      assert.are.equal(0, #state.processed)
    end)

    it("starts a merge on an [F] line while browsing", function()
      state.view = "browse"
      state.browse = { path = "/vault/projects/alpha", stack = {}, entries = { { kind = "file", path = "/vault/projects/alpha/note.md", name = "note.md" } } }
      state.selected = 1
      actions.accept()
      assert.are.equal("/vault/projects/alpha/note.md", client:first("op.merge_preview").params.target)
    end)

    it("descends into a [D] line while browsing", function()
      state.view = "browse"
      state.browse = { path = "/vault/projects", stack = {}, entries = { { kind = "dir", path = "/vault/projects/alpha", name = "alpha" } } }
      state.selected = 1
      actions.accept()
      assert.are.equal("browse", state.view)
      assert.are.equal("/vault/projects/alpha", state.browse.path)
    end)
  end)

  describe("a = archive", function()
    it("issues op.archive for the current capture", function()
      actions.archive()
      assert.are.equal("/vault/capture/one.md", client:first("op.archive").params.path)
      assert.are.same({ "/vault/capture/one.md" }, state.processed)
      assert.are.equal(2, state.current)
    end)
  end)

  describe("m = merge (spec 03 §5)", function()
    it("previews, then commits the edited buffer with the snapshot", function()
      actions.merge_with("/vault/projects/alpha/target.md")
      local preview = client:first("op.merge_preview")
      assert.are.equal("/vault/capture/one.md", preview.params.path)
      assert.are.equal("/vault/projects/alpha/target.md", preview.params.target)
      assert.are.equal("merge", state.view)
      assert.is_truthy(state.merge.content:match("Merged from"))
      assert.are.equal("organize", fui.focused)

      fui.merge_content_value = "edited by matt"
      actions.merge_complete()
      local commit = client:first("op.merge_commit")
      assert.are.equal("/vault/projects/alpha/target.md", commit.params.target)
      assert.are.equal("edited by matt", commit.params.content)
      -- The snapshot is echoed back with every NUMBER stringified — see the
      -- precision regression below.
      assert.are.same({ mtime = "42", hash = "deadbeef", path = "/vault/projects/alpha/target.md" }, commit.params.snapshot)
      assert.are.same({ "/vault/capture/one.md" }, state.processed)
    end)

    it("round-trips the snapshot mtime WITHOUT losing precision (spec 03 §5 step 3)", function()
      -- `vim.json.encode` renders a Lua number at ~16 significant digits, so
      -- echoing the decoded snapshot back dropped the last digit of the
      -- core's mtime and `op.merge_commit` rejected EVERY merge with
      -- ConcurrentModificationError — i.e. merge could never be completed
      -- from the plugin at all.
      local exact = vim.json.decode('{"mtime":1786864908.3653965}').mtime
      assert.is_false(
        vim.json.decode(vim.json.encode({ mtime = exact })).mtime == exact,
        "precondition: encoding a Lua number is lossy; if this ever stops being true, simplify wire_snapshot"
      )

      local wire = actions.wire_snapshot({ path = "/v/t.md", mtime = exact, sha256 = "b77bb54" })
      assert.are.equal("string", type(wire.mtime))
      -- The core coerces with float(...), so what matters is that the STRING
      -- parses back to the identical double.
      assert.are.equal(exact, tonumber(wire.mtime))
      assert.are.equal(exact, tonumber(vim.json.decode(vim.json.encode(wire)).mtime))
      assert.are.equal("b77bb54", wire.sha256)
    end)

    it("sends the merge decision context, ranked against the TARGET's folder", function()
      state.session_id = "ses_merge"
      actions.merge_with("/vault/areas/health/target.md")
      fui.merge_content_value = "merged"
      actions.merge_complete()
      local commit = client:first("op.merge_commit")
      assert.are.equal("ses_merge", commit.params.session_id)
      -- areas/health is suggestion #2, and merging into a note THERE is a
      -- decision for that folder (spec 03 §6: merge learns the target folder).
      assert.are.equal(2, commit.params.chosen_rank)
      assert.are.equal(2, #commit.params.suggestions_shown)
    end)

    it("cancel restores the previous view and writes nothing", function()
      actions.merge_with("/vault/projects/alpha/target.md")
      actions.merge_cancel()
      assert.are.equal("suggestions", state.view)
      assert.is_nil(state.merge)
      assert.are.equal(0, client:count("op.merge_commit"))
    end)

    it("drives the pickers module when it exposes the spec 03 §4 pickers", function()
      local real = package.loaded["para-organize.pickers"]
      local seen = {}
      package.loaded["para-organize.pickers"] = {
        open_folder_picker = function(on_select)
          seen.folder = true
          on_select({ path = "/vault/areas/health" })
        end,
        open_folder_notes_picker = function(folder, on_select)
          seen.folder_arg = folder
          on_select({ path = folder .. "/note.md" })
        end,
      }
      actions.merge()
      package.loaded["para-organize.pickers"] = real
      assert.is_true(seen.folder)
      assert.are.equal("/vault/areas/health", seen.folder_arg)
      assert.are.equal("/vault/areas/health/note.md", client:first("op.merge_preview").params.target)
    end)

    it("falls back to a prompt when the pickers module is absent", function()
      local real = package.loaded["para-organize.pickers"]
      package.loaded["para-organize.pickers"] = { _stub_without_pickers = true }
      vim.ui.input = function(_, cb)
        cb("/vault/areas/health/note.md")
      end
      actions.merge()
      package.loaded["para-organize.pickers"] = real
      assert.are.equal("/vault/areas/health/note.md", client:first("op.merge_preview").params.target)
    end)
  end)

  -- --- metadata editing (spec 07) ------------------------------------------

  describe("metadata editing", function()
    it("t: splits and trims a comma list, then issues meta.set", function()
      local prompt_opts
      vim.ui.input = function(opts, cb)
        prompt_opts = opts
        cb("foo, Bar Baz ")
      end
      actions.load_current()
      actions.set_meta_field("tags")
      local call = client:first("meta.set")
      assert.is_truthy(call)
      assert.are.equal("/vault/capture/one.md", call.params.path)
      assert.are.same({ "foo", "Bar Baz" }, call.params.changes.tags)
      assert.is_truthy(prompt_opts.prompt:match("Add tag"))
      -- Completion is wired from meta.fields (spec 07 complete="existing").
      assert.are.equal("customlist,v:lua.__para_organize_meta_complete", prompt_opts.completion)
    end)

    it("t: offers the core's completion values", function()
      vim.ui.input = function(opts, cb)
        actions._completions = actions._completions
        assert.are.same({ "alpha", "beta" }, actions._completions)
        assert.are.same({ "alpha" }, _G.__para_organize_meta_complete("al"))
        cb(nil)
      end
      actions.load_current()
      actions.set_meta_field("tags")
    end)

    it("i: selects one enum value", function()
      vim.ui.select = function(items, _, cb)
        assert.are.same({ "high", "medium", "low" }, items)
        cb("high")
      end
      actions.load_current()
      actions.set_meta_field("importance")
      assert.are.same({ importance = "high" }, client:first("meta.set").params.changes)
    end)

    it("v: toggles a boolean off the current frontmatter value", function()
      actions.load_current()
      state.captures[1].frontmatter = { remember = false }
      actions.set_meta_field("remember")
      assert.are.same({ remember = true }, client:first("meta.set").params.changes)
    end)

    it("E: rejects a non-numeric value for a number field", function()
      vim.ui.input = function(_, cb)
        cb("not-a-number")
      end
      actions.load_current()
      actions.set_meta_field("energy")
      assert.is_nil(client:first("meta.set"))
      assert.is_true((notified("must be a number")))
    end)

    it("re-reads the note through the core after writing", function()
      vim.ui.select = function(_, _, cb)
        cb("low")
      end
      actions.load_current()
      local before = client:count("note.get")
      actions.set_meta_field("importance")
      assert.is_true(client:count("note.get") > before)
    end)

    it("a novel configured field works with no code change (spec 07 test 3)", function()
      setup_with({
        ["meta.fields"] = { fields = { { key = "energy", type = "number", keymap = "E" } } },
      })
      vim.ui.input = function(_, cb)
        cb("7")
      end
      actions.load_current()
      actions.set_meta_field("energy")
      assert.are.same({ energy = 7 }, client:first("meta.set").params.changes)
      local lhs = {}
      for _, entry in ipairs(actions.keymap_table()) do
        lhs[entry.lhs] = entry.name
      end
      assert.are.equal("meta:energy", lhs["E"])
    end)
  end)

  -- --- browse / search / sort ---------------------------------------------

  describe("browse, search and sort", function()
    it("S cycles the three sort modes", function()
      state.sort = "alphabetical"
      actions.sort_cycle()
      assert.are.equal("modified", state.sort)
      actions.sort_cycle()
      assert.are.equal("intelligent", state.sort)
      actions.sort_cycle()
      assert.are.equal("alphabetical", state.sort)
      assert.is_true((notified("sort: Alphabetical")))
    end)

    it("sorts dirs first, case-insensitively, in alphabetical mode", function()
      state.sort = "alphabetical"
      state.browse = {
        path = "/vault/projects",
        stack = {},
        entries = {
          { kind = "file", path = "/vault/projects/a.md", display = "Apple" },
          { kind = "dir", path = "/vault/projects/zeta", display = "zeta" },
          { kind = "dir", path = "/vault/projects/Beta", display = "Beta" },
        },
      }
      actions.apply_sort()
      local order = vim.tbl_map(function(entry)
        return entry.display
      end, state.browse.entries)
      assert.are.same({ "Beta", "zeta", "Apple" }, order)
    end)

    it("browses via folder.children, the method that takes a path", function()
      -- NOT `folder.list`: that one ignores `path` and answers with every PARA
      -- subfolder in the vault under a `folders` key, so browsing rendered
      -- "(empty folder)" for every directory. `folder.children` is documented
      -- in server.py as "one directory level for 03 §3 browsing".
      setup_with({
        ["folder.children"] = {
          dirs = { { path = "/vault/projects/alpha", name = "alpha", description = "the alpha project" } },
          notes = {
            { path = "/vault/projects/x.md", title = "Ex", aliases = { "The X note" } },
            { path = "/vault/projects/y.md", title = "Why" },
          },
        },
      })
      actions.open_item({ kind = "dir", path = "/vault/projects" })
      assert.are.equal("browse", state.view)
      assert.are.equal("/vault/projects", client:first("folder.children").params.path)
      assert.are.equal(0, client:count("folder.list"))
      assert.are.equal(0, client:count("search.query"))

      local by_display = {}
      for _, entry in ipairs(state.browse.entries) do
        by_display[entry.display] = entry
      end
      -- dirs first, then notes; `[F]` labels are aliases[1] → title → filename
      -- (spec 03 §3 state 2).
      assert.are.equal("dir", by_display["alpha"].kind)
      assert.are.equal("the alpha project", by_display["alpha"].description)
      assert.are.equal("file", by_display["The X note"].kind)
      assert.are.equal("file", by_display["Why"].kind)
      assert.are.equal(3, #state.browse.entries)
    end)

    it("degrades to deriving children from indexed records", function()
      -- folder.children is absent from the mock: no crash, no filesystem read.
      actions.open_item({ kind = "dir", path = "/vault/projects" })
      assert.are.equal("browse", state.view)
      local kinds = {}
      for _, entry in ipairs(state.browse.entries) do
        kinds[entry.display or entry.name] = entry.kind
      end
      assert.are.equal("dir", kinds["alpha"])
      assert.are.equal("dir", kinds["beta"])
      assert.is_true(client:count("search.query") > 0)
    end)

    it("<BS> returns to the parent, then to the suggestions list", function()
      setup_with({ ["folder.children"] = { dirs = {}, notes = {} } })
      actions.open_item({ kind = "dir", path = "/vault/projects" })
      actions.open_item({ kind = "dir", path = "/vault/projects/alpha" })
      assert.are.equal("/vault/projects/alpha", state.browse.path)
      actions.back_to_parent()
      assert.are.equal("/vault/projects", state.browse.path)
      actions.back_to_parent()
      assert.are.equal("suggestions", state.view)
    end)

    it("/ searches the index and scopes to the browsed folder", function()
      vim.ui.input = function(_, cb)
        cb("note")
      end
      state.browse = { path = "/vault/projects/alpha", stack = {} }
      actions.search()
      local call = client:first("search.query")
      assert.are.same({ text = "note" }, call.params.criteria)
      assert.are.equal("search", state.view)
      assert.are.equal(1, #state.search.results, "results outside the browsed folder are filtered out")
      assert.are.equal("/vault/projects/alpha/one.md", state.search.results[1].path)
    end)

    it("r regenerates suggestions for the current capture", function()
      actions.refresh_suggestions()
      assert.are.equal("/vault/capture/one.md", client:first("suggest.for_note").params.path)
      assert.are.equal("suggestions", state.view)
    end)

    it("p toggles the preview flag and re-renders", function()
      local before = fui.refreshes
      actions.toggle_preview()
      assert.is_true(state.preview)
      assert.is_true(fui.refreshes > before)
      actions.toggle_preview()
      assert.is_false(state.preview)
    end)

    it("<A-j>/<A-k> move the selection and clamp", function()
      actions.next_suggestion()
      assert.are.equal(2, state.selected)
      actions.next_suggestion()
      assert.are.equal(2, state.selected)
      actions.prev_suggestion()
      assert.are.equal(1, state.selected)
      actions.prev_suggestion()
      assert.are.equal(1, state.selected)
    end)
  end)

  -- --- folders, help, quit -------------------------------------------------

  describe("folder creation", function()
    it("<leader>np issues folder.create under projects", function()
      vim.ui.input = function(_, cb)
        cb("new-thing")
      end
      actions.new_project()
      local call = client:first("folder.create")
      assert.are.same({ para_type = "projects", name = "new-thing" }, call.params)
      assert.are.equal(0, client:count("op.move"))
    end)

    it("moves the capture there when ui.auto_move_to_new_folder is on", function()
      actions.setup({ config = ui.setup({ ui = { close_on_complete = false, auto_move_to_new_folder = true } }) })
      actions.new_area("health")
      assert.are.equal("/vault/areas/health", client:first("op.move").params.destination)
    end)

    it("<leader>nr targets resources", function()
      actions.new_resource("papers")
      assert.are.equal("resources", client:first("folder.create").params.para_type)
    end)
  end)

  describe("help and teardown", function()
    it("? renders help from the live keymap table", function()
      actions.load_current()
      actions.help()
      assert.is_truthy(fui.help)
      local by_name = {}
      for _, entry in ipairs(fui.help) do
        by_name[entry.name] = entry
      end
      assert.are.equal("s", by_name.skip.lhs)
      assert.are.equal("S", by_name.sort_cycle.lhs)
      assert.are.equal("t", by_name["meta:tags"].lhs)
    end)

    it("<Esc> unmounts the UI", function()
      actions.quit()
      assert.are.equal(1, fui.unmounts)
    end)
  end)

  -- --- the keymap table ----------------------------------------------------

  describe("keymap table (spec 03 §3)", function()
    it("implements every documented row, with s=skip and S=sort-cycle", function()
      actions.load_current()
      local by_lhs, by_name = {}, {}
      for _, entry in ipairs(actions.keymap_table()) do
        by_lhs[entry.lhs] = entry.name
        by_name[entry.name] = entry
      end
      local expected = {
        ["<CR>"] = "accept",
        ["<Esc>"] = "cancel",
        ["<Tab>"] = "next",
        ["<S-Tab>"] = "prev",
        ["s"] = "skip",
        ["S"] = "sort_cycle",
        ["a"] = "archive",
        ["m"] = "merge",
        ["/"] = "search",
        ["r"] = "refresh",
        ["p"] = "toggle_preview",
        ["?"] = "help",
        ["<A-j>"] = "next_suggestion",
        ["<A-k>"] = "prev_suggestion",
        ["<C-h>"] = "focus_capture",
        ["<C-l>"] = "focus_organize",
        ["<BS>"] = "back",
        ["<leader>np"] = "new_project",
        ["<leader>na"] = "new_area",
        ["<leader>nr"] = "new_resource",
        ["<leader>mc"] = "merge_complete",
        ["<leader>mx"] = "merge_cancel",
        ["t"] = "meta:tags",
        ["i"] = "meta:importance",
        ["v"] = "meta:remember",
        ["E"] = "meta:energy",
      }
      for lhs, name in pairs(expected) do
        assert.are.equal(name, by_lhs[lhs], ("keymap %s should run %s"):format(lhs, name))
      end
      -- Every entry actually dispatches something.
      for name, entry in pairs(by_name) do
        assert.are.equal("function", type(entry.fn), name .. " has no handler")
      end
    end)

    it("respects rebound keys from config", function()
      actions.setup({ config = ui.setup({ ui = { close_on_complete = false }, keymaps = { buffer = { skip = "x", archive = "" } } }) })
      local by_name = {}
      for _, entry in ipairs(actions.keymap_table()) do
        by_name[entry.name] = entry.lhs
      end
      assert.are.equal("x", by_name.skip)
      assert.is_nil(by_name.archive, "an empty binding is dropped, not bound to ''")
    end)

    it("reports a metadata/core keymap collision (spec 07 test 4)", function()
      state.meta_fields = { { key = "importance", type = "enum", keymap = "a", values = { "high" } } }
      local conflicts = actions.detect_collisions()
      assert.is_true(#conflicts > 0)
      assert.are.equal("a", conflicts[1].lhs)
      assert.are.equal("archive", conflicts[1].first)
      assert.are.equal("meta:importance", conflicts[1].second)
    end)

    it("binds buffer-locally into the real UI buffers on mount", function()
      actions.setup({ ui = ui, config = ui.setup({ ui = { close_on_complete = false } }) })
      state.meta_fields = DEFAULT_RESPONSES["meta.fields"].fields
      ui.mount(state)
      local bufs = ui.current_bufs()
      local function descs(buf)
        local out = {}
        for _, map in ipairs(vim.api.nvim_buf_get_keymap(buf, "n")) do
          if map.desc and map.desc:match("^para%-organize: ") then
            out[map.desc] = true
          end
        end
        return out
      end
      local organize = descs(bufs.organize)
      assert.is_true(organize["para-organize: Skip capture"])
      assert.is_true(organize["para-organize: Cycle sort mode"])
      assert.is_true(organize["para-organize: Back to parent folder"])
      assert.is_true(organize["para-organize: Set tags (list)"])

      local capture = descs(bufs.capture)
      assert.is_true(capture["para-organize: Skip capture"], "spec 03 keeps s=skip globally")
      assert.is_nil(capture["para-organize: Set tags (list)"], "spec 07 scopes metadata keys to the organize pane")
      ui.unmount()
    end)

    it("capture_pane_keymaps='navigation' leaves the capture buffer editable", function()
      actions.setup({ ui = ui, config = ui.setup({ ui = { close_on_complete = false, capture_pane_keymaps = "navigation" } }) })
      ui.mount(state)
      local out = {}
      for _, map in ipairs(vim.api.nvim_buf_get_keymap(ui.current_bufs().capture, "n")) do
        out[map.lhs] = true
      end
      assert.is_nil(out["s"])
      assert.is_nil(out["a"])
      assert.is_true(out["?"] or false)
      ui.unmount()
    end)
  end)

  -- --- graceful degradation (spec 10 §1) -----------------------------------

  describe("graceful degradation", function()
    it("notifies once and does not crash when there is no core", function()
      actions.setup({ client = false and {} or nil })
      actions.set_client(nil)
      assert.has_no.errors(function()
        actions.accept()
      end)
      assert.is_true((notified("core is not running")))
    end)

    it("surfaces the core's error kind and hint", function()
      setup_with({
        ["op.archive"] = function()
          return nil, { code = -32000, message = "cannot read note", data = { kind = "VaultError", hint = "check the path" } }
        end,
      })
      assert.has_no.errors(function()
        actions.archive()
      end)
      assert.is_true((notified("cannot read note")))
      assert.is_true((notified("VaultError")))
      assert.is_true((notified("check the path")))
      assert.are.equal(1, state.current, "a failed op never advances the session")
    end)

    it("does nothing dangerous with no capture loaded", function()
      setup_with({}, { captures = {}, current = 1 })
      assert.has_no.errors(function()
        actions.accept()
        actions.archive()
        actions.skip()
        actions.merge()
        actions.set_meta_field("tags")
      end)
      assert.are.equal(0, client:count("op.move"))
      assert.are.equal(0, client:count("op.archive"))
      assert.are.equal(0, client:count("meta.set"))
    end)
  end)
end)
