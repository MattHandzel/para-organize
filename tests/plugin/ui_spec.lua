-- ui_spec — rendering + teardown for the two-pane organize UI (spec 03 §3).
--
-- Everything here runs headless against a THROWAWAY fixture vault under
-- `vim.fn.tempname()`. No real vault, no core process, no RPC: the UI is a
-- pure function of the session state table.

local ui = require("para-organize.ui")
local render = require("para-organize.ui.render")

local function para_bufs()
  local out = {}
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buf) then
      local name = vim.api.nvim_buf_get_name(buf)
      if name:match("para%-organize://") then
        table.insert(out, name)
      end
    end
  end
  return out
end

local function organize_lines()
  local bufs = ui.current_bufs()
  if not bufs.organize then
    return {}
  end
  return vim.api.nvim_buf_get_lines(bufs.organize, 0, -1, false)
end

local function has_line(lines, pattern)
  for _, line in ipairs(lines) do
    if line:match(pattern) then
      return true, line
    end
  end
  return false
end

local function virt_lines(buf)
  local out = {}
  for _, mark in ipairs(vim.api.nvim_buf_get_extmarks(buf, -1, 0, -1, { details = true })) do
    local details = mark[4] or {}
    for _, chunks in ipairs(details.virt_lines or {}) do
      local text = {}
      for _, chunk in ipairs(chunks) do
        table.insert(text, chunk[1])
      end
      table.insert(out, table.concat(text, ""))
    end
  end
  return out
end

