-- integrate_spec — spec 12 §1's edit modes and review gate, from the nvim
-- client's side.
--
-- Two halves, deliberately:
--
--   1. MODE RESOLUTION + THE GATE, against a scripted mock core. Proves the
--      THIN CLIENT LAW by construction (spec 10 §1): there is no vault and no
--      core process in these tests, so an implementation that applied a diff
--      itself could not pass them.
--   2. THE REAL THING (spec 09 §3's bar): a real `organize serve` spawned by
--      the plugin against a real fixture vault and a fake `claude` script, so
--      config → get_client → propose → the LLM subprocess → the deletion
--      guard → the atomic write are all under test. Every post-commit
--      assertion is made against BYTES ON DISK, never against an RPC reply.
--
-- SAFETY (spec 09 §1.4, CLAUDE.md): the vault, the core's config/state/
-- runtime dirs, its socket and the fake model all live inside one throwaway
-- sandbox that `after_each` deletes pass or fail. Nothing here can see
-- ~/Obsidian/Main or ~/.local/share/organize-core.

local helpers = require("helpers")

local actions = require("para-organize.actions")
local integrate = require("para-organize.integrate")
local ui = require("para-organize.ui")

local uv = vim.uv or vim.loop

---------------------------------------------------------------------------
-- part 1 — mode resolution and the review gate, against a mock core
---------------------------------------------------------------------------

local NOT_FOUND = { code = -32601, message = "method not found", data = { kind = "MethodNotFound" } }

--- The exact double the core's `st_mtime` is: 17 significant digits do NOT
--- survive `vim.json.encode`, which is the whole reason `wire_snapshot`
--- exists (see the merge regression it was written for).
local EXACT_MTIME = vim.json.decode('{"mtime":1786864908.3653965}').mtime

local DIFF = table.concat({
  "--- /vault/resources/performing/impro.md",
  "+++ /vault/resources/performing/impro.md",
  "@@ -1,5 +1,8 @@",
  " ---",
  " tags:",
  " - impro",
  " ---",
  " improv resources",
  "+",
  "+## Ideas",
  "+",
  "+An idea about improv warmups.",
  "",
}, "\n")

local function proposal_payload(overrides)
  return vim.tbl_extend("force", {
    proposal_id = "prop_01J0TESTTESTTESTTESTTESTTE",
    capture_path = "/vault/capture/one.md",
    target_path = "/vault/areas/health/target.md",
    diff = DIFF,
    rationale = "Woven under a new Ideas heading.",
    llm = {
      backend = "claude-cli",
      model = "fake-sonnet",
      prompt_hash = "abc123",
      proposed_diff = DIFF,
    },
    target_snapshot = {
      path = "/vault/areas/health/target.md",
      mtime = EXACT_MTIME,
      sha256 = "b77bb54deadbeef",
    },
    route = vim.NIL,
    description = vim.NIL,
    summarize = false,
    review = "diff",
  }, overrides or {})
end

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

  function client:close() end

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

--- The `actions`-shaped fake UI, plus the one method the review gate needs:
--- `organize_content()`, which is how a hand-edited diff comes back.
local function fake_ui()
  local f = { refreshes = 0, focused = nil, cursor_item = nil, content = nil, mounted = false }
  function f.refresh()
    f.refreshes = f.refreshes + 1
  end
  function f.unmount()
    return true
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
  function f.organize_content()
    return f.content
  end
  function f.merge_content(state)
    return ((state or {}).merge or {}).content
  end
  function f.config()
    return ui.config()
  end
  return f
end

describe("para-organize.integrate (spec 12 §1)", function()
  local client, state, fui, notices
  local real_notify, real_select

  local function setup_with(overrides, state_overrides)
    local base = {
      ["note.get"] = function(params)
        return { record = { path = params.path }, frontmatter = {} }
      end,
      ["suggest.for_note"] = {
        { path = "/vault/projects/alpha", name = "alpha", type = "projects", score = 2.4 },
        { path = "/vault/areas/health", name = "health", type = "areas", score = 1.1 },
      },
      ["op.merge_preview"] = function(params)
        return { content = "target body", snapshot = { mtime = 42, sha256 = "x", path = params.target } }
      end,
      ["op.integrate_propose"] = function()
        return proposal_payload()
      end,
      ["op.integrate_commit"] = function(params)
        return {
          ok = true,
          operation = "integrate",
          source = "/vault/capture/one.md",
          destination = params.proposal and params.proposal.target_path,
          dry_run = params.dry_run == true,
          details = { verdict = params.verdict, written = params.verdict ~= "rejected" },
        }
      end,
    }
    for key, value in pairs(overrides or {}) do
      if value == false then
        base[key] = nil
      else
        base[key] = value
      end
    end
    client = mock_client(base)
    state = vim.tbl_extend("force", {
      session_id = "ses_integrate",
      captures = {
        { path = "/vault/capture/one.md", filename = "one.md", tags = { "impro", "creativity" } },
        { path = "/vault/capture/two.md", filename = "two.md", tags = {} },
      },
      current = 1,
      processed = {},
      skipped = {},
      selected = 2,
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

  --- Install a route table as if `routes.resolve` had answered with it.
  local function with_routes(matches, default_mode)
    state.routes = integrate.normalize_routes({ default_mode = default_mode, matches = matches })
    state.routes.path = state.captures[state.current].path
    return state.routes
  end

  before_each(function()
    notices = {}
    real_notify, real_select = vim.notify, vim.ui.select
    vim.notify = function(msg, level)
      table.insert(notices, { msg = msg, level = level })
    end
    setup_with({})
  end)

  after_each(function()
    vim.notify, vim.ui.select = real_notify, real_select
    actions.reset()
    ui.unmount()
  end)

  local function notified(pattern)
    for _, notice in ipairs(notices) do
      if type(notice.msg) == "string" and notice.msg:match(pattern) then
        return true, notice
      end
    end
    return false
  end

  -- --- mode resolution -----------------------------------------------------

  describe("edit-mode resolution", function()
    it("reads BOTH the RPC's bare array and the CLI's object shape", function()
      local from_array = integrate.normalize_routes({
        { route_name = "workout", destination = "/v/areas/fitness/log.md", mode = "append" },
      })
      assert.is_nil(from_array.default_mode)
      assert.are.equal("append", from_array.matches[1].mode)
      assert.are.equal("workout", from_array.matches[1].route_name)

      -- The shape `organize routes resolve --json` already speaks, and the one
      -- the RPC reply is asked to grow (see the seam note in integrate.lua):
      -- it is the only shape that can answer "what mode does a capture that
      -- matched NOTHING get?".
      local from_object = integrate.normalize_routes({
        default_mode = "integrate",
        matches = {
          { route_name = "impro", destination = "/v/resources/performing/impro.md", mode = "integrate", review = "auto" },
        },
      })
      assert.are.equal("integrate", from_object.default_mode)
      assert.are.equal("auto", from_object.matches[1].review)
    end)

    it("prefers the route, then the core default, then spec 12's `manual`", function()
      assert.are.equal("manual", integrate.mode_for("/vault/areas/health/target.md"))

      with_routes({
        { route_name = "impro", destination = "/vault/areas/health/target.md", mode = "integrate" },
      }, "append")
      local mode, route = integrate.mode_for("/vault/areas/health/target.md")
      assert.are.equal("integrate", mode)
      assert.are.equal("impro", route.route_name)
      -- a destination NO route claims falls through to the core's default
      assert.are.equal("append", (integrate.mode_for("/vault/projects/alpha/other.md")))
    end)

    it("matches a folder route against the notes inside it", function()
      with_routes({
        { route_name = "fitness", destination = "/vault/areas/fitness", is_folder = true, mode = "integrate" },
      })
      assert.are.equal("integrate", (integrate.mode_for("/vault/areas/fitness/log.md")))
      assert.are.equal("manual", (integrate.mode_for("/vault/areas/fitness-other/log.md")))
    end)

    it("resolves the capture's routes once per capture, quietly", function()
      setup_with({
        ["routes.resolve"] = function()
          return { { route_name = "impro", destination = "/vault/areas/health/target.md", mode = "integrate" } }
        end,
      })
      actions.load_current()
      assert.are.equal(1, client:count("routes.resolve"))
      assert.are.equal("/vault/capture/one.md", client:first("routes.resolve").params.path)
      assert.are.equal("integrate", (integrate.mode_for("/vault/areas/health/target.md")))
      integrate.load_routes()
      assert.are.equal(1, client:count("routes.resolve"), "cached for the capture it was resolved for")
    end)

    it("degrades SILENTLY to `manual` on a core with no routes.resolve", function()
      -- The `op.skip` precedent: a passive surface an older core cannot serve
      -- must not scold. `routes.resolve` is not scripted here, so the mock
      -- answers MethodNotFound.
      actions.load_current()
      assert.is_false((notified("routes")))
      assert.is_false((notified("MethodNotFound")))
      assert.are.equal("manual", (integrate.mode_for("/vault/areas/health/target.md")))
    end)
  end)

  -- --- dispatch ------------------------------------------------------------

  describe("dispatch (spec 12 §1's three modes)", function()
    it("`manual` opens the merge editor and proposes nothing", function()
      actions.integrate_or_merge("/vault/areas/health/target.md")
      assert.are.equal(1, client:count("op.merge_preview"))
      assert.are.equal(0, client:count("op.integrate_propose"))
      assert.are.equal("merge", state.view)
    end)

    it("`append` is SURFACED, not performed — no op fires at all", function()
      with_routes({
        { route_name = "workout", destination = "/vault/areas/health/target.md", mode = "append" },
      })
      actions.integrate_or_merge("/vault/areas/health/target.md")
      assert.are.equal(0, client:count("op.merge_preview"))
      assert.are.equal(0, client:count("op.integrate_propose"))
      assert.is_true((notified("mode: append")))
      assert.is_true((notified("route: workout")))
      assert.is_true((notified("routes apply")), "the append path must name where appends DO happen")
      assert.are.equal("suggestions", state.view, "nothing was opened")
    end)

    it("`integrate` proposes and raises the review gate, writing nothing", function()
      with_routes({
        { route_name = "impro", destination = "/vault/areas/health/target.md", mode = "integrate" },
      })
      actions.integrate_or_merge("/vault/areas/health/target.md")

      local call = client:first("op.integrate_propose")
      assert.is_truthy(call)
      -- the one op.* asymmetry: the capture param is `note`, not `path`
      assert.are.equal("/vault/capture/one.md", call.params.note)
      assert.are.equal("/vault/areas/health/target.md", call.params.target)
      assert.are.equal("impro", call.params.route)
      assert.are.equal(0, client:count("op.integrate_commit"), "propose writes nothing and commits nothing")

      assert.are.equal("integrate", state.view)
      assert.are.equal(DIFF, state.integrate.diff)
      assert.are.equal("diff", state.integrate.review)
      assert.are.equal("organize", fui.focused)
    end)

    it("`<leader>mm` overrides the route's mode for this invocation", function()
      with_routes({
        { route_name = "workout", destination = "/vault/areas/health/target.md", mode = "append" },
      })
      vim.ui.select = function(items, _, cb)
        assert.are.same({ "manual", "append", "integrate" }, items)
        cb("integrate")
      end
      integrate.choose_mode("/vault/areas/health/target.md")
      assert.are.equal(1, client:count("op.integrate_propose"))
      assert.are.equal("integrate", state.view)
    end)

    it("`m` / merge_with stays MANUAL even on an integrate route", function()
      -- `m` means "I will edit this myself" (spec 03 §5). A key that sometimes
      -- summoned an LLM instead is the 08 §A19 class of surprise.
      with_routes({
        { route_name = "impro", destination = "/vault/areas/health/target.md", mode = "integrate" },
      })
      actions.merge_with("/vault/areas/health/target.md")
      assert.are.equal(1, client:count("op.merge_preview"))
      assert.are.equal(0, client:count("op.integrate_propose"))
    end)
  end)

  -- --- the review gate -----------------------------------------------------

  describe("the review gate (spec 12 §1)", function()
    local function open_gate(overrides, dispatch_opts)
      setup_with(overrides)
      integrate.propose("/vault/areas/health/target.md", dispatch_opts)
      return state.integrate
    end

    it("accept commits `accepted` and echoes the proposal VERBATIM", function()
      open_gate()
      actions.accept()

      local commit = client:first("op.integrate_commit")
      assert.is_truthy(commit)
      assert.are.equal("accepted", commit.params.verdict)
      assert.is_nil(commit.params.final_diff, "an accepted verdict applies the proposal unchanged")

      local sent = commit.params.proposal
      assert.are.equal("prop_01J0TESTTESTTESTTESTTESTTE", sent.proposal_id)
      assert.are.equal(DIFF, sent.diff)
      assert.are.same({ backend = "claude-cli", model = "fake-sonnet", prompt_hash = "abc123", proposed_diff = DIFF }, sent.llm)
      -- `review` is the server's annotation, not part of IntegrationProposal.
      assert.is_nil(sent.review)
    end)

    it("round-trips target_snapshot.mtime WITHOUT losing precision", function()
      -- The defect this pins made EVERY merge fail with
      -- ConcurrentModificationError: `vim.json.encode` renders a Lua number at
      -- ~14 significant digits, so echoing the decoded snapshot back changed
      -- the core's float mtime. `check_unmodified` compares it exactly, so an
      -- integrate commit would have been unreachable in exactly the same way.
      assert.is_false(
        vim.json.decode(vim.json.encode({ mtime = EXACT_MTIME })).mtime == EXACT_MTIME,
        "precondition: encoding a Lua float is lossy"
      )
      open_gate()
      actions.accept()

      local snapshot = client:first("op.integrate_commit").params.proposal.target_snapshot
      assert.are.equal("string", type(snapshot.mtime))
      assert.are.equal(EXACT_MTIME, tonumber(snapshot.mtime))
      assert.are.equal(EXACT_MTIME, tonumber(vim.json.decode(vim.json.encode(snapshot)).mtime))
      assert.are.equal("b77bb54deadbeef", snapshot.sha256)
    end)

    it("`e` opens the diff for editing and accept then sends verdict `edited`", function()
      open_gate()
      assert.is_false(state.integrate.editing)
      integrate.edit()
      assert.is_true(state.integrate.editing)

      fui.content = DIFF:gsub("An idea about improv warmups%.", "An idea about improv warmups, in Matt's words.")
      actions.merge_complete() -- `<leader>mc`, the gate's other accept key

      local commit = client:first("op.integrate_commit")
      assert.are.equal("edited", commit.params.verdict)
      assert.is_truthy(commit.params.final_diff)
      assert.are_not.equal(commit.params.proposal.diff, commit.params.final_diff)
      -- 12 §2's labelled-edit example needs BOTH halves, and the proposed one
      -- must be untouched by the editing.
      assert.are.equal(DIFF, commit.params.proposal.diff)
      assert.is_truthy(commit.params.final_diff:find("in Matt's words", 1, true))
    end)

    it("opening the editor and changing nothing is an ACCEPT, not an edit", function()
      -- Recording `edited` with two identical diffs would poison the corpus
      -- with a non-edit; the core would also refuse the mismatch.
      open_gate()
      integrate.edit()
      fui.content = DIFF
      actions.accept()
      assert.are.equal("accepted", client:first("op.integrate_commit").params.verdict)
      assert.is_nil(client:first("op.integrate_commit").params.final_diff)
    end)

    it("reject COMMITS the verdict — the negative signal is the point", function()
      open_gate()
      actions.merge_cancel() -- `<leader>mx`

      local commit = client:first("op.integrate_commit")
      assert.are.equal("rejected", commit.params.verdict)
      assert.is_nil(state.integrate, "the gate closes")
      assert.are.equal("suggestions", state.view, "the pane returns to where it was")
      assert.is_true((notified("rejected")))
      assert.is_true((notified("untouched")))
    end)

    it("carries the spec 12 §2 decision context, ranked against the target's folder", function()
      open_gate()
      actions.accept()
      local params = client:first("op.integrate_commit").params
      assert.are.equal("ses_integrate", params.session_id)
      assert.are.equal(2, #params.suggestions_shown)
      -- /vault/areas/health is suggestion #2, and integrating into a note
      -- THERE is a decision for that folder (spec 03 §6's merge rule).
      assert.are.equal(2, params.chosen_rank)
    end)

    it("does NOT archive or advance, and says so (the visible asymmetry)", function()
      -- A standalone integrate archives nothing — "I may be adding them to
      -- multiple files" (12's opening directive) and the commit is stateless.
      -- The obligation that creates is that the capture must not silently sit
      -- in the backlog with nothing saying why.
      open_gate()
      actions.accept()
      assert.are.equal(1, state.current, "no auto-advance")
      assert.are.same({}, state.processed, "the capture is not processed by an integrate")
      assert.is_true((notified("still in the backlog")))
      assert.is_true((notified("one%.md")), "the notice names the capture")
      assert.is_true((notified("press a to archive")), "…and the key that finishes the job")
    end)

    it("keeps the gate up when the commit fails, so the verdict can be retried", function()
      open_gate({
        ["op.integrate_commit"] = function()
          return nil, { code = -32000, message = "target changed on disk", data = { kind = "ConcurrentModificationError", hint = "re-propose" } }
        end,
      })
      actions.accept()
      assert.is_truthy(state.integrate, "the proposal survives a failed commit")
      assert.are.equal("integrate", state.view)
      assert.is_true((notified("target changed on disk")))
      assert.is_true((notified("ConcurrentModificationError")))
    end)

    it("surfaces an ok=false result without losing the proposal", function()
      open_gate({
        ["op.integrate_commit"] = function()
          return { ok = false, operation = "integrate", source = "/vault/capture/one.md", error = "deletion guard: 4 lines removed" }
        end,
      })
      actions.accept()
      assert.is_truthy(state.integrate)
      assert.is_true((notified("deletion guard")))
    end)

    it("commits ONE verdict however many times the key is pressed", function()
      -- The mock replies synchronously, so a client with no in-flight guard
      -- would send two commits for one proposal and record the same
      -- integration twice.
      open_gate()
      actions.accept()
      actions.accept()
      assert.are.equal(1, client:count("op.integrate_commit"))
    end)

    it("refuses a second proposal while one is under review", function()
      open_gate()
      integrate.propose("/vault/projects/alpha/other.md")
      assert.are.equal(1, client:count("op.integrate_propose"))
      assert.is_true((notified("already under review")))
    end)

    it("`review = \"auto\"` applies without asking (the per-route opt-in)", function()
      open_gate({
        ["op.integrate_propose"] = function()
          return proposal_payload({ review = "auto" })
        end,
      })
      assert.are.equal(1, client:count("op.integrate_commit"))
      assert.are.equal("accepted", client:first("op.integrate_commit").params.verdict)
      assert.is_nil(state.integrate)
    end)

    it("marks a dry run everywhere it can be seen", function()
      open_gate(nil, { dry_run = true })
      assert.is_true(client:first("op.integrate_propose").params.dry_run)
      local lines = table.concat(integrate.render(state, ui.config()).lines, "\n")
      assert.is_truthy(lines:find("DRY RUN", 1, true))
      actions.accept()
      assert.is_true(client:first("op.integrate_commit").params.dry_run)
      assert.is_true((notified("DRY RUN")))
    end)

    it("drops a gate that belongs to a capture Matt has moved on from", function()
      open_gate()
      assert.is_truthy(state.integrate)
      actions.next_capture()
      assert.is_nil(state.integrate, "a proposal belongs to ONE capture")
    end)
  end)

  -- --- degradation ---------------------------------------------------------

  describe("graceful degradation (spec 10 §1)", function()
    it("an older core with no op.integrate_propose degrades to the merge path", function()
      setup_with({ ["op.integrate_propose"] = false })
      with_routes({
        { route_name = "impro", destination = "/vault/areas/health/target.md", mode = "integrate" },
      })
      assert.has_no.errors(function()
        actions.integrate_or_merge("/vault/areas/health/target.md")
      end)
      -- ONE clear line, naming the fallback: unlike the passive route probe,
      -- this was an explicit keystroke, and a key that does nothing at all is
      -- the 09 §1.5 failure rather than the fix.
      assert.is_true((notified("no `integrate`")))
      assert.is_true((notified("merge")))
      assert.is_nil(state.integrate)
      assert.are.equal("suggestions", state.view, "the pane is restored, not left loading")
    end)

    it("does nothing dangerous with no capture, no target and no gate", function()
      setup_with({}, { captures = {}, current = 1 })
      assert.has_no.errors(function()
        integrate.propose("/vault/areas/health/target.md")
        integrate.accept()
        integrate.edit()
        integrate.reject()
        integrate.commit("accepted")
        integrate.dispatch(nil)
        integrate.choose_mode(nil)
      end)
      assert.are.equal(0, client:count("op.integrate_propose"))
      assert.are.equal(0, client:count("op.integrate_commit"))
    end)

    it("refuses a verdict that is not one of the three", function()
      setup_with({})
      integrate.propose("/vault/areas/health/target.md")
      integrate.commit("maybe")
      assert.are.equal(0, client:count("op.integrate_commit"))
      assert.is_true((notified("not an integrate verdict")))
    end)
  end)

  -- --- rendering + keymaps -------------------------------------------------

  describe("rendering the gate", function()
    it("shows the target, the gate, the rationale and the highlighted diff", function()
      integrate.propose("/vault/areas/health/target.md")
      local rendered = integrate.render(state, ui.config())
      local text = table.concat(rendered.lines, "\n")
      assert.is_truthy(text:find("Integrate — review", 1, true))
      assert.is_truthy(text:find("/vault/areas/health/target.md", 1, true))
      assert.is_truthy(text:find("review: diff", 1, true))
      assert.is_truthy(text:find("Woven under a new Ideas heading.", 1, true))
      assert.is_truthy(text:find("+## Ideas", 1, true))

      local by_line = {}
      for _, mark in ipairs(rendered.marks) do
        by_line[rendered.lines[mark.line]] = mark.hl
      end
      assert.are.equal("DiffAdd", by_line["+## Ideas"])
      assert.are.equal("DiffText", by_line["@@ -1,5 +1,8 @@"])
    end)

    it("renders ONLY the diff while editing, so no header can reach final_diff", function()
      -- Spec 03 §5's rule, applied to the gate: what is in the modifiable
      -- buffer is sent verbatim, so instructions live in virtual text.
      integrate.propose("/vault/areas/health/target.md")
      integrate.edit()
      local lines = integrate.render(state, ui.config()).lines
      assert.are.same(vim.split(DIFF, "\n", { plain = true }), lines)
      assert.is_truthy(integrate.hint(state, ui.config()):find("INTEGRATE", 1, true))
    end)

    it("names the keys it tells Matt to press, from the live keymap table", function()
      actions.setup({ config = ui.setup({ ui = { close_on_complete = false }, keymaps = { buffer = { integrate_edit = "E" } } }) })
      integrate.propose("/vault/areas/health/target.md")
      local text = table.concat(integrate.render(state, actions.context().config).lines, "\n")
      assert.is_truthy(text:find("E edit", 1, true), text)
    end)
  end)

  describe("keymaps", function()
    it("lists the edit-mode and review-gate rows in the generated help", function()
      local by_name = {}
      for _, entry in ipairs(actions.keymap_table()) do
        by_name[entry.name] = entry
      end
      assert.are.equal("<leader>mi", by_name.integrate.lhs)
      assert.are.equal("<leader>mm", by_name.integrate_mode.lhs)
      assert.are.equal("e", by_name.integrate_edit.lhs)
      -- Spec 03 §2: the `?` overlay is generated from the real table, so a key
      -- Matt can press that help never mentions cannot exist.
      assert.are.equal("Review gate", by_name.integrate_edit.group)
      assert.are.equal("integrate", by_name.integrate_edit.view)
    end)

    it("does NOT shadow `e` in the organize pane when no proposal is up", function()
      actions.setup({ ui = ui, config = ui.setup({ ui = { close_on_complete = false } }) })
      ui.mount(state)
      local bound = {}
      for _, map in ipairs(vim.api.nvim_buf_get_keymap(ui.current_bufs().organize, "n")) do
        bound[map.lhs] = map.desc
      end
      assert.is_nil(bound["e"], "`e` is plain cursor motion until the review gate is up")
      assert.is_truthy(bound[" mi"] or bound["<leader>mi"] or bound[" mi"])
      ui.unmount()
    end)

    it("binds `e` only while the gate is up, and unbinds it after the verdict", function()
      actions.setup({ ui = ui, client = client, state = state, config = ui.setup({ ui = { close_on_complete = false } }) })
      ui.mount(state)
      local organize = ui.current_bufs().organize

      local function has_e()
        for _, map in ipairs(vim.api.nvim_buf_get_keymap(organize, "n")) do
          if map.lhs == "e" then
            return true
          end
        end
        return false
      end

      assert.is_false(has_e())
      integrate.propose("/vault/areas/health/target.md")
      assert.is_true(has_e(), "the gate binds its own edit key")
      actions.merge_cancel()
      assert.is_false(has_e(), "and takes it away again")
      ui.unmount()
    end)

    it("makes the organize buffer modifiable ONLY while editing the diff", function()
      actions.setup({ ui = ui, client = client, state = state, config = ui.setup({ ui = { close_on_complete = false } }) })
      ui.mount(state)
      local organize = ui.current_bufs().organize
      integrate.propose("/vault/areas/health/target.md")
      assert.is_false(vim.bo[organize].modifiable, "the review pane is read-only")
      integrate.edit()
      assert.is_true(vim.bo[organize].modifiable)
      -- and the buffer really holds the diff, so `organize_content` is the
      -- final diff rather than a rendered header.
      assert.are.same(vim.split(DIFF, "\n", { plain = true }), vim.api.nvim_buf_get_lines(organize, 0, -1, false))
      ui.unmount()
    end)
  end)
end)

---------------------------------------------------------------------------
-- part 2 — the real core, the real wire, a fake model
---------------------------------------------------------------------------

--- Paths inside the fixture vault (`tests/conftest.py:build_fixture_vault`,
--- the SAME builder the python suite uses, so the two corpora cannot drift).
local CAPTURE = "capture/raw_capture/2026-06-10T21:33:05.379Z.md" -- tags: impro, creativity
local TARGET = "resources/performing/impro.md"

local function read(path)
  local fd = assert(uv.fs_open(path, "r", 438), "cannot open " .. path)
  local stat = uv.fs_fstat(fd)
  local data = uv.fs_read(fd, stat.size, 0)
  uv.fs_close(fd)
  return data or ""
end

local function write(path, text)
  vim.fn.mkdir(vim.fn.fnamemodify(path, ":h"), "p")
  local fd = assert(uv.fs_open(path, "w", tonumber("644", 8)))
  uv.fs_write(fd, text, 0)
  uv.fs_close(fd)
end

--- The capture's BODY, derived from the fixture rather than hardcoded, so the
--- golden below cannot drift away from what the vault actually holds.
local function capture_body(vault)
  local text = read(vault .. "/" .. CAPTURE)
  local body = text:match("^%-%-%-\n.-\n%-%-%-\n(.*)$") or text
  return vim.trim(body)
end

--- What a well-behaved model returns: the target with the capture's words
--- woven in VERBATIM (spec 12 §1's style directive), adding only.
local function woven(vault)
  return read(vault .. "/" .. TARGET) .. "\n## Ideas\n\n" .. capture_body(vault) .. "\n"
end

--- A `claude -p` stand-in: ignores its prompt, prints one fixed JSON object.
--- The prompt CONTRACT is the core suite's business; what this fake is for is
--- everything AROUND it — config → get_client → ClaudeCLIClient's argv/stdin/
--- timeout/utf-8 handling → the guards → the atomic write.
local function install_fake_claude(sb, content, rationale)
  local payload = sb.dir .. "/fake-claude-payload.json"
  write(payload, vim.json.encode({ content = content, rationale = rationale or "Woven under Ideas." }))
  local script = sb.dir .. "/fake-claude.py"
  write(
    script,
    table.concat({
      "import sys",
      "sys.stdin.read()",
      ("sys.stdout.write(open(%q, encoding='utf-8').read())"):format(payload),
      "",
    }, "\n")
  )
  return script
end

local function write_core_config(sb, script, review)
  write(
    sb.config_dir .. "/config.toml",
    table.concat({
      "[vault]",
      ('root = "%s"'):format(sb.vault),
      "",
      "[llm]",
      'backend = "claude-cli"',
      'integrate_backend = "claude-cli"',
      ('claude_command = ["%s", "%s"]'):format(helpers.python_bin(), script),
      "",
      "[integrate]",
      ('review = "%s"'):format(review or "diff"),
      "",
      "[[routes]]",
      'tags = ["creativity"]',
      ('destination = "%s"'):format(TARGET),
      'mode = "integrate"',
      ('review = "%s"'):format(review or "diff"),
      'description = "Improv practice notes."',
      "",
    }, "\n")
  )
end

local function until_true(fn, timeout_ms, what)
  local value
  local ok = vim.wait(timeout_ms or 30000, function()
    value = fn()
    return value ~= nil and value ~= false
  end, 20)
  if not ok then
    error("timed out waiting for " .. (what or "condition"), 2)
  end
  return value
end

--- Every ActionRecord the core has written, decoded.
local function action_records(sb)
  local out = {}
  local handle = uv.fs_scandir(sb.state_dir .. "/actions")
  if not handle then
    return out
  end
  while true do
    local name = uv.fs_scandir_next(handle)
    if not name then
      break
    end
    for line in (read(sb.state_dir .. "/actions/" .. name) .. "\n"):gmatch("([^\n]*)\n") do
      if line ~= "" then
        local ok, decoded = pcall(vim.json.decode, line)
        if ok and type(decoded) == "table" then
          out[#out + 1] = decoded
        end
      end
    end
  end
  return out
end

local function records_of(sb, operation)
  local out = {}
  for _, record in ipairs(action_records(sb)) do
    if record.operation == operation then
      out[#out + 1] = record
    end
  end
  return out
end

describe("E2E: integrate through a REAL core (spec 12 §1, 09 §3)", function()
  local sb, init, state, actions_mod, ui_mod, commands, core, integrate_mod

  before_each(function()
    for _, name in ipairs({
      "para-organize",
      "para-organize.config",
      "para-organize.state",
      "para-organize.actions",
      "para-organize.integrate",
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
    state = require("para-organize.state")
    actions_mod = require("para-organize.actions")
    integrate_mod = require("para-organize.integrate")
    ui_mod = require("para-organize.ui")
    commands = require("para-organize.commands")
    core = require("para-organize.core")

    sb = helpers.sandbox()
    helpers.build_vault(sb)
  end)

  after_each(function()
    pcall(function()
      init.stop({ quiet = true })
    end)
    pcall(core.stop)
    pcall(state.reset)
    pcall(actions_mod.reset)
    helpers.cleanup(sb)
  end)

  --- Start a session and park it on the capture the route matches.
  local function session_on_capture()
    commands.execute({ fargs = { "start" } })
    until_true(function()
      return state.has_session()
    end, 40000, "session.start to return captures")
    local session = state.get()
    local wanted = sb.vault .. "/" .. CAPTURE
    local index
    for i, record in ipairs(session.captures) do
      if record.path == wanted then
        index = i
      end
    end
    assert.is_truthy(index, "the fixture capture should be in the session")
    session.current = index
    actions_mod.load_current()
    -- Wait for the routes resolved for THIS capture, not for whichever
    -- capture the session opened on: `routes.resolve` matches on the
    -- capture's tags, so a stale table would answer the mode question for the
    -- wrong note.
    until_true(function()
      return session.view == "suggestions" and (session.routes or {}).path == wanted
    end, 40000, "the capture and its routes to load")
    return session, wanted
  end

  it("propose → accept in the UI puts the reviewed bytes on disk", function()
    local script = install_fake_claude(sb, "PLACEHOLDER")
    write_core_config(sb, script)
    local expected = woven(sb.vault)
    install_fake_claude(sb, expected) -- now that the target's bytes are known
    local body = capture_body(sb.vault)
    assert.is_truthy(body ~= "" and expected:find(body, 1, true), "the fixture really carries the words")

    init.setup(helpers.plugin_config(sb))
    local session = session_on_capture()
    local target = sb.vault .. "/" .. TARGET
    local before = read(target)

    -- The route reached the CLIENT: this is `routes.resolve` over the real
    -- wire deciding which of spec 12 §1's three modes `<CR>` runs.
    local mode, route = integrate_mod.mode_for(target)
    assert.are.equal("integrate", mode)
    assert.are.equal("creativity", route.route_name)

    -- `<CR>` on the `[F]` line for that note.
    actions_mod.integrate_or_merge(target)
    until_true(function()
      return session.view == "integrate"
    end, 60000, "the review gate to open")

    -- PROPOSE WROTE NOTHING. Asserted before the commit rather than inferred.
    assert.are.equal(before, read(target))
    local gate = session.integrate
    assert.is_truthy(gate.proposal.proposal_id:find("^prop_"))
    assert.are.equal("diff", gate.review)
    assert.is_truthy(gate.diff:find("^--- "))
    assert.is_truthy(gate.rationale)
    -- the route's description reached the proposal (12 §2 / 11 §1)
    assert.are.equal("Improv practice notes.", gate.proposal.description)
    -- and the diff is on screen
    local pane = table.concat(vim.api.nvim_buf_get_lines(ui_mod.current_bufs().organize, 0, -1, false), "\n")
    assert.is_truthy(pane:find("Integrate — review", 1, true))
    assert.is_truthy(pane:find("+## Ideas", 1, true))

    local proposal_id = gate.proposal.proposal_id
    actions_mod.accept()
    -- Wait for the COMMIT REPLY, not merely for the bytes: the core writes
    -- before it answers, so a disk-only wait races the client's own
    -- post-commit state (the gate coming down, the notice).
    until_true(function()
      return session.integrate == nil
    end, 60000, "the commit reply to land")
    assert.are_not.equal(before, read(target))

    ------------------------------------------------------------------
    -- 1. EXACT bytes: what was reviewed is what landed
    ------------------------------------------------------------------
    assert.are.equal(expected, read(target))
    -- 12 §1's VERBATIM directive, on the bytes actually on disk
    assert.is_truthy(read(target):find(body, 1, true))

    ------------------------------------------------------------------
    -- 2. ONE ActionRecord with the full doc-12 §2 trace
    ------------------------------------------------------------------
    local records = until_true(function()
      local found = records_of(sb, "integrate")
      return #found == 1 and found or false
    end, 30000, "the integrate ActionRecord")
    local record = records[1]
    assert.are.equal("claude-integrate", record.actor, "the RPC door forces the actor")
    assert.are.equal("integrate", record.edit_mode)
    assert.are.equal("accepted", record.llm.verdict)
    assert.are.equal("claude-cli", record.llm.backend)
    assert.are.equal(proposal_id, record.llm.proposal_id)
    -- proposed == final for an accepted verdict, and both are the real diff
    assert.are.equal(record.llm.proposed_diff, record.llm.final_diff)
    assert.are.equal(sb.vault .. "/" .. CAPTURE, record.capture.path)
    assert.are.equal(1, #record.targets)
    assert.are.equal("Improv practice notes.", record.targets[1].description)
    assert.are_not.equal(record.targets[1].before_hash, record.targets[1].after_hash)
    -- the client's own decision context survived the round trip (12 §2)
    assert.are.equal(session.session_id, record.context.session_id)

    ------------------------------------------------------------------
    -- 3. the capture is deliberately NOT archived (the visible asymmetry)
    ------------------------------------------------------------------
    assert.is_truthy(uv.fs_stat(sb.vault .. "/" .. CAPTURE), "a standalone integrate archives nothing")
    assert.are.same({}, session.processed)
    assert.is_nil(session.integrate, "the gate is down")
  end)

  it("`e` → hand-edit the diff → accept lands MATT's bytes and records `edited`", function()
    -- The half that turns a review into a labelled edit example (12 §2:
    -- "`proposed_diff` vs `final_diff` turns every reviewed integration into
    -- a labeled edit example"). Driven through the REAL modifiable buffer, so
    -- what is asserted is the whole path: `e` → buffer → `organize_content`
    -- → `final_diff` → the core's re-run guards → the atomic write.
    local script = install_fake_claude(sb, "PLACEHOLDER")
    write_core_config(sb, script)
    local proposed_result = woven(sb.vault)
    install_fake_claude(sb, proposed_result)

    init.setup(helpers.plugin_config(sb))
    local session = session_on_capture()
    local target = sb.vault .. "/" .. TARGET

    actions_mod.integrate_or_merge(target)
    until_true(function()
      return session.view == "integrate"
    end, 60000, "the review gate to open")
    local proposed_diff = session.integrate.diff

    integrate_mod.edit()
    local organize = ui_mod.current_bufs().organize
    assert.is_true(vim.bo[organize].modifiable, "`e` makes the diff editable")
    local edited_diff = proposed_diff:gsub("\n$", "") .. "\n+Reviewed by Matt.\n"
    vim.api.nvim_buf_set_lines(organize, 0, -1, false, vim.split(edited_diff, "\n", { plain = true }))

    actions_mod.accept()
    until_true(function()
      return session.integrate == nil
    end, 60000, "the commit reply to land")

    -- MATT's bytes, not the model's.
    local on_disk = read(target)
    assert.are.equal(proposed_result .. "Reviewed by Matt.\n", on_disk)
    assert.are_not.equal(proposed_result, on_disk)

    local records = until_true(function()
      local found = records_of(sb, "integrate")
      return #found == 1 and found or false
    end, 30000, "the edited ActionRecord")
    local record = records[1]
    assert.are.equal("edited", record.llm.verdict)
    -- BOTH diffs, and they differ — which is the entire point of the verdict.
    assert.are.equal(proposed_diff, record.llm.proposed_diff)
    assert.are_not.equal(record.llm.proposed_diff, record.llm.final_diff)
    assert.is_truthy(record.llm.final_diff:find("Reviewed by Matt.", 1, true))
    -- …and the capture is STILL unarchived, exactly as for an accept.
    assert.is_truthy(uv.fs_stat(sb.vault .. "/" .. CAPTURE))
    assert.are.same({}, session.processed)
  end)

  it("reject records the verdict and leaves every vault byte alone", function()
    local script = install_fake_claude(sb, "PLACEHOLDER")
    write_core_config(sb, script)
    install_fake_claude(sb, woven(sb.vault))

    init.setup(helpers.plugin_config(sb))
    local session = session_on_capture()
    local target = sb.vault .. "/" .. TARGET
    local before = read(target)
    local capture_before = read(sb.vault .. "/" .. CAPTURE)

    actions_mod.integrate_or_merge(target)
    until_true(function()
      return session.view == "integrate"
    end, 60000, "the review gate to open")
    local proposal_id = session.integrate.proposal.proposal_id

    actions_mod.merge_cancel() -- `<leader>mx` = reject

    -- The RECORD is the whole deliverable here: a rejection that recorded
    -- nothing is indistinguishable from a proposal that was never made, and
    -- doc 12's premise is that "a rejection is as much signal as an
    -- acceptance".
    local records = until_true(function()
      local found = records_of(sb, "integrate")
      return #found == 1 and found or false
    end, 30000, "the rejected ActionRecord")
    local record = records[1]
    assert.are.equal("rejected", record.llm.verdict)
    assert.are.equal(proposal_id, record.llm.proposal_id)
    assert.are.equal("claude-integrate", record.actor)
    assert.is_truthy(record.llm.proposed_diff ~= "", "the rejected proposal is stored")

    -- …and NOTHING was written.
    assert.are.equal(before, read(target))
    assert.are.equal(capture_before, read(sb.vault .. "/" .. CAPTURE))
    until_true(function()
      return session.view ~= "loading"
    end, 30000, "the pane to come back")
    assert.is_nil(session.integrate)
    assert.are.same({}, session.processed)
  end)
end)
