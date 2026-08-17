--- para-organize.integrate — edit modes and the `integrate` review gate
--- (spec 12 §1).
---
--- THIN CLIENT LAW (spec 10 §1), restated for the one path most tempted to
--- break it: this module NEVER applies a diff, never reads a vault file and
--- never writes one. It asks the core for a proposal, RENDERS it, collects
--- Matt's verdict, and hands the proposal straight back. The bytes on disk
--- are produced by `integrate.apply` inside organize-core, behind the
--- deletion guard, the `no-ai` refusal and the TOCTOU check — all of which
--- re-run at commit precisely because nothing this client says is trusted for
--- safety (ARCHITECTURE, "Phase-5 integrate wire contract").
---
--- The wire is STATELESS: `op.integrate_propose` returns the COMPLETE
--- proposal and `op.integrate_commit` takes it back verbatim, because review
--- time is exactly when an idle core exits and server-held state would yield
--- "unknown proposal_id" at the moment Matt presses accept. `proposal_id` is
--- a correlation id echoed into the ActionRecord, never a lookup key.
---
--- The three edit modes of spec 12 §1 (`manual` / `append` / `integrate`)
--- are resolved PER DESTINATION from the route table, with a per-invocation
--- override (`<leader>mm`). What each mode means here:
---
---   manual     → the spec 03 §5 merge editor (`actions.merge_with`).
---   append     → deterministic, no LLM, and NOT a client operation: it is
---                the route/pipeline's job (11 §1). The client SURFACES it.
---   integrate  → propose → review gate → verdict, this module.
---
--- Public API:
---   setup/reset · load_routes · routes_for · mode_for · route_for
---   dispatch · choose_mode · propose · accept · edit · reject · commit
---   render · hint · is_active · GATE_VIEW · MODES
---
--- Owned by the NVIM CLIENT seat (Phase 5).

local M = {}

local render = require("para-organize.ui.render")

--- The right pane's fifth state (spec 03 §3 has four; 12 §1 adds the review
--- gate). `state.view` takes this value while a proposal is on screen.
M.GATE_VIEW = "integrate"

--- Spec 12 §1's table, in the order it is documented.
M.MODES = { "manual", "append", "integrate" }

--- Spec 12 §1: "global default `manual`". The core owns the real default
--- (`[integrate] default_mode`); until `routes.resolve` reports it (see the
--- seam note on `routes_for`) the client assumes the spec's default rather
--- than inventing a different one.
M.DEFAULT_MODE = "manual"

--- The three verdicts of the review gate. Every one of them COMMITS: a
--- rejection writes no vault byte but DOES write the ActionRecord, because
--- "a rejection is as much signal as an acceptance" (12 §2) and a rejection
--- that recorded nothing is indistinguishable from a proposal never made.
M.VERDICTS = { "accepted", "edited", "rejected" }

--- Default review gate. The core's answer travels back ON the proposal
--- (`payload.review`), so this is only the value used before one exists.
M.DEFAULT_REVIEW = "diff"

---------------------------------------------------------------------------
-- plumbing
---------------------------------------------------------------------------

--- Lazily, because `actions` requires THIS module for its keymap rows: a
--- top-level require either way would be a load-order cycle.
local function actions()
  return require("para-organize.actions")
end

local function ui()
  local injected = actions().context().ui
  if type(injected) == "table" then
    return injected
  end
  return require("para-organize.ui")
end

local function session()
  return actions().context().state
end

local function cfg()
  local injected = actions().context().config
  if type(injected) == "table" then
    return injected
  end
  return ui().config()
end

local function notify(msg, level)
  vim.notify("para-organize: " .. msg, level or vim.log.levels.INFO)
end

local function warn(msg)
  notify(msg, vim.log.levels.WARN)
end

local function refresh()
  local mod = ui()
  if type(mod.refresh) == "function" then
    mod.refresh(session())
  end
  local s = session()
  if s and type(s.emit) == "function" then
    pcall(s.emit, s)
  end
end