describe("para-organize.ui", function()
  local dir, capture_path, second_path, state, baseline_wins

  local function fixture_state()
    return {
      captures = {
        {
          path = capture_path,
          filename = "capture-one.md",
          title = "Capture one",
          para_type = "capture",
          timestamp = "2026-08-15T06:31:00",
          aliases = { "capture-one", "20260815T063100" },
          capture_id = "20260815T063100",
          tags = { "alpha", "beta" },
          sources = { "obsidian" },
          context = { "morning" },
          modalities = { "text" },
          -- The `note.get` frontmatter dict, which is what the spec-15 card
          -- renders from. Four of these keys ship HIDDEN and must not appear
          -- in a compact card; `importance` is a spec-07 metadata field and
          -- is therefore implicitly pinned.
          frontmatter = {
            timestamp = "2026-08-15T06:31:00",
            context = { "morning" },
            tags = { "alpha", "beta" },
            sources = { "obsidian" },
            importance = "high",
            capture_id = "20260815T063100",
            aliases = { "20260815T063100" },
            location = { city = "San Francisco" },
            processing_status = "raw",
          },
        },
        { path = second_path, filename = "capture-two.md", title = "Capture two", tags = {} },
      },
      current = 1,
      processed = 0,
      skipped = 0,
      selected = 1,
      sort = "intelligent",
      view = "suggestions",
      suggestions = {
        { path = "/vault/projects/alpha", name = "alpha", type = "projects", score = 2.4, reasons = { "tag match: alpha" } },
        { path = "/vault/areas/health", name = "health", type = "areas", score = 1.1, reasons = {} },
        { path = "/vault/resources/misc", name = "misc", type = "resources", score = 0.4 },
        { path = "/vault/archive", name = "archive", type = "archives", score = 0.1 },
      },
      meta_fields = {
        { key = "tags", type = "list", keymap = "t", prompt = "Add tag(s)" },
        { key = "importance", type = "enum", keymap = "i", values = { "high", "medium", "low" } },
      },
    }
  end

  before_each(function()
    ui.unmount()
    baseline_wins = #vim.api.nvim_list_wins()
    dir = vim.fn.tempname()
    vim.fn.mkdir(dir .. "/capture", "p")
    capture_path = dir .. "/capture/capture-one.md"
    second_path = dir .. "/capture/capture-two.md"
    vim.fn.writefile({ "---", "tags:", "  - alpha", "---", "", "# Capture one", "body line" }, capture_path)
    vim.fn.writefile({ "# Capture two" }, second_path)
    ui.setup({ ui = { close_on_complete = false } })
    state = fixture_state()
  end)

  after_each(function()
    ui.unmount()
    pcall(vim.fn.delete, dir, "rf")
  end)

  describe("pure rendering", function()
    it("uses the spec-03 type letters", function()
      assert.are.equal("[P]", render.type_marker("projects"))
      assert.are.equal("[A]", render.type_marker("areas"))
      assert.are.equal("[R]", render.type_marker("resources"))
      assert.are.equal("[🗑]", render.type_marker("archives"))
      assert.are.equal("[D]", render.type_marker("dir"))
      assert.are.equal("[F]", render.type_marker("file"))
    end)

    it("buckets scores at 2.0 / 1.0 per spec 03 §3", function()
      local hl = { score_high = "H", score_medium = "M", score_low = "L" }
      assert.are.equal("H", render.score_hl(2.0, hl))
      assert.are.equal("M", render.score_hl(1.99, hl))
      assert.are.equal("M", render.score_hl(1.0, hl))
      assert.are.equal("L", render.score_hl(0.99, hl))
    end)

    it("labels the three sort modes", function()
      assert.are.same({ "alphabetical", "modified", "intelligent" }, render.SORT_MODES)
      assert.are.equal("Intelligent Suggestions", render.SORT_LABELS.intelligent)
      assert.are.equal("Last Modified", render.SORT_LABELS.modified)
    end)

    it("renders route-sourced suggestions distinctly (spec 11 §1)", function()
      local s = fixture_state()
      s.suggestions[1].route = "work"
      local rendered = render.suggestions(s, ui.config())
      assert.is_true((has_line(rendered.lines, "route: work")))
    end)

    it("counts processed/skipped whether they are counters or lists", function()
      assert.are.equal(3, render.count(3))
      assert.are.equal(2, render.count({ "a", "b" }))
      assert.are.equal(0, render.count(nil))
    end)
  end)

  describe("mount", function()
    it("opens two panes; the left one IS the real capture file buffer", function()
      assert.is_truthy(ui.mount(state, { bind = false }))
      local bufs = ui.current_bufs()
      local wins = ui.current_wins()

      assert.is_true(vim.api.nvim_win_is_valid(wins.capture))
      assert.is_true(vim.api.nvim_win_is_valid(wins.organize))
      -- spec 10 §4: the capture pane is the vault file's own buffer, so
      -- `:w` writes the real note and the plugin never has to.
      assert.are.equal(vim.fn.fnamemodify(capture_path, ":p"), vim.api.nvim_buf_get_name(bufs.capture))
      assert.is_truthy(vim.api.nvim_buf_get_name(bufs.organize):match("para%-organize://organize$"))
      assert.are.equal("", vim.bo[bufs.capture].buftype)
      assert.are.equal("nofile", vim.bo[bufs.organize].buftype)
    end)

    it("never writes header text INTO the capture buffer", function()
      ui.mount(state, { bind = false })
      local bufs = ui.current_bufs()
      -- The file's bytes are untouched...
      assert.are.same(
        { "---", "tags:", "  - alpha", "---", "", "# Capture one", "body line" },
        vim.api.nvim_buf_get_lines(bufs.capture, 0, -1, false)
      )
      assert.is_false(vim.bo[bufs.capture].modified)
      -- ...and the header exists only as virtual text.
      local virtuals = virt_lines(bufs.capture)
      assert.is_true((has_line(virtuals, "^ Capture 1 of 2$")))
      -- `tags` is BOTH a shipped pinned key and a spec-07 metadata field, so
      -- it renders once, with its edit keymap as a dim suffix.
      assert.is_true((has_line(virtuals, "^ tags%s+#alpha #beta%s+%(t%)$")))
      assert.is_true((has_line(virtuals, "^ sources%s+obsidian$")))
      assert.is_true((has_line(virtuals, "^ context%s+morning$")))
      -- The shipped `hidden` list (spec 15 §7): the four spellings of the
      -- capture's identity never reach a compact card.
      for _, gone in ipairs({ "capture_id", "aliases", "location", "processing_status" }) do
        assert.is_false((has_line(virtuals, "^ " .. gone)), gone)
      end
      -- The spec-07 metadata field is implicitly pinned, and renders its edit
      -- keymap as a dim suffix (spec 15 §2).
      assert.is_true((has_line(virtuals, "^ importance high%s+%(i%)$")))
    end)

    it("renders the suggestions list with markers, scores and reasons", function()
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^Suggestions — sort: Intelligent Suggestions$")))
      assert.is_true((has_line(lines, "^%[P%] alpha%s+2%.40$")))
      assert.is_true((has_line(lines, "^%[A%] health%s+1%.10$")))
      assert.is_true((has_line(lines, "^%[🗑%] archive")))
      assert.is_true((has_line(lines, "^    tag match: alpha$")))
    end)

    -- `ui.display.*` was DELETED as a section by spec 15 §2 / ruling R1; the
    -- key's new home is `ui.organize.show_scores` (spec 15 §6), which is now
    -- the SINGLE source the pane and the pickers both read.
    it("hides scores when ui.organize.show_scores is off", function()
      ui.setup({ ui = { organize = { show_scores = false } } })
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^%[P%] alpha$")))
    end)

    -- Reported from the live install: with the common markdown setup
    -- (`foldmethod=expr` + a treesitter `foldexpr`) BOTH panes opened folded.
    -- `foldexpr = "1"` reproduces that without treesitter: every line sits in
    -- a level-1 fold, and `foldlevel = 0` starts it closed.
    --- Observe both windows under hostile fold globals, restoring the globals
    --- BEFORE any assertion so a failure cannot leak them into the suite.
    local function observe_folds(setup_opts)
      local saved = {
        foldmethod = vim.o.foldmethod,
        foldexpr = vim.o.foldexpr,
        foldenable = vim.o.foldenable,
        foldlevel = vim.o.foldlevel,
      }
      vim.o.foldmethod = "expr"
      vim.o.foldexpr = "1"
      vim.o.foldenable = true
      vim.o.foldlevel = 0

      if setup_opts then
        ui.setup(setup_opts)
      end
      ui.mount(state, { bind = false })
      local wins = ui.current_wins()
      local observed = {}
      for _, pane in ipairs({ "capture", "organize" }) do
        local win = wins[pane]
        observed[pane] = {
          foldenable = vim.wo[win].foldenable,
          foldmethod = vim.wo[win].foldmethod,
          first_closed_fold = vim.api.nvim_win_call(win, function()
            return vim.fn.foldclosed(1)
          end),
          -- The fixture capture is `---` / `tags:` / `  - alpha` / `---`, so
          -- line 5 is the first BODY line.
          body_closed_fold = vim.api.nvim_win_call(win, function()
            return vim.fn.foldclosed(5)
          end),
        }
      end

      for name, value in pairs(saved) do
        vim.o[name] = value
      end
      return observed
    end

    -- ⚠ RULING R14: the assertions are made PER WINDOW, because the two
    -- windows are not in the same state and never were. A blanket
    -- `foldclosed(1) == -1` in BOTH windows is unsatisfiable against spec 15
    -- §3 and an implementer would discharge it by deleting the frontmatter
    -- fold — which reopens Matt item 5's sibling and empties spec 15 §2.
    it("opens the organize pane unfolded even when the user's globals fold everything", function()
      local observed = observe_folds(nil)
      assert.is_false(observed.organize.foldenable)
      -- Not just `foldenable=false`: manual defeats a later `zx` too.
      assert.are.equal("manual", observed.organize.foldmethod)
      -- The symptom Matt actually saw: line 1 swallowed by a closed fold.
      assert.are.equal(-1, observed.organize.first_closed_fold)
    end)

    it("leaves exactly ONE closed fold in the capture pane: the plugin's frontmatter fold", function()
      local observed = observe_folds(nil)
      assert.are.equal("manual", observed.capture.foldmethod)
      assert.are.equal(1, observed.capture.first_closed_fold)
      -- …and it is the ONLY closed one: the body is open.
      assert.are.equal(-1, observed.capture.body_closed_fold)
    end)

    it("creates no fold at all under the ui.capture.frontmatter = \"none\" control", function()
      local observed = observe_folds({ ui = { close_on_complete = false, capture = { frontmatter = "none" } } })
      assert.are.equal("manual", observed.capture.foldmethod)
      assert.are.equal(-1, observed.capture.first_closed_fold)
    end)

    it("honors ui.win_options, the way back to the user's own fold settings", function()
      ui.setup({ ui = { win_options = { foldenable = true, wrap = false } } })
      ui.mount(state, { bind = false })
      local wins = ui.current_wins()
      assert.is_true(vim.wo[wins.capture].foldenable)
      assert.is_false(vim.wo[wins.capture].wrap)
      assert.is_true(vim.wo[wins.organize].foldenable)
      -- A pane default the user did NOT override still applies — and this is
      -- the must-not-be-connected half of spec 15 §10.2: opting back into
      -- folds must NEVER reconnect the user's `foldexpr` method.
      assert.are.equal("manual", vim.wo[wins.capture].foldmethod)
      assert.are.equal("manual", vim.wo[wins.organize].foldmethod)
    end)
  end)

  describe("UI states (spec 03 §3)", function()
    it("renders the loading state", function()
      state.view = "loading"
      ui.mount(state, { bind = false })
      assert.is_true((has_line(organize_lines(), "Loading")))
    end)

    it("renders the session-complete notice with counts", function()
      state.view = "empty"
      state.processed = 3
      state.skipped = 1
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^Session complete$")))
      assert.is_true((has_line(lines, "Processed: 3")))
      assert.is_true((has_line(lines, "Skipped:   1")))
    end)

    it("renders the no-captures notice when nothing matched the filters", function()
      local empty = { captures = {}, current = 1, view = "empty" }
      ui.mount(empty, { bind = false })
      assert.is_true((has_line(organize_lines(), "No captures found matching filters")))
    end)

    it("renders the SQ-1 archive-only state sensibly (zero-signal capture)", function()
      -- Since the SQ-1 ruling, min_confidence applies to the SIGNAL score, so
      -- a zero-signal capture yields exactly ONE suggestion — the safe
      -- archive default. This is the entry EXACTLY as the wire delivers it:
      -- `description`/`route` are JSON null, i.e. vim.NIL — which is TRUTHY,
      -- the trap that once rendered "→ route: vim.NIL" on the line.
      state.preview = true
      state.suggestions = {
        {
          name = "Archive Now",
          path = "/vault/archive/capture/raw_capture",
          type = "archive",
          score = 0.1,
          reasons = { "Safe default option" },
          description = vim.NIL,
          route = vim.NIL,
        },
      }
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^%[🗑%] Archive Now%s+0%.10$")))
      assert.is_true((has_line(lines, "^    Safe default option$")))
      assert.is_true((has_line(lines, "┊ %(no description%)")))
      for _, line in ipairs(lines) do
        assert.is_nil(line:find("vim.NIL", 1, true), line)
      end
    end)

    it("renders the directory browse view", function()
      state.view = "browse"
      state.sort = "alphabetical"
      state.browse = {
        path = "/vault/projects",
        label = "projects",
        stack = {},
        entries = {
          { kind = "dir", path = "/vault/projects/alpha", name = "alpha", display = "alpha" },
          { kind = "file", path = "/vault/projects/note.md", name = "note.md", display = "Note alias" },
        },
      }
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^Browse: projects — sort: Alphabetical$")))
      assert.is_true((has_line(lines, "^%[D%] alpha$")))
      assert.is_true((has_line(lines, "^%[F%] Note alias$")))
    end)

    it("renders search results as [F] lines so <CR> starts a merge", function()
      state.view = "search"
      state.search = { query = "alpha", results = { { path = "/vault/projects/a.md", title = "Alpha note", folder = "alpha" } } }
      ui.mount(state, { bind = false })
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^Search: alpha %(1%)$")))
      assert.is_true((has_line(lines, "^%[F%] Alpha note")))
    end)

    it("renders the merge editor with instructions as virtual text only", function()
      state.view = "merge"
      state.merge = {
        target = "/vault/projects/alpha/target.md",
        content = "---\ntitle: target\n---\n\nbody\n\n## Merged from capture-one.md on 2026-08-15 06:31\n\ncapture body",
        snapshot = { mtime = 1, hash = "abc" },
        previous_view = "suggestions",
      }
      ui.mount(state, { bind = false })
      local bufs = ui.current_bufs()
      local lines = organize_lines()
      assert.is_true((has_line(lines, "^## Merged from capture%-one%.md on 2026%-08%-15 06:31$")))
      assert.is_true((has_line(lines, "^capture body$")))
      -- The buffer is editable (Matt edits freely, spec 03 §5 step 2)…
      assert.is_true(vim.bo[bufs.organize].modifiable)
      -- …and no instruction text is in the buffer that op.merge_commit reads.
      assert.is_false((has_line(lines, "leader")))
      assert.is_true((has_line(virt_lines(bufs.organize), "complete")))
      assert.are.equal(state.merge.content, ui.merge_content(state))
    end)

    -- Reported from the live install: editing a merge and pressing `:w` — the
    -- one key every Vim user reaches for — failed with "E382: Cannot write,
    -- 'buftype' option is set", because the pane was `nofile`.
    it("lets :w commit the merge instead of failing E382", function()
      state.view = "merge"
      state.merge = {
        target = "/vault/projects/alpha/target.md",
        content = "body\n\ncapture body",
        snapshot = { mtime = 1, hash = "abc" },
        previous_view = "suggestions",
      }
      ui.mount(state, { bind = false })
      local bufs = ui.current_bufs()

      -- The pane accepts a write at all…
      assert.are.equal("acwrite", vim.bo[bufs.organize].buftype)

      -- …and the write is routed to the merge commit, not to the filesystem.
      local actions = require("para-organize.actions")
      local committed = 0
      local real_complete = actions.merge_complete
      actions.merge_complete = function()
        committed = committed + 1
      end
      local ok, err = pcall(function()
        vim.api.nvim_buf_call(bufs.organize, function()
          vim.cmd("write")
        end)
      end)
      actions.merge_complete = real_complete

      assert.is_true(ok, "«:w» errored in the merge editor: " .. tostring(err))
      assert.are.equal(1, committed)
      -- Nothing was written to disk: the pane has no file name to write to,
      -- and the core is what touches the vault (thin-client law, 10 §1).
      assert.are.equal("para-organize://organize", vim.api.nvim_buf_get_name(bufs.organize))
      assert.is_false(vim.bo[bufs.organize].modified)
    end)

    it("keeps the pane nofile — and unwritable — outside the editable views", function()
      state.view = "suggestions"
      ui.mount(state, { bind = false })
      local bufs = ui.current_bufs()
      assert.are.equal("nofile", vim.bo[bufs.organize].buftype)
      assert.is_false(vim.bo[bufs.organize].modifiable)
    end)

    it("shows a help overlay generated from the keymap table", function()
      local actions = require("para-organize.actions")
      actions.setup({ state = state, config = ui.config() })
      ui.mount(state, { bind = false })
      ui.show_help(actions.keymap_table())
      local bufs = ui.current_bufs()
      assert.is_truthy(bufs.help)
      local lines = vim.api.nvim_buf_get_lines(bufs.help, 0, -1, false)
      assert.is_true((has_line(lines, "Cycle sort mode")))
      assert.is_true((has_line(lines, "Skip capture")))
      assert.is_true((has_line(lines, "^Metadata$")))
      assert.is_true((has_line(lines, "Set tags %(list%)")))
      ui.close_help()
      assert.is_nil(ui.current_bufs().help)
      actions.reset()
    end)
  end)

  describe("refresh", function()
    it("swaps the left pane to the next capture's real buffer", function()
      ui.mount(state, { bind = false })
      state.current = 2
      ui.refresh(state)
      assert.are.equal(vim.fn.fnamemodify(second_path, ":p"), vim.api.nvim_buf_get_name(ui.current_bufs().capture))
      assert.is_true((has_line(virt_lines(ui.current_bufs().capture), "^ Capture 2 of 2$")))
    end)

    it("moves the cursor to the selected suggestion", function()
      ui.mount(state, { bind = false })
      state.selected = 3
      ui.refresh(state)
      local row = vim.api.nvim_win_get_cursor(ui.current_wins().organize)[1]
      local line = organize_lines()[row]
      assert.is_truthy(line:match("^%[R%] misc"))
      local item = ui.item_at(row)
      assert.are.equal(3, item.index)
      assert.are.equal("/vault/resources/misc", item.path)
    end)

    it("is a safe no-op when nothing is mounted", function()
      assert.is_false(ui.refresh(state))
    end)
  end)

  describe("teardown (spec 09 §2)", function()
    it("leaves zero para-organize buffers, windows or autocmds", function()
      ui.mount(state, { bind = false })
      ui.show_help({ { lhs = "?", desc = "Help popup", panes = { "organize" } } })
      assert.is_true(#para_bufs() > 0)

      assert.is_true(ui.unmount())

      assert.are.same({}, para_bufs())
      assert.is_false(ui.is_mounted())
      assert.are.equal(baseline_wins, #vim.api.nvim_list_wins())
      local ok = pcall(vim.api.nvim_get_autocmds, { group = "ParaOrganizeUI" })
      assert.is_false(ok)
    end)

    it("tears down when either window is closed (WinClosed)", function()
      ui.mount(state, { bind = false })
      local wins = ui.current_wins()
      vim.api.nvim_win_close(wins.organize, true)
      vim.wait(500, function()
        return not ui.is_mounted()
      end, 10)
      assert.is_false(ui.is_mounted())
      assert.are.same({}, para_bufs())
      assert.are.equal(baseline_wins, #vim.api.nvim_list_wins())
    end)

    it("keeps a capture buffer that has unsaved edits", function()
      ui.mount(state, { bind = false })
      local capture_buf = ui.current_bufs().capture
      vim.api.nvim_buf_set_lines(capture_buf, 0, 0, false, { "unsaved edit" })
      ui.unmount()
      assert.is_true(vim.api.nvim_buf_is_valid(capture_buf))
      assert.is_true(vim.bo[capture_buf].modified)
      vim.bo[capture_buf].modified = false
      vim.api.nvim_buf_delete(capture_buf, { force = true })
    end)

    it("is idempotent", function()
      ui.mount(state, { bind = false })
      assert.is_true(ui.unmount())
      assert.is_false(ui.unmount())
    end)
  end)
end)
