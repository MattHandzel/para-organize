-- capture_presentation_spec — spec 15's test obligations (§10), the field
-- policy (§2), the rendering model (§3), the formatters (§4), the two
-- renderer seams (§5/§6) and the honored-key enumeration (§10.15).
--
-- Everything runs headless against a THROWAWAY fixture vault under
-- `vim.fn.tempname()`. No core process, no RPC: the presentation layer is a
-- pure function of the session state table plus the merged config.
--
-- ⚠ THE LOAD-BEARING CLAIM OF THIS FILE (spec 10 §4 + 15 §3): the capture
-- buffer is the REAL note. Every artifact this plugin draws around it is an
-- extmark or a foldtext, and §10.3 proves it at the BYTE level — mount,
-- render in all three modes, `:w`, and the file's bytes are unchanged.

local ui = require("para-organize.ui")
local fields = require("para-organize.ui.fields")
local render = require("para-organize.ui.render")
local config = require("para-organize.config")

local NS_CAPTURE = vim.api.nvim_create_namespace("para_organize_capture")

---------------------------------------------------------------------------
-- helpers
---------------------------------------------------------------------------

--- The card, as plain strings — one per virtual line, chunks concatenated.
--- Used to assert CONTENT; §10.12 separately asserts the card is on SCREEN,
--- which is a different claim and a different failure.
local function card(buf)
  buf = buf or ui.current_bufs().capture
  local out = {}
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return out
  end
  for _, mark in ipairs(vim.api.nvim_buf_get_extmarks(buf, NS_CAPTURE, 0, -1, { details = true })) do
    for _, chunks in ipairs((mark[4] or {}).virt_lines or {}) do
      local text = {}
      for _, chunk in ipairs(chunks) do
        text[#text + 1] = chunk[1]
      end
      out[#out + 1] = table.concat(text, "")
    end
  end
  return out
end

local function joined(lines)
  return table.concat(lines or {}, "\n")
end

local function has(lines, pattern)
  for _, line in ipairs(lines) do
    if line:match(pattern) then
      return true, line
    end
  end
  return false
end

--- The label of a FIELD row, or nil for the position line, the REST collapse
--- line and the dim notices — none of which is a field row.
local function row_label(line)
  if line:match("^ Capture %d+ of %d+$") or line:match("^ %+ ") or line:match("^ %(") or line:match("^ ⚠") or line:match("^ %-%- ") then
    return nil
  end
  return line:match("^ (%S+) +%S")
end

--- The row for `key`, without its label padding — so a test can assert the
--- rendered VALUE as a literal without pinning the column arithmetic that
--- §2's layout rule already owns.
local function value_of(lines, key)
  for _, line in ipairs(lines) do
    if row_label(line) == key then
      local rest = line:match("^ %S+ +(.*)$")
      return (rest:gsub("%s+$", ""))
    end
  end
  return nil
end

local function read_bytes(path)
  local fh = assert(io.open(path, "rb"))
  local data = fh:read("*a")
  fh:close()
  return data
end

local function fold_closed(win, line)
  return vim.api.nvim_win_call(win, function()
    return vim.fn.foldclosed(line)
  end)
end

local function buf_binds(buf, lhs)
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(buf, "n")) do
    if map.lhs == lhs then
      return true
    end
  end
  return false
end

--- Count `fields.notify` calls for the duration of `fn`. The module exposes
--- `notify` precisely so an exact count is assertable (§10.7, §10.8).
local function counting_notifies(fn)
  local original = fields.notify
  local seen = {}
  fields.notify = function(msg, level)
    seen[#seen + 1] = { msg = msg, level = level }
  end
  local ok, err = pcall(fn, seen)
  fields.notify = original
  if not ok then
    error(err, 0)
  end
  return seen
end

describe("spec 15 — capture presentation", function()
  local dir, capture_path, broken_path, plain_path

  -- Nine keys: four pinned present (timestamp, context, tags, sources), four
  -- hidden (location, processing_status, capture_id, aliases) and ONE rest
  -- (source_url) — so `+ 1 more` is the correct collapse line and a `+ 5 more`
  -- would prove hidden keys leaked into REST.
  local function frontmatter()
    return {
      timestamp = "2026-08-15T19:45:00",
      context = { "morning" },
      tags = { "impro", "blog-idea" },
      sources = { "obsidian" },
      source_url = "https://example.invalid/x",
      location = { city = "San Francisco", country = "United States" },
      processing_status = "raw",
      capture_id = "20260815T194500",
      aliases = { "20260815T194500" },
    }
  end

  local CAPTURE_FILE = {
    "---",
    "timestamp: '2026-08-15T19:45:00'",
    "context:",
    "  - morning",
    "tags:",
    "  - impro",
    "  - blog-idea",
    "sources:",
    "  - obsidian",
    "source_url: https://example.invalid/x",
    "location:",
    "  city: San Francisco",
    "  country: United States",
    "processing_status: raw",
    "capture_id: '20260815T194500'",
    "aliases:",
    "  - '20260815T194500'",
    "---",
    "## Content",
    "I realized that I think I need to actually try and do recruiting.",
    "",
    "A second body line.",
  }
  -- The `---` that closes the block; line 19 is therefore the first BODY line.
  local CLOSE_LINE = 18
  local FIRST_BODY_LINE = 19

  local function state_for(path, overrides)
    local st = {
      captures = { { path = path, filename = vim.fn.fnamemodify(path, ":t"), frontmatter = frontmatter() } },
      current = 1,
      selected = 1,
      processed = 0,
      skipped = 0,
      sort = "intelligent",
      view = "suggestions",
      suggestions = {
        {
          path = "/vault/projects/alpha",
          name = "alpha",
          type = "projects",
          score = 2.4,
          reasons = { "tag match: impro", "route rule", "recent sibling" },
        },
      },
      meta_fields = {},
    }
    return vim.tbl_deep_extend("force", st, overrides or {})
  end

  --- Mount with `cfg` merged over the test baseline. Returns the state table,
  --- which the caller mutates and hands back to `ui.refresh`.
  local function mount(cfg, st)
    ui.setup(vim.tbl_deep_extend("force", { ui = { close_on_complete = false } }, cfg or {}))
    st = st or state_for(capture_path)
    ui.mount(st, { bind = false })
    return st
  end

  before_each(function()
    ui.unmount()
    config.reset()
    fields.reset_session()
    dir = vim.fn.tempname()
    vim.fn.mkdir(dir .. "/capture", "p")
    capture_path = dir .. "/capture/capture-one.md"
    broken_path = dir .. "/capture/broken.md"
    plain_path = dir .. "/capture/plain.md"
    vim.fn.writefile(CAPTURE_FILE, capture_path)
    -- Delimited, but the YAML inside is malformed: the "no fold" assertion of
    -- §10.6 is then about `parse_error`, not about a missing delimiter.
    vim.fn.writefile({ "---", "tags: [a, b", 'context: "unterminated', "---", "broken body" }, broken_path)
    vim.fn.writefile({ "# No frontmatter at all", "body" }, plain_path)
  end)

  after_each(function()
    ui.unmount()
    config.reset()
    fields.notify = fields.notify -- (restored by counting_notifies; kept explicit)
    pcall(vim.fn.delete, dir, "rf")
  end)

  -------------------------------------------------------------------------
  -- §2 — the field policy, and §9 acceptance 3
  -------------------------------------------------------------------------

  describe("field policy (§2)", function()
    it("compact shows the pinned rows, collapses REST and drops HIDDEN entirely", function()
      mount()
      local lines = card()

      -- Position line first, then the pinned keys IN THE DECLARED ORDER.
      assert.are.equal(" Capture 1 of 1", lines[1])
      local order = {}
      for _, line in ipairs(lines) do
        local label = row_label(line)
        if label then
          order[#order + 1] = label
        end
      end
      -- `title` and `summary` are pinned but absent, and `show_empty_pinned`
      -- ships FALSE (ruling R15), so they are omitted rather than padded.
      assert.are.same({ "timestamp", "context", "tags", "sources" }, order)

      -- ONE rest key. `+ 5 more` here would mean hidden keys leaked in.
      assert.is_true((has(lines, "^ %+ 1 more: source_url · zi$")))
      for _, hidden in ipairs({ "location", "processing_status", "capture_id", "aliases" }) do
        assert.is_nil(value_of(lines, hidden), hidden)
        assert.is_nil(joined(lines):match(hidden), hidden)
      end
    end)

    it("full shows every key, hidden ones included; raw shows no rows at all", function()
      mount()
      ui.cycle_fields()
      assert.are.equal("full", ui.field_mode())
      local full = card()
      assert.is_truthy(value_of(full, "processing_status"))
      assert.is_truthy(value_of(full, "location"))
      assert.is_truthy(value_of(full, "source_url"))
      -- The collapse line belongs to compact only.
      assert.is_false((has(full, "more: source_url")))

      ui.cycle_fields()
      assert.are.equal("raw", ui.field_mode())
      local raw = card()
      assert.is_nil(value_of(raw, "timestamp"))
      assert.is_nil(value_of(raw, "context"))
      -- …and `raw` OPENS the frontmatter fold, because in `raw` the metadata
      -- IS the buffer text (§2).
      assert.are.equal(-1, fold_closed(ui.current_wins().capture, 1))

      -- Cycling out of `raw` re-closes the fold the PLUGIN opened.
      ui.cycle_fields()
      assert.are.equal("compact", ui.field_mode())
      assert.are.equal(1, fold_closed(ui.current_wins().capture, 1))
    end)

    it("show_empty_pinned = true restores the constant shape with `—` rows", function()
      local lines = (function()
        mount({ ui = { capture = { fields = { show_empty_pinned = true } } } })
        return card()
      end)()
      assert.are.equal("—", value_of(lines, "title"))
      assert.are.equal("—", value_of(lines, "summary"))
      -- Six rows now, not four.
      local rows = 0
      for _, line in ipairs(lines) do
        if row_label(line) then
          rows = rows + 1
        end
      end
      assert.are.equal(6, rows)
    end)

    it("labels are display-only: they rename the row, never the policy", function()
      mount({
        ui = {
          capture = {
            fields = { labels = { timestamp = "When", sources = "From" } },
          },
        },
      })
      local lines = card()
      assert.is_truthy(value_of(lines, "When"))
      assert.is_nil(value_of(lines, "timestamp"))
      -- `pinned`/`hidden` still name the FRONTMATTER key, so the labelled row
      -- keeps its pinned position and the collapse line is unchanged.
      assert.is_true((has(lines, "^ %+ 1 more: source_url · zi$")))
    end)

    it("pads labels to the widest label IN THIS CARD, capped at 20 columns", function()
      mount({
        ui = {
          capture = {
            fields = {
              pinned = { "timestamp", "context" },
              labels = { context = "a-label-that-is-far-wider-than-the-cap" },
            },
          },
        },
      })
      local lines = card()
      -- The over-wide label is NOT truncated; it pushes its own value one
      -- column past the cap for that row alone (§2).
      assert.is_true((has(lines, "^ a%-label%-that%-is%-far%-wider%-than%-the%-cap morning$")))
      -- Every other label is padded to the 20-column cap, not to 38.
      assert.is_true((has(lines, "^ timestamp" .. string.rep(" ", 12) .. "%S")))
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.3 — the byte-identity tripwire (spec 10 §4's whole point)
  -------------------------------------------------------------------------

  it("§10.3 — mount, render in all three modes and `:w` change ZERO bytes on disk", function()
    local before = read_bytes(capture_path)
    local disk_lines = #CAPTURE_FILE

    local st = mount()
    for _ = 1, 3 do
      ui.cycle_fields()
      ui.refresh(st)
    end
    assert.are.equal("compact", ui.field_mode())

    local bufs, wins = ui.current_bufs(), ui.current_wins()
    vim.api.nvim_win_call(wins.capture, function()
      vim.cmd("silent write")
    end)

    assert.are.equal(before, read_bytes(capture_path))
    assert.are.equal(disk_lines, vim.api.nvim_buf_line_count(bufs.capture))
    assert.are.equal(disk_lines, #vim.fn.readfile(capture_path))
    -- And the card is still there — the write did not cost the rendering.
    assert.is_true(#card(bufs.capture) > 0)
  end)

  it("§10.3 — a REAL user edit writes exactly that edit, and nothing the plugin drew", function()
    local st = mount()
    local bufs, wins = ui.current_bufs(), ui.current_wins()
    vim.api.nvim_buf_set_lines(bufs.capture, FIRST_BODY_LINE, FIRST_BODY_LINE, false, { "a line Matt typed" })
    ui.refresh(st)
    vim.api.nvim_win_call(wins.capture, function()
      vim.cmd("silent write")
    end)

    local written = vim.fn.readfile(capture_path)
    local expected = vim.deepcopy(CAPTURE_FILE)
    table.insert(expected, FIRST_BODY_LINE + 1, "a line Matt typed")
    assert.are.same(expected, written)
    -- Nothing the card, the foldtext or the mode hint says reached the file.
    local text = joined(written)
    for _, artifact in ipairs({ "Capture 1 of", "▸ frontmatter", "+ 1 more", "-- raw --", "cycles, zo opens" }) do
      assert.is_nil(text:find(artifact, 1, true), artifact)
    end
  end)

  -------------------------------------------------------------------------
  -- §3 — the rendering model: anchoring, the fold, the failure table
  -------------------------------------------------------------------------

  describe("rendering model (§3)", function()
    it("anchors the card AFTER the frontmatter close delimiter, never at (0,0)", function()
      mount()
      local marks = vim.api.nvim_buf_get_extmarks(ui.current_bufs().capture, NS_CAPTURE, 0, -1, { details = true })
      assert.are.equal(1, #marks)
      -- 0-indexed row of the FIRST BODY line…
      assert.are.equal(FIRST_BODY_LINE - 1, marks[1][2])
      -- …with the card drawn above it, i.e. BELOW the closed fold line.
      assert.is_true(marks[1][4].virt_lines_above)
    end)

    it("anchors to line 1 with a window fill when there is no frontmatter", function()
      mount(nil, state_for(plain_path))
      local marks = vim.api.nvim_buf_get_extmarks(ui.current_bufs().capture, NS_CAPTURE, 0, -1, { details = true })
      assert.are.equal(0, marks[1][2])
      assert.is_true(marks[1][4].virt_lines_above)
      -- ⚠ Ruling R13: `topfill` is 0 after mount, so a virt_lines block above
      -- the topmost line is INVISIBLE unless the fill is established.
      local fill = vim.api.nvim_win_call(ui.current_wins().capture, function()
        return vim.fn.winsaveview().topfill
      end)
      assert.are.equal(#marks[1][4].virt_lines, fill)
      assert.is_true(fill > 0)
    end)

    it("frontmatter = \"none\" leaves the YAML visible and moves the card to line 1", function()
      mount({ ui = { capture = { frontmatter = "none" } } })
      local marks = vim.api.nvim_buf_get_extmarks(ui.current_bufs().capture, NS_CAPTURE, 0, -1, { details = true })
      assert.are.equal(0, marks[1][2])
      assert.are.equal(-1, fold_closed(ui.current_wins().capture, 1))
    end)

    it("the foldtext names the LIVE binding and the payload's field count", function()
      mount()
      local text = vim.api.nvim_win_call(ui.current_wins().capture, function()
        vim.v.foldstart = 1
        return ui.foldtext()
      end)
      assert.are.equal("▸ frontmatter (9 fields) — zi cycles, zo opens", text)
    end)

    it("a rebound cycle_fields is read back VERBATIM in the foldtext and the hint", function()
      config.setup({ keymaps = { buffer = { cycle_fields = "<leader>tf" } } })
      mount()
      local text = vim.api.nvim_win_call(ui.current_wins().capture, function()
        return ui.foldtext()
      end)
      assert.are.equal("▸ frontmatter (9 fields) — <leader>tf cycles, zo opens", text)
      assert.is_true((has(card(), "^ %+ 1 more: source_url · <leader>tf$")))
      -- must-not-be-connected: no hardcoded `zi` survives anywhere on screen.
      assert.is_nil(joined(card()):find("· zi", 1, true))
    end)

    it("a custom ui.capture.foldtext is honored, and a raising one falls back", function()
      mount({ ui = { capture = { foldtext = "{count} keys ({cycle_fields})" } } })
      assert.are.equal(
        "9 keys (zi)",
        vim.api.nvim_win_call(ui.current_wins().capture, function()
          return ui.foldtext()
        end)
      )
      ui.unmount()
      mount({
        ui = {
          capture = {
            foldtext = function()
              error("boom")
            end,
          },
        },
      })
      assert.are.equal(
        "▸ frontmatter (9 fields) — zi cycles, zo opens",
        vim.api.nvim_win_call(ui.current_wins().capture, function()
          return ui.foldtext()
        end)
      )
    end)

    it("a not-yet-fetched frontmatter says `(loading…)` and NEVER `(no frontmatter)`", function()
      local st = state_for(capture_path)
      st.captures[1].frontmatter = nil
      mount(nil, st)
      local lines = card()
      assert.are.same({ " Capture 1 of 1", " (loading…)" }, lines)
      -- ⚠ `nil` (not fetched) and `{}` (fetched, empty) are DIFFERENT states:
      -- claiming "no frontmatter" here is a statement the client cannot make,
      -- and it would flash falsely once per capture advance.
      assert.is_nil(joined(lines):find("no frontmatter", 1, true))
    end)

    it("a fetched-and-empty frontmatter says `(no frontmatter)`", function()
      local st = state_for(plain_path)
      st.captures[1].frontmatter = {}
      mount(nil, st)
      assert.are.same({ " Capture 1 of 1", " (no frontmatter)" }, card())
    end)

    it("a user-opened fold is never re-closed; a capture advance re-applies it", function()
      local st = mount()
      local win = ui.current_wins().capture
      -- The user's own `zo`.
      vim.api.nvim_win_call(win, function()
        vim.api.nvim_win_set_cursor(0, { 1, 0 })
        vim.cmd("normal! zo")
      end)
      assert.are.equal(-1, fold_closed(win, 1))
      ui.refresh(st)
      -- Fighting the user's `zo` is a bug: it stays open for this capture.
      assert.are.equal(-1, fold_closed(win, 1))

      -- …but the next capture starts folded again.
      vim.fn.writefile(CAPTURE_FILE, plain_path)
      st.captures[2] = { path = plain_path, frontmatter = frontmatter() }
      st.current = 2
      ui.refresh(st)
      assert.are.equal(1, fold_closed(ui.current_wins().capture, 1))
    end)

    it("an edit marks the card as stale instead of showing values it cannot vouch for", function()
      local st = mount()
      local bufs = ui.current_bufs()
      vim.api.nvim_buf_set_lines(bufs.capture, 1, 2, false, { "timestamp: 'edited'" })
      -- `TextChanged` does not fire for an API write, so drive the same state
      -- transition the autocmd drives.
      ui.refresh(st)
      assert.is_false((has(card(), "edited — :w to refresh")))
      vim.api.nvim_exec_autocmds("TextChanged", { buffer = bufs.capture })
      vim.wait(400, function()
        return (has(card(), "edited — :w to refresh"))
      end, 20)
      assert.is_true((has(card(), "^ %(edited — :w to refresh%)$")))
      -- ⚠ and the FOLD is NOT recomputed on TextChanged: re-closing a fold
      -- mid-insert is a hostile edit experience (§3's failure table).
      assert.are.equal(1, fold_closed(ui.current_wins().capture, 1))
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.6 — unparseable YAML
  -------------------------------------------------------------------------

  it("§10.6 — a malformed capture says so loudly, and never shows the LAST capture's values", function()
    local st = state_for(capture_path)
    st.captures[1].frontmatter = { context = { "alpha" } }
    mount(nil, st)
    assert.are.equal("alpha", value_of(card(), "context"))

    st.captures[2] = { path = broken_path, frontmatter = {}, parse_error = true }
    st.current = 2
    ui.refresh(st)

    local lines = card()
    assert.are.same({ " Capture 2 of 2", " ⚠ frontmatter unparseable — showing raw" }, lines)
    -- No fold: hiding the broken YAML would hide the thing that needs fixing.
    assert.are.equal(-1, fold_closed(ui.current_wins().capture, 1))
    -- must-not-be-connected: the previous capture's value is GONE.
    assert.is_nil(joined(lines):find("alpha", 1, true))
    -- The body is still readable and editable — it is the real buffer.
    assert.are.equal("", vim.bo[ui.current_bufs().capture].buftype)
    assert.are.same(
      { "---", "tags: [a, b", 'context: "unterminated', "---", "broken body" },
      vim.api.nvim_buf_get_lines(ui.current_bufs().capture, 0, -1, false)
    )
  end)

  -------------------------------------------------------------------------
  -- §4 — formatters
  -------------------------------------------------------------------------

  describe("formatters (§4)", function()
    local function fmt(key, value, capture_cfg, extra)
      return fields.format(
        key,
        value,
        vim.tbl_extend("force", { capture_config = capture_cfg or {}, meta_fields = {} }, extra or {})
      )
    end

    it("ships the named set, each with its documented output", function()
      assert.are.equal("a, b", fmt("k", { "a", "b" }, { formatters = { k = "raw" } }))
      assert.are.equal("x=1 y=2", fmt("k", { y = 2, x = 1 }, { formatters = { k = "raw" } }))
      assert.are.equal("a · b · +1", fmt("k", { "a", "b", "c" }, { formatters = { k = { name = "list", sep = " · ", max = 2 } } }))
      assert.are.equal("#impro #blog-idea", fmt("k", { "impro", "blog-idea" }, { formatters = { k = "tags" } }))
      assert.are.equal("resources/performing/impro", fmt("k", "resources/performing/impro.md", { formatters = { k = "link" } }))
      assert.are.equal("impro", fmt("k", "resources/performing/impro.md", { formatters = { k = { name = "link", basename = true } } }))
      assert.are.equal("✓", fmt("k", true, { formatters = { k = "boolean" } }))
      assert.are.equal("✗", fmt("k", false, { formatters = { k = "boolean" } }))
      assert.are.equal("3", fmt("k", 3.0, { formatters = { k = "number" } }))
      assert.are.equal("3.14", fmt("k", 3.14159, { formatters = { k = { name = "number", precision = 2 } } }))
    end)

    it("derives the formatter from the core-declared meta.fields type when unlisted", function()
      local ctx = { capture_config = {}, meta_fields = { { key = "topics", type = "list" }, { key = "done", type = "boolean" } } }
      assert.are.equal("a, b", fields.format("topics", { "a", "b" }, ctx))
      assert.are.equal("✓", fields.format("done", true, ctx))
      -- `["*"]` is the fallback for everything the core did not declare.
      assert.are.equal("#x", fields.format("whatever", { "x" }, { capture_config = { formatters = { ["*"] = "tags" } } }))
    end)

    it("§10.14 — calendar bucketing is LOCAL-time and date-only tolerant", function()
      local saved_tz = vim.env.TZ
      vim.env.TZ = "EST5" -- fixed UTC−5, no DST
      local now = os.time({ year = 2026, month = 8, day = 16, hour = 9, min = 0, sec = 0, isdst = false })
      local cc = { formatters = { timestamp = "calendar", created_date = "calendar" } }
      local out = {
        utc = fmt("timestamp", "2026-08-16T02:00Z", cc, { now = now }),
        naive = fmt("timestamp", "2026-08-16 08:00", cc, { now = now }),
        date_only = fmt("created_date", "2026-08-15", cc, { now = now }),
        within_week = fmt("timestamp", "2026-08-12T19:45:00", cc, { now = now }),
        far = fmt("timestamp", "2026-06-01T19:45:00", cc, { now = now }),
        garbage = fmt("timestamp", "not a timestamp at all", cc, { now = now }),
      }
      vim.env.TZ = saved_tz

      -- Bucketed in UTC this reads "Today"; it is 2026-08-15 21:00 LOCALLY.
      assert.are.equal("Yesterday · 9:00 PM", out.utc)
      -- A naive value is already local and is NEVER shifted by the offset.
      assert.are.equal("Today · 8:00 AM", out.naive)
      -- Date-only parses, and the time half is OMITTED, not fabricated as
      -- `12:00 AM` — without this the two date-only keys the `calendar`
      -- formatter ships for would silently fall back to `raw`.
      assert.are.equal("Yesterday", out.date_only)
      assert.are.equal("Wed · 7:45 PM · 4 days ago", out.within_week)
      assert.are.equal("Mon 01 Jun 2026 · 7:45 PM", out.far)
      -- The core keeps timestamps as opaque strings: an unparseable one is
      -- returned VERBATIM, never dropped.
      assert.are.equal("not a timestamp at all", out.garbage)
    end)

    it("§10.7 — a formatter that raises degrades to `raw` and warns exactly ONCE", function()
      local st
      local seen = counting_notifies(function()
        st = mount({
          ui = {
            capture = {
              formatters = {
                tags = function()
                  error("boom")
                end,
              },
            },
          },
        })
        ui.refresh(st)
        ui.refresh(st)
      end)

      local lines = card()
      -- The broken row shows the RAW value…
      assert.are.equal("impro, blog-idea", value_of(lines, "tags"))
      -- …and every other row is intact: one bad formatter is not a bad pane.
      assert.is_truthy(value_of(lines, "context"))
      assert.is_truthy(value_of(lines, "sources"))
      assert.is_truthy(value_of(lines, "timestamp"))
      -- Exactly one WARN per (key, session) across THREE renders.
      assert.are.equal(1, #seen)
      assert.is_truthy(seen[1].msg:find("tags", 1, true))
      -- …and the failure is reportable, not merely survived.
      assert.is_true((has(ui.render_diagnostics(), "^formatter%[tags%]")))
    end)

    it("a formatter returning a newline is rejected the same way as one that raises", function()
      local seen = counting_notifies(function()
        mount({
          ui = {
            capture = {
              formatters = {
                tags = function()
                  return "one\ntwo"
                end,
              },
            },
          },
        })
      end)
      assert.are.equal("impro, blog-idea", value_of(card(), "tags"))
      assert.are.equal(1, #seen)
    end)
  end)

  -------------------------------------------------------------------------
  -- §5 — the ui.capture.render escape hatch
  -------------------------------------------------------------------------

  describe("ui.capture.render (§5)", function()
    it("replaces the card wholesale when it returns a valid shape", function()
      mount({
        ui = {
          capture = {
            render = function(ctx)
              return { { { ("mine: %d/%d %s"):format(ctx.index, ctx.total, ctx.mode), "Comment" } } }
            end,
          },
        },
      })
      assert.are.same({ "mine: 1/1 compact" }, card())
    end)

    it("receives the documented context, including the resolved field lists", function()
      local captured
      mount({
        ui = {
          capture = {
            render = function(ctx)
              captured = ctx
              return { "ok" }
            end,
          },
        },
      })
      assert.are.equal(1, captured.index)
      assert.are.equal(1, captured.total)
      assert.are.equal("compact", captured.mode)
      assert.are.equal(false, captured.parse_error)
      assert.are.same({ "timestamp", "context", "tags", "sources" }, (function()
        local present = {}
        for _, key in ipairs(captured.fields.pinned) do
          if captured.frontmatter[key] ~= nil then
            present[#present + 1] = key
          end
        end
        return present
      end)())
      assert.are.same({ "source_url" }, captured.fields.rest)
      assert.are.equal("#impro #blog-idea", captured.format("tags", { "impro", "blog-idea" }))
      assert.is_true(captured.width > 0)
    end)

    it("§10.8 — rejects a newline, rejects a non-list, and caps at max_card_lines", function()
      -- (a) newline.
      counting_notifies(function()
        mount({ ui = { capture = { render = function() return { "a\nb" } end } } })
      end)
      assert.is_true((has(card(), "^ Capture 1 of 1$")), "newline return must fall back to the built-in card")
      ui.unmount()

      -- (b) not a list.
      counting_notifies(function()
        mount({ ui = { capture = { render = function() return { nope = true } end } } })
      end)
      assert.is_true((has(card(), "^ Capture 1 of 1$")), "non-list return must fall back to the built-in card")
      ui.unmount()

      -- (c) 100 lines against the default cap of 40.
      mount({
        ui = {
          capture = {
            render = function()
              local out = {}
              for i = 1, 100 do
                out[i] = "line " .. i
              end
              return out
            end,
          },
        },
      })
      local lines = card()
      assert.are.equal(40, #lines)
      assert.are.equal("line 39", lines[39])
      assert.are.equal(" + 61 more · zi", lines[40])
    end)

    it("§10.8 — disables a raising override after THREE failures, warning exactly once", function()
      local st
      local seen = counting_notifies(function()
        st = mount({
          ui = {
            capture = {
              render = function()
                error("nope")
              end,
            },
          },
        })
        ui.refresh(st) -- 2
        ui.refresh(st) -- 3 → disabled
        ui.refresh(st) -- 4 → built-in, silent
      end)
      assert.are.equal(1, #seen)
      assert.is_true((has(card(), "^ Capture 1 of 1$")))
      assert.is_true((has(ui.render_diagnostics(), '^capture_render = "override%-disabled %(3 errors%)"$')))
    end)

    it("there is no code path from the override to buffer text", function()
      local before = read_bytes(capture_path)
      local st = mount({
        ui = {
          capture = {
            render = function()
              return { "MALICIOUS CARD LINE" }
            end,
          },
        },
      })
      ui.refresh(st)
      vim.api.nvim_win_call(ui.current_wins().capture, function()
        vim.cmd("silent write")
      end)
      assert.are.equal(before, read_bytes(capture_path))
      assert.is_nil(joined(vim.api.nvim_buf_get_lines(ui.current_bufs().capture, 0, -1, false)):find("MALICIOUS", 1, true))
    end)
  end)

  -------------------------------------------------------------------------
  -- §6 — the organize pane
  -------------------------------------------------------------------------

  describe("organize pane (§6)", function()
    local function right(cfg, st)
      local merged = vim.tbl_deep_extend("force", vim.deepcopy(ui.DEFAULTS), cfg or {})
      return render.right_pane(st or state_for(capture_path), merged)
    end

    it("max_reasons caps the indented reason lines; 0 means all", function()
      local function reasons(cfg)
        local n = 0
        for _, line in ipairs(right(cfg).lines) do
          if line:match("^    %S") then
            n = n + 1
          end
        end
        return n
      end
      assert.are.equal(3, reasons(nil))
      assert.are.equal(1, reasons({ ui = { organize = { max_reasons = 1 } } }))
      assert.are.equal(3, reasons({ ui = { organize = { max_reasons = 0 } } }))
      assert.are.equal(0, reasons({ ui = { organize = { show_reasons = false } } }))
    end)

    it("score_thresholds promote the buckets out of the source (ruling R3)", function()
      local hl = { score_high = "HI", score_medium = "MED", score_low = "LO" }
      -- The shipped literals of 03 §3.
      assert.are.equal("HI", render.score_hl(2.4, hl, nil))
      assert.are.equal("MED", render.score_hl(1.5, hl, nil))
      assert.are.equal("LO", render.score_hl(0.4, hl, nil))
      -- A differently-weighted vault re-buckets the same numbers.
      local thresholds = { high = 3.0, medium = 1.5 }
      assert.are.equal("MED", render.score_hl(2.4, hl, thresholds))
      assert.are.equal("HI", render.score_hl(3.1, hl, thresholds))
      -- ⚠ `ui.highlights.score_*` stay highlight-GROUP names and are
      -- unrelated to these numbers.
      assert.are.equal("string", type(ui.DEFAULTS.ui.highlights.score_high))
      assert.are.equal("number", type(ui.DEFAULTS.ui.organize.score_thresholds.high))
    end)

    it("render_row overrides one row, falls back on nil, and cannot break dispatch", function()
      local out = right({
        ui = {
          organize = {
            render_row = function(ctx)
              if ctx.index ~= 1 then
                return nil
              end
              return { ("row %d: %s"):format(ctx.index, ctx.item.name), "Special" }
            end,
          },
        },
      })
      assert.is_true((has(out.lines, "^row 1: alpha$")))
      -- Dispatch stays keyed on the ITEM, never on the rendered text.
      local lnum = out:line_of(function(item)
        return item.kind == "suggestion"
      end)
      assert.are.equal("/vault/projects/alpha", out.map[lnum].path)
    end)

    it("a raising render_row disables itself after three failures, warning once", function()
      local calls = 0
      local seen = counting_notifies(function()
        for _ = 1, 4 do
          calls = calls + 1
          right({
            ui = {
              organize = {
                render_row = function()
                  error("row boom")
                end,
              },
            },
          })
        end
      end)
      assert.are.equal(4, calls)
      assert.are.equal(1, #seen)
      assert.is_true((has(ui.render_diagnostics(), "^organize_render_row = \"override%-disabled %(3 errors%)\"$")))
    end)

    it("lands render.VIEWS as a registry so 16 and 17 add a view without touching the dispatcher", function()
      assert.are.equal("table", type(render.VIEWS))
      for _, view in ipairs({ "suggestions", "browse", "search", "merge", "loading", "empty" }) do
        assert.are.equal("function", type(render.VIEWS[view]), view)
      end
      render.VIEWS.probe_view = function(_, cfg)
        local out = render.new()
        out:line("registered view for " .. tostring((cfg.ui.organize or {}).max_reasons))
        return out
      end
      local st = state_for(capture_path)
      st.view = "probe_view"
      assert.is_true((has(right(nil, st).lines, "^registered view for 3$")))
      render.VIEWS.probe_view = nil
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.12 — the card is ON SCREEN, not merely in the extmark table
  -------------------------------------------------------------------------

  describe("§10.12 — screen-level card assertion", function()
    --- Everything drawn inside the capture WINDOW's rectangle, as text.
    --- ⚠ Deliberately NOT `nvim_buf_get_extmarks`: the whole point of this
    --- obligation is that an extmark can exist and render nothing (a card
    --- anchored inside a CLOSED fold is never drawn, because Neovim forces
    --- `w_topfill = 0` over one).
    local function capture_screen(win)
      vim.cmd("redraw")
      local pos = vim.fn.win_screenpos(win)
      local height = vim.api.nvim_win_get_height(win)
      local width = vim.api.nvim_win_get_width(win)
      local rows = {}
      for row = pos[1], pos[1] + height - 1 do
        local cells = {}
        for col = pos[2], pos[2] + width - 1 do
          cells[#cells + 1] = vim.fn.screenstring(row, col)
        end
        rows[#rows + 1] = table.concat(cells, "")
      end
      return table.concat(rows, "\n")
    end

    local function mount_at_3_of_47(cfg)
      local st = state_for(capture_path)
      for i = 2, 47 do
        st.captures[i] = { path = dir .. "/capture/filler-" .. i .. ".md", frontmatter = {} }
      end
      st.captures[3] = { path = capture_path, frontmatter = frontmatter() }
      st.current = 3
      return mount(cfg, st)
    end

    it("draws the card under the shipped frontmatter = \"fold\"", function()
      mount_at_3_of_47(nil)
      local screen = capture_screen(ui.current_wins().capture)
      assert.is_truthy(screen:find("Capture 3 of 47", 1, true), screen)
      -- The fold line is ABOVE the card — spec 15 §7's rendered example.
      assert.is_truthy(screen:find("frontmatter (9 fields)", 1, true), screen)
      assert.is_true(screen:find("frontmatter (9 fields)", 1, true) < screen:find("Capture 3 of 47", 1, true))
    end)

    it("draws the card under frontmatter = \"none\" (the winrestview fill path)", function()
      mount_at_3_of_47({ ui = { capture = { frontmatter = "none" } } })
      local screen = capture_screen(ui.current_wins().capture)
      assert.is_truthy(screen:find("Capture 3 of 47", 1, true), screen)
      -- No fold line in this configuration; the raw YAML is the buffer text.
      assert.is_nil(screen:find("frontmatter (9 fields)", 1, true))
      assert.is_truthy(screen:find("context:", 1, true), screen)
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.13 — teardown of a buffer the plugin did not open
  -------------------------------------------------------------------------

  it("§10.13 — `stop` leaves a buffer the plugin did not open exactly as it found it", function()
    -- Matt opens the capture himself, and folds a region of the BODY by hand.
    vim.cmd("edit " .. vim.fn.fnameescape(capture_path))
    local user_win = vim.api.nvim_get_current_win()
    local user_buf = vim.api.nvim_get_current_buf()
    local pre_foldmethod = vim.wo[user_win].foldmethod
    vim.api.nvim_win_call(user_win, function()
      vim.api.nvim_win_set_cursor(0, { FIRST_BODY_LINE, 0 })
      vim.cmd("normal! zf2j")
    end)
    assert.are.equal(FIRST_BODY_LINE, fold_closed(user_win, FIRST_BODY_LINE))

    local st = mount()
    ui.refresh(st)
    ui.unmount()

    assert.is_true(vim.api.nvim_buf_is_valid(user_buf))
    -- The plugin's frontmatter fold is gone…
    assert.are.equal(-1, fold_closed(user_win, 1))
    -- …zero extmarks in NS_CAPTURE…
    assert.are.same({}, vim.api.nvim_buf_get_extmarks(user_buf, NS_CAPTURE, 0, -1, {}))
    -- …zero ParaOrganizeUI autocmds on that buffer…
    local ok, autocmds = pcall(vim.api.nvim_get_autocmds, { group = "ParaOrganizeUI", buffer = user_buf })
    assert.is_true(not ok or #autocmds == 0)
    -- …the window's foldmethod back to what it was…
    assert.are.equal(pre_foldmethod, vim.wo[user_win].foldmethod)
    -- …and, must-not-be-connected, THE USER'S OWN `zf` FOLD STILL EXISTS.
    -- ⚠ This is the assertion that a `zE` teardown would fail.
    assert.are.equal(FIRST_BODY_LINE, fold_closed(user_win, FIRST_BODY_LINE))

    vim.api.nvim_win_call(user_win, function()
      vim.cmd("normal! zE")
    end)
    vim.bo[user_buf].modified = false
    vim.cmd("enew")
    pcall(vim.api.nvim_buf_delete, user_buf, { force = true })
  end)

  -------------------------------------------------------------------------
  -- §10.11 — the 09 §4 UI budget
  -------------------------------------------------------------------------

  it("§10.11 — a 40-key frontmatter card, formatters included, renders under 50 ms", function()
    local st = state_for(capture_path)
    local fm = {
      timestamp = "2026-08-15T19:45:00",
      created_date = "2026-08-15",
      last_edited_date = "2026-08-15",
      tags = { "impro", "blog-idea", "writing", "recruiting" },
      context = { "morning", "reflecting" },
      sources = { "obsidian", "ntfy" },
    }
    for i = 1, 34 do
      fm[("extra_key_%02d"):format(i)] = { "value " .. i, "second " .. i }
    end
    assert.are.equal(40, vim.tbl_count(fm))
    st.captures[1].frontmatter = fm

    -- `max_card_lines` is raised so the measurement covers the WHOLE card:
    -- at the shipped 40 the last row would be a truncation line and eleven
    -- formatters would never run.
    mount({ ui = { capture = { mode = "full", max_card_lines = 100 } } }, st)
    local started = vim.uv.hrtime()
    for _ = 1, 10 do
      ui.refresh(st)
    end
    local per_render_ms = (vim.uv.hrtime() - started) / 1e6 / 10
    -- Every one of the 40 keys is on the card, so the measurement is of the
    -- real work and not of an early return.
    assert.are.equal(41, #card()) -- position line + 40 rows
    assert.is_true(per_render_ms < 50, ("card render took %.1f ms (budget 50)"):format(per_render_ms))
    -- …and the budget warning did NOT fire, which is the same claim read
    -- from the implementation's own instrument.
    assert.are.same({}, ui.render_diagnostics())
  end)

  -------------------------------------------------------------------------
  -- §9.8 — the Two-Users Test (14 §1) applied to this pane
  -------------------------------------------------------------------------

  it("§9.8 — a vault with a completely different vocabulary gets a correct card, zero code changes", function()
    local st = state_for(capture_path)
    st.captures[1].frontmatter = {
      captured_at = "2026-08-15T19:45:00",
      project = "kms-rewrite",
      people = { "ana", "dev", "sam", "kim" },
      summary = "folding both panes was the whole complaint",
      source_url = "resources/reading/x.md",
      uuid = "9f1c",
      device = "phone",
      geo = "37.7,-122.4",
      schema_version = 3,
    }
    mount({
      ui = {
        win_options = { wrap = false },
        capture = {
          mode = "compact",
          fields = {
            pinned = { "captured_at", "project", "people", "summary" },
            hidden = { "uuid", "device", "geo", "schema_version" },
            labels = { captured_at = "When", people = "With" },
          },
          formatters = {
            captured_at = "calendar",
            people = { name = "list", sep = " · ", max = 3 },
            summary = function(v)
              return { { tostring(v), "Comment" } }
            end,
            source_url = "link",
          },
        },
        organize = { max_reasons = 1, score_thresholds = { high = 3.0, medium = 1.5 } },
      },
    }, st)

    local lines = card()
    assert.are.equal(" Capture 1 of 1", lines[1])
    assert.is_truthy(value_of(lines, "When"))
    assert.are.equal("kms-rewrite", value_of(lines, "project"))
    assert.are.equal("ana · dev · sam · +1", value_of(lines, "With"))
    assert.are.equal("folding both panes was the whole complaint", value_of(lines, "summary"))
    -- Nine keys: four pinned, four hidden, ONE rest.
    assert.is_true((has(lines, "^ %+ 1 more: source_url · zi$")))
    -- Labels padded to the widest IN THIS CARD (`project`/`summary`, 7 cols).
    assert.is_true((has(lines, "^ When    ")))
    assert.is_true((has(lines, "^ project kms%-rewrite$")))
  end)
end)