--- Is this error an older core answering "I have never heard of that"?
---
--- JSON-RPC reserves -32601 for it; the core additionally tags `data.kind`.
--- Both are checked because the client must degrade the same way against a
--- core that predates the tag.
function M.is_method_missing(err)
  if type(err) ~= "table" then
    return false
  end
  if tonumber(err.code) == -32601 then
    return true
  end
  local kind = err.kind or (type(err.data) == "table" and err.data.kind or nil)
  return kind == "MethodNotFound"
end

--- One presence test for a decoded-JSON optional (the wire's `null` decodes
--- to `vim.NIL`, which is TRUTHY in Lua — render.lua documents the same trap).
local function present(value)
  return value ~= nil and value ~= vim.NIL and value ~= ""
end

local function text_or_nil(value)
  if not present(value) then
    return nil
  end
  return tostring(value)
end

---------------------------------------------------------------------------
-- route → mode resolution (spec 12 §1 "per-route default via `mode`")
---------------------------------------------------------------------------

--- Normalise whichever shape `routes.resolve` answers with.
---
--- SEAM (recorded for the integrator): the CLI's `organize routes resolve
--- --json` is an OBJECT — `{default_mode, matches: [{… effective mode,
--- effective review …}]}` — but the RPC handler still returns a BARE ARRAY
--- carrying `match.route.mode` (the RAW per-route mode) and no `review` and
--- no `default_mode`. Spec 10 §3's rule is that "CLI and UI can never
--- disagree", and the missing pieces are exactly the doc 12 §1 answer for a
--- capture that matched nothing. Rather than reimplement `effective_mode` /
--- `effective_review` in Lua — which is how the two frontends start to
--- disagree — this reader accepts BOTH shapes and falls back to the spec's
--- documented default. It upgrades itself the day the RPC reply grows the
--- object form; nothing here needs to change.
function M.normalize_routes(payload)
  local out = { default_mode = nil, matches = {} }
  if type(payload) ~= "table" then
    return out
  end
  local matches = payload
  if payload.matches ~= nil or payload.default_mode ~= nil then
    matches = type(payload.matches) == "table" and payload.matches or {}
    out.default_mode = text_or_nil(payload.default_mode)
  end
  for _, match in ipairs(matches) do
    if type(match) == "table" and present(match.destination) then
      out.matches[#out.matches + 1] = {
        route_name = text_or_nil(match.route_name),
        destination = tostring(match.destination),
        is_folder = match.is_folder == true,
        mode = text_or_nil(match.mode) or M.DEFAULT_MODE,
        review = text_or_nil(match.review),
        description = text_or_nil(match.description),
        auto = match.auto == true,
      }
    end
  end
  return out
end

--- Fetch (once per capture) the routes that match the current capture.
---
--- Cached exactly like `meta.fields` and the browse roots, and keyed by the
--- capture's PATH because a route matches on the capture's tags. `quiet`
--- throughout: this is a passive surface, so an older core with no
--- `routes.resolve` must degrade SILENTLY to "manual" — the `op.skip`
--- precedent. Pressing a key is a different matter; see `dispatch`.
---@param done fun(routes: table)|nil
function M.load_routes(done)
  local s = session()
  if not s then
    if done then
      done({ default_mode = nil, matches = {} })
    end
    return
  end
  local record = actions().current_capture()
  local path = record and record.path
  if not path then
    if done then
      done({ default_mode = nil, matches = {} })
    end
    return
  end
  local cached = s.routes
  if type(cached) == "table" and cached.path == path then
    if done then
      done(cached)
    end
    return
  end
  actions()._rpc("routes.resolve", { path = path }, function(result, err)
    local routes = M.normalize_routes(err and nil or result)
    routes.path = path
    if s == session() then
      s.routes = routes
    end
    if done then
      done(routes)
    end
    refresh()
  end, { quiet = true })
end

--- The cached route resolution for the current capture (never fetches).
function M.routes_for(s)
  s = s or session()
  local cached = s and s.routes
  if type(cached) == "table" then
    return cached
  end
  return { default_mode = nil, matches = {} }
end

--- Does `destination` name (or live under) this route's destination?
local function destination_matches(match, target)
  if match.destination == target then
    return true
  end
  if match.is_folder then
    return target:sub(1, #match.destination + 1) == (match.destination .. "/")
  end
  return false
end

--- The route governing `target`, or nil.
function M.route_for(target, s)
  if type(target) ~= "string" or target == "" then
    return nil
  end
  for _, match in ipairs(M.routes_for(s).matches) do
    if destination_matches(match, target) then
      return match
    end
  end
  return nil
end

--- The effective edit mode for `target` (spec 12 §1's three-layer rule, minus
--- the per-invocation top layer, which `dispatch` applies).
---
--- Precedence: an explicit per-invocation `override` → the matching route's
--- `mode` → the core's `[integrate] default_mode` when it reports one →
--- `manual`.
---@return string mode, table|nil route
function M.mode_for(target, override, s)
  local route = M.route_for(target, s)
  if override and vim.tbl_contains(M.MODES, override) then
    return override, route
  end
  if route then
    return route.mode, route
  end
  return M.routes_for(s).default_mode or M.DEFAULT_MODE, nil
end

--- The effective review gate for `target` before a proposal exists. Opting IN
--- is the only move either level can make (ARCHITECTURE "OR-of-auto"), so a
--- route saying `auto` wins; otherwise the spec's `diff`.
function M.review_for(target, s)
  local route = M.route_for(target, s)
  if route and route.review == "auto" then
    return "auto"
  end
  return M.DEFAULT_REVIEW
end

---------------------------------------------------------------------------
-- per-action mode selection (spec 12 §1 "per-invocation choice in the UI")
---------------------------------------------------------------------------

--- One line naming the mode a destination will use and where that came from.
--- This is the "surfaced per-route/per-action" half: every dispatch says
--- which of the three modes ran and why, so an `append` route is visible as
--- an append rather than silently doing nothing interactive.
function M.describe_mode(target, mode, route)
  local origin
  if route then
    origin = ("route: %s"):format(route.route_name or "?")
  elseif M.routes_for().default_mode then
    origin = "core default"
  else
    origin = "default"
  end
  return ("mode: %s (%s) → %s"):format(mode, origin, vim.fn.fnamemodify(target, ":t"))
end

--- `<leader>mm` — choose the edit mode for a destination, this once.
---
--- The picker MARKS the mode that would run anyway, so the per-invocation
--- choice is an override of something visible rather than a blind pick.
---@param target string|nil defaults to the selected item's path
function M.choose_mode(target)
  target = target or M.selected_target()
  if type(target) ~= "string" or target == "" then
    warn("select a note first — the edit mode is chosen per destination (spec 12 §1)")
    return
  end
  local effective, route = M.mode_for(target)
  local items, labels = {}, {}
  for _, mode in ipairs(M.MODES) do
    items[#items + 1] = mode
    local suffix = ""
    if mode == effective then
      suffix = route and (" ← route: " .. (route.route_name or "?")) or " ← default"
    end
    labels[#labels + 1] = mode .. suffix
  end
  vim.ui.select(items, {
    prompt = ("Edit mode for %s"):format(vim.fn.fnamemodify(target, ":t")),
    format_item = function(item)
      for index, mode in ipairs(items) do
        if mode == item then
          return labels[index]
        end
      end
      return item
    end,
  }, function(choice)
    if not choice then
      return
    end
    M.dispatch(target, { mode = choice })
  end)
end

--- The destination under the cursor / selection, for the keymap entry points.
function M.selected_target()
  local item = actions()._cursor_item()
  if type(item) == "table" and item.kind == "file" and present(item.path) then
    return tostring(item.path)
  end
  local entry = actions().selected_entry()
  if type(entry) == "table" and entry.kind ~= "dir" and present(entry.path) then
    return tostring(entry.path)
  end
  return nil
end

--- Run the effective mode for `target` (spec 12 §1).
---
--- This is the ONE place the three modes fan out, so `<CR>` on an `[F]` line,
--- `<leader>mi` and the mode picker can never disagree about what a mode
--- means.
---@param target string
---@param opts table|nil { mode = "manual"|"append"|"integrate", dry_run = boolean }
function M.dispatch(target, opts)
  opts = opts or {}
  if type(target) ~= "string" or target == "" then
    warn("no destination note")
    return
  end
  local mode, route = M.mode_for(target, opts.mode)

  if mode == "append" then
    -- Deterministic and mechanical (12 §1), and NOT a client operation: the
    -- append template belongs to the route pipeline (11 §1), which has no RPC
    -- door. Surfacing it is the honest answer — silently merging instead
    -- would be the 09 §1.5 silently-wrong-answer class.
    notify(
      ("%s — append is the route pipeline's job (`organize routes apply`); press %s to pick another mode"):format(
        M.describe_mode(target, mode, route),
        M.key("integrate_mode")
      )
    )
    return
  end

  if mode == "integrate" then
    notify(M.describe_mode(target, mode, route))
    M.propose(target, { route = route, dry_run = opts.dry_run })
    return
  end

  -- `manual` — Matt edits, in the spec 03 §5 merge editor.
  if opts.mode then
    notify(M.describe_mode(target, "manual", route))
  end
  actions().merge_with(target)
end

--- `<leader>mi` — integrate the capture into the selected note, whatever the
--- route says (the per-invocation choice, spelled as one keystroke).
function M.integrate_selected()
  local target = M.selected_target()
  if not target then
    warn("select a note to integrate into (spec 12 §1)")
    return
  end
  M.dispatch(target, { mode = "integrate" })
end

--- `<leader>mm` bound to the selection.
function M.choose_mode_selected()
  M.choose_mode(M.selected_target())
end

---------------------------------------------------------------------------
-- propose → the review gate
---------------------------------------------------------------------------

--- The lhs currently bound to a keymap row, for message text that cannot go
--- stale when Matt rebinds (spec 03 §2's generated-help rule, applied to
--- prose).
function M.key(name)
  for _, entry in ipairs(actions().keymap_table()) do
    if entry.name == name then
      return entry.lhs
    end
  end
  local defaults = ((cfg() or {}).keymaps or {}).buffer or {}
  return defaults[name] or ("<" .. name .. ">")
end

--- `op.integrate_propose` — ask the core (and, through it, the LLM) for a
--- proposal, then raise the review gate.
---
--- PROPOSE WRITES NOTHING. That is the half a client is most likely to get
--- wrong, so it is stated here as well as asserted in the specs.
---@param target string
---@param opts table|nil { route = table|nil, dry_run = boolean }
function M.propose(target, opts)
  opts = opts or {}
  local s = session()
  local record = actions().current_capture()
  if not (s and record) then
    warn("no capture open")
    return
  end
  if type(target) ~= "string" or target == "" then
    warn("no integrate target")
    return
  end
  if s.integrate then
    warn("a proposal is already under review — accept, edit or reject it first")
    return
  end

  local previous_view = s.view
  s.view = "loading"
  refresh()

  -- The one op.* asymmetry the client already knows from `op.skip`: the
  -- capture param is `note`, not `path`.
  local params = {
    note = record.path,
    target = target,
    route = opts.route and opts.route.route_name or nil,
  }
  if opts.dry_run then
    params.dry_run = true
  end

  actions()._rpc("op.integrate_propose", params, function(result, err)
    if err then
      s.view = previous_view or "suggestions"
      refresh()
      if M.is_method_missing(err) then
        -- Graceful degradation (10 §1): an older core has no integrate at
        -- all. Unlike the passive route probe this was an explicit keystroke,
        -- so it gets ONE clear line naming the fallback rather than silence —
        -- a keypress that does nothing is the 09 §1.5 failure, not the fix.
        warn(
          ("this organize-core has no `integrate` (spec 12 §1) — press %s to merge %s by hand instead"):format(
            M.key("merge"),
            vim.fn.fnamemodify(target, ":t")
          )
        )
      end
      return
    end
    if s ~= session() then
      return -- the session was torn down while the model was thinking
    end
    local proposal = type(result) == "table" and result or {}
    if not present(proposal.diff) then
      warn("the core returned a proposal with no diff — nothing to review")
      s.view = previous_view or "suggestions"
      refresh()
      return
    end

    local review = text_or_nil(proposal.review) or M.review_for(target, s)
    s.integrate = {
      proposal = proposal,
      target = target,
      route = opts.route,
      review = review,
      rationale = text_or_nil(proposal.rationale),
      diff = tostring(proposal.diff),
      editing = false,
      dry_run = opts.dry_run == true,
      previous_view = previous_view or "suggestions",
    }
    s.view = M.GATE_VIEW
    refresh()

    if review == "auto" then
      -- `review = "auto"` is the per-route opt-in of spec 12 §1: apply
      -- without asking. The gate is still RENDERED for the instant it takes,
      -- so an auto route is never invisible.
      M.commit("accepted")
      return
    end

    M.bind_gate()
    ui().focus("organize")
  end)
end

--- Is the review gate up?
function M.is_active(s)
  s = s or session()
  return type(s) == "table" and type(s.integrate) == "table"
end

--- `e` — open the FINAL diff for hand-editing before applying (spec 12 §1).
---
--- What Matt edits is the DIFF, because `final_diff` is what the wire carries
--- and what the record stores: "`proposed_diff` vs `final_diff` turns every
--- reviewed integration into a labeled edit example" (12 §2). The buffer
--- becomes modifiable and holds the diff and nothing else, so a `:w`-shaped
--- reflex cannot write prose into it.
function M.edit()
  local s = session()
  if not M.is_active(s) then
    warn("no proposal under review")
    return
  end
  if s.integrate.editing then
    return
  end
  s.integrate.editing = true
  refresh()
  ui().focus("organize")
end

--- `<CR>` / `<leader>mc` — the accept half of the gate.
---
--- After an edit this necessarily becomes verdict `edited`: the client must
--- never claim `accepted` for bytes that differ from the proposal (the core
--- refuses that combination outright, and the corpus would be wrong even if
--- it did not).
function M.accept()
  local s = session()
  if not M.is_active(s) then
    warn("no proposal under review")
    return
  end
  if not s.integrate.editing then
    M.commit("accepted")
    return
  end
  local final = M.edited_diff(s)
  if final == s.integrate.diff then
    -- Opened the editor, changed nothing: that is an ACCEPT, and recording it
    -- as `edited` with two identical diffs would poison 12 §2's labelled-edit
    -- corpus with a non-edit.
    M.commit("accepted")
    return
  end
  if vim.trim(final) == "" then
    warn("the edited diff is empty — reject instead if you do not want this")
    return
  end
  M.commit("edited", final)
end

--- `<leader>mx` — reject. This COMMITS (record written, no vault byte).
function M.reject()
  local s = session()
  if not M.is_active(s) then
    warn("no proposal under review")
    return
  end
  M.commit("rejected")
end

--- The diff currently in the organize pane (the hand-edited version).
function M.edited_diff(s)
  s = s or session()
  local mod = ui()
  if type(mod.organize_content) == "function" then
    local ok, content = pcall(mod.organize_content)
    if ok and type(content) == "string" then
      return content
    end
  end
  return ((s or {}).integrate or {}).diff or ""
end

--- Close the gate and put the pane back where it was.
local function close_gate(s)
  M.unbind_gate()
  if not s then
    return
  end
  local previous = (s.integrate or {}).previous_view or "suggestions"
  s.integrate = nil
  s.view = previous
  refresh()
end

M._close_gate = close_gate

--- `op.integrate_commit` — the one write path, for all three verdicts.
---
--- The proposal is echoed back VERBATIM, with one transformation that is not
--- optional: every NUMBER in `target_snapshot` is stringified at `%.17g`
--- first. `vim.json.encode` renders Lua numbers at `%.14g`, which drops the
--- last significant digit of the core's float mtime — the exact defect that
--- made `op.merge_commit` fail with ConcurrentModificationError on EVERY
--- merge (see `actions.wire_snapshot`). The core's `float(...)` coercion
--- reconstructs the identical double from the string.
---@param verdict string one of `M.VERDICTS`
---@param final_diff string|nil required for `edited`
function M.commit(verdict, final_diff)
  local s = session()
  if not M.is_active(s) then
    warn("no proposal under review")
    return
  end
  if not vim.tbl_contains(M.VERDICTS, verdict) then
    warn(("%s is not an integrate verdict"):format(tostring(verdict)))
    return
  end
  local gate = s.integrate
  if gate.committing then
    -- A second `<CR>` while the first verdict is still on the wire would
    -- record the same proposal twice — the double-keypress defect
    -- `actions.begin_op` guards the session ops against.
    return
  end
  local target = gate.target

  local params = vim.tbl_extend("force", {
    proposal = M.wire_proposal(gate.proposal),
    verdict = verdict,
  }, actions().decision_context({
    chosen_rank = actions().rank_of(s, vim.fn.fnamemodify(target, ":h")) or actions().NONE,
  }))
  if final_diff ~= nil then
    params.final_diff = final_diff
  end
  if gate.dry_run then
    params.dry_run = true
  end

  -- The gate comes down before the round-trip so a second `<CR>` cannot
  -- commit the same proposal twice (`actions.begin_op` guards the session ops
  -- the same way).
  gate.committing = true
  s.view = "loading"
  refresh()

  actions()._rpc("op.integrate_commit", params, function(result, err)
    if s ~= session() then
      return
    end
    if err then
      -- Nothing was written; put the proposal back so the verdict can be
      -- retried or changed. The error itself was already notified by `rpc`.
      gate.committing = false
      s.view = M.GATE_VIEW
      refresh()
      M.bind_gate()
      return
    end
    if type(result) == "table" and result.ok == false then
      notify(("integrate failed: %s"):format(tostring(result.error or "unknown error")), vim.log.levels.ERROR)
      gate.committing = false
      s.view = M.GATE_VIEW
      refresh()
      M.bind_gate()
      return
    end
    close_gate(s)
    M.report(verdict, target, result, gate)
  end)
end

--- Echo a proposal back exactly as it arrived, minus the server-added
--- `review` (which is not part of `IntegrationProposal`) and with the
--- snapshot's numbers made lossless.
function M.wire_proposal(proposal)
  if type(proposal) ~= "table" then
    return proposal
  end
  local out = {}
  for key, value in pairs(proposal) do
    if key ~= "review" then
      out[key] = value
    end
  end
  if type(out.target_snapshot) == "table" then
    out.target_snapshot = actions().wire_snapshot(out.target_snapshot)
  end
  return out
end

--- What Matt is told after a verdict lands.
---
--- The capture is deliberately NOT archived and NOT marked processed: a
--- standalone integrate archives nothing, because "I may be adding them to
--- multiple files" (12's opening directive) and the commit is stateless, so
--- it cannot know whether another integration is coming. ARCHITECTURE makes
--- the obligation explicit — "the asymmetry must be VISIBLE" — so the notice
--- names the file AND the key that finishes the job.
function M.report(verdict, target, result, gate)
  local dry = (type(result) == "table" and result.dry_run == true) or (gate or {}).dry_run == true
  local prefix = dry and "DRY RUN — " or ""
  local name = vim.fn.fnamemodify(target, ":t")
  if verdict == "rejected" then
    notify(("%sintegration rejected — the verdict was recorded, %s is untouched"):format(prefix, name))
    return
  end
  local record = actions().current_capture()
  local capture_name = record and vim.fn.fnamemodify(record.path, ":t") or "the capture"
  notify(
    ("%s%s into %s — %s is still in the backlog: press %s to archive it, or integrate it elsewhere first"):format(
      prefix,
      verdict == "edited" and "applied your edited diff" or "integrated",
      name,
      capture_name,
      M.key("archive")
    )
  )
end

---------------------------------------------------------------------------
-- the gate's own keymap (spec 12 §1: accept / edit / reject)
---------------------------------------------------------------------------

--- `e` is bound ONLY while a proposal is on screen, and unbound the moment
--- the gate closes — the organize pane's `e` is otherwise plain cursor motion
--- and shadowing it permanently would be a keymap change spec 03 never asked
--- for. Accept (`<CR>` / `<leader>mc`) and reject (`<leader>mx`) need no
--- binding of their own: `actions` dispatches them on `state.view`, the same
--- line-kind-not-line-text discipline `<CR>` already uses.
function M.bind_gate()
  local mod = ui()
  if type(mod.current_bufs) ~= "function" then
    return false
  end
  local buf = (mod.current_bufs() or {}).organize
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return false
  end
  local lhs = M.key("integrate_edit")
  if not lhs or lhs == "" then
    return false
  end
  M._bound = { buf = buf, lhs = lhs }
  pcall(vim.keymap.set, "n", lhs, function()
    M.edit()
  end, {
    buffer = buf,
    nowait = true,
    silent = true,
    desc = "para-organize: Edit the proposed diff (review gate)",
  })
  return true
end

function M.unbind_gate()
  local bound = M._bound
  M._bound = nil
  if not bound then
    return false
  end
  if bound.buf and vim.api.nvim_buf_is_valid(bound.buf) then
    pcall(vim.keymap.del, "n", bound.lhs, { buffer = bound.buf })
  end
  return true
end

---------------------------------------------------------------------------
-- rendering (spec 12 §1: "the nvim client shows the unified diff")
---------------------------------------------------------------------------

--- Standard Vim diff groups. They are not in `ui.highlights` on purpose: that
--- table is a CLOSED schema (config.lua rejects unknown keys), and DiffAdd /
--- DiffDelete / DiffText are defined by every colorscheme, so a diff reads
--- correctly without asking Matt to configure anything.
M.HL_ADD = "DiffAdd"
M.HL_DELETE = "DiffDelete"
M.HL_HUNK = "DiffText"

--- Highlight group for one unified-diff line.
function M.diff_hl(line, hl)
  hl = hl or {}
  if line:sub(1, 3) == "+++" or line:sub(1, 3) == "---" then
    return hl.header or "Title"
  end
  if line:sub(1, 2) == "@@" then
    return M.HL_HUNK
  end
  local first = line:sub(1, 1)
  if first == "+" then
    return M.HL_ADD
  end
  if first == "-" then
    return M.HL_DELETE
  end
  return nil
end

--- The review-gate pane. Pure: same `{lines, marks, map}` builder every other
--- view uses, so the whole gate is assertable without mounting a window.
---
--- While EDITING the pane holds the diff and NOTHING else — the header would
--- otherwise be sent back as part of `final_diff`, which is the spec 03 §5
--- "instructions must never be buffer lines" rule applied to this gate.
function M.render(state, config)
  state = state or {}
  local out = render.new()
  local gate = state.integrate or {}
  local hl = (config and config.ui and config.ui.highlights) or {}
  local diff = tostring(gate.diff or "")

  if gate.editing then
    for _, line in ipairs(vim.split(diff, "\n", { plain = true })) do
      out:line(line)
    end
    if #out.lines == 0 then
      out:line("")
    end
    return out
  end

  local suffix = gate.route and (" · route: " .. (gate.route.route_name or "?")) or ""
  if gate.dry_run then
    suffix = suffix .. " · DRY RUN"
  end
  out:header(("Integrate — review%s"):format(suffix), hl.header)
  out:line(("  target:  %s"):format(gate.target or "?"), hl.reason)
  out:line(("  mode:    integrate · review: %s"):format(gate.review or M.DEFAULT_REVIEW), hl.reason)
  if gate.rationale then
    out:line(("  why:     %s"):format(gate.rationale), hl.reason)
  end
  if gate.dry_run then
    out:line("  DRY RUN — no vault byte will be written", hl.header)
  end
  out:line("")
  out:line(
    ("  %s accept · %s edit · %s reject"):format(
      M.key("accept"),
      M.key("integrate_edit"),
      M.key("merge_cancel")
    ),
    hl.hint or "Comment"
  )
  out:line("")
  for _, line in ipairs(vim.split(diff, "\n", { plain = true })) do
    out:line(line, M.diff_hl(line, hl))
  end
  return out
end

--- The virtual-text hint drawn over the pane while the diff is being edited
--- (never buffer lines — spec 03 §5 / 10 §4).
function M.hint(state, config)
  local gate = (state or {}).integrate or {}
  local _ = config
  return ("INTEGRATE (editing the diff) → %s   %s apply as `edited` · %s reject"):format(
    gate.target or "?",
    M.key("merge_complete"),
    M.key("merge_cancel")
  )
end

---------------------------------------------------------------------------
-- lifecycle
---------------------------------------------------------------------------

--- Drop every trace of a gate (session teardown, and the specs).
function M.reset()
  M.unbind_gate()
  local s = session()
  if type(s) == "table" then
    s.integrate = nil
    s.routes = nil
  end
end

return M
