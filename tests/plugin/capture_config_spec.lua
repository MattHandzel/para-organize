-- capture_config_spec — spec 15's CONFIG law: the two closed records
-- (`ui.capture.*`, `ui.organize.*`), the deleted `ui.display` section, the
-- list leaves, the refusal predicates of §10.5 and the honored-key
-- enumeration of §10.15 (21 rows owned by this document).
--
-- ⚠ HOUSE STANDARD (ARCHITECTURE.md): every refusal predicate here is paired
-- with a FIRING CONTROL at an adjacent parameter where the operation must
-- SUCCEED. A guard that refuses everything passes a guard-deleted test, and
-- the firing-control half is what catches that blindness.

local ui = require("para-organize.ui")
local fields = require("para-organize.ui.fields")
local render = require("para-organize.ui.render")
local config = require("para-organize.config")
local actions = require("para-organize.actions")

local NS_CAPTURE = vim.api.nvim_create_namespace("para_organize_capture")

---------------------------------------------------------------------------
-- helpers
---------------------------------------------------------------------------

local function card(buf)
  local out = {}
  buf = buf or ui.current_bufs().capture
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

local function row_label(line)
  if line:match("^ Capture %d+ of %d+$") or line:match("^ %+ ") or line:match("^ %(") or line:match("^ ⚠") or line:match("^ %-%- ") then
    return nil
  end
  return line:match("^ (%S+) +%S")
end

local function labels_of(lines)
  local out = {}
  for _, line in ipairs(lines) do
    local label = row_label(line)
    if label then
      out[#out + 1] = label
    end
  end
  return out
end

local function value_of(lines, key)
  for _, line in ipairs(lines) do
    if row_label(line) == key then
      return (line:match("^ %S+ +(.*)$"):gsub("%s+$", ""))
    end
  end
  return nil
end

local function collapse_line(lines)
  for _, line in ipairs(lines) do
    if line:match("^ %+ %d+ more") then
      return line
    end
  end
  return nil
end

--- Every para-organize binding on `buf`, keyed by DESCRIPTION: `<leader>` is
--- already expanded to `mapleader` in the keymap table, so the literal string
--- never appears as an lhs.
local function bound_descs(buf)
  local out = {}
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return out
  end
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(buf, "n")) do
    if map.desc and map.desc:match("^para%-organize: ") then
      out[map.desc] = map.lhs
    end
  end
  return out
end

local function bound_lhs(buf, lhs)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return false
  end
  for _, map in ipairs(vim.api.nvim_buf_get_keymap(buf, "n")) do
    if map.lhs == lhs then
      return true
    end
  end
  return false
end

local CYCLE_DESC = "para-organize: Cycle capture fields (compact/full/raw)"

describe("spec 15 — config law", function()
  local dir, capture_path

  local FRONTMATTER = {
    timestamp = "2026-08-15T19:45:00",
    context = { "morning" },
    tags = { "impro", "blog-idea" },
    sources = { "obsidian" },
    source_url = "https://example.invalid/x",
    location = { city = "San Francisco" },
    processing_status = "raw",
    capture_id = "20260815T194500",
    aliases = { "20260815T194500" },
    importance = "high",
  }

  local function state_for()
    return {
      captures = {
        { path = capture_path, filename = "capture-one.md", frontmatter = vim.deepcopy(FRONTMATTER) },
      },
      current = 1,
      selected = 1,
      view = "suggestions",
      sort = "intelligent",
      suggestions = {
        {
          path = "/vault/projects/alpha",
          name = "alpha",
          type = "projects",
          score = 2.4,
          reasons = { "tag match: impro", "route rule", "recent sibling" },
        },
      },
      meta_fields = { { key = "importance", type = "enum", keymap = "i", values = { "high", "low" } } },
    }
  end

  --- Mount the real UI with `cfg` as the user's `setup()` table — through
  --- `config.setup`, which is the path a real install takes, so the closed
  --- records and the list leaves are exercised, not bypassed.
  local function with(cfg, fn)
    ui.unmount()
    config.reset()
    actions.reset()
    fields.reset_session()
    local merged = config.setup(vim.tbl_deep_extend("force", { ui = { close_on_complete = false } }, cfg or {}))
    ui.setup(merged)
    actions.setup({ ui = ui, config = merged })
    local st = state_for()
    ui.mount(st, {})
    local value = fn(st)
    ui.unmount()
    config.reset()
    actions.reset()
    return value
  end

  before_each(function()
    ui.unmount()
    config.reset()
    actions.reset()
    fields.reset_session()
    dir = vim.fn.tempname()
    vim.fn.mkdir(dir .. "/capture", "p")
    capture_path = dir .. "/capture/capture-one.md"
    vim.fn.writefile({
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
      "processing_status: raw",
      "capture_id: '20260815T194500'",
      "importance: high",
      "---",
      "## Content",
      "body line",
    }, capture_path)
  end)

  after_each(function()
    ui.unmount()
    config.reset()
    actions.reset()
    pcall(vim.fn.delete, dir, "rf")
  end)

  -------------------------------------------------------------------------
  -- §10.4 — the list-replacement tripwire
  -------------------------------------------------------------------------

  describe("§10.4 — pinned/hidden are LIST LEAVES", function()
    -- ⚠ The literals below are written out ON PURPOSE. Asserting against
    -- `fields.DEFAULT_PINNED` or `ui.DEFAULTS…` would pass whatever the
    -- source happened to say, which is not a test of the SHIPPED default.
    it("ships exactly the measured default lists, in the ruling-R15 order", function()
      config.setup({})
      local shipped = config.get().ui.capture.fields
      assert.are.same({ "title", "summary", "timestamp", "context", "tags", "sources" }, shipped.pinned)
      assert.are.same({
        "location",
        "processing_status",
        "created_date",
        "last_edited_date",
        "id",
        "aliases",
        "capture_id",
        "modalities",
        "metadata",
      }, shipped.hidden)
      assert.are.equal(false, shipped.show_empty_pinned)
      assert.are.equal(true, shipped.show_rest_keys)
      assert.are.equal(true, shipped.pin_metadata_fields)
      assert.are.same({}, shipped.labels)
    end)

    it("`pinned = { \"tags\" }` yields exactly ONE pinned row, and it is tags", function()
      config.setup({ ui = { capture = { fields = { pinned = { "tags" } } } } })
      -- ⚠ `vim.tbl_deep_extend` merges array-like tables BY INDEX: without the
      -- list-leaf rule this reads
      -- { "tags", "summary", "timestamp", "context", "tags", "sources" } —
      -- the user asked for one row, got six, and `tags` appears twice.
      assert.are.same({ "tags" }, config.get().ui.capture.fields.pinned)

      local rows = with({ ui = { capture = { fields = { pinned = { "tags" }, pin_metadata_fields = false } } } }, function()
        return labels_of(card())
      end)
      assert.are.same({ "tags" }, rows)
    end)

    it("`hidden` is replaced wholesale too, so an emptied list hides nothing", function()
      config.setup({ ui = { capture = { fields = { hidden = { "aliases" } } } } })
      assert.are.same({ "aliases" }, config.get().ui.capture.fields.hidden)

      local collapse = with({ ui = { capture = { fields = { hidden = {} } } } }, function()
        return collapse_line(card())
      end)
      -- Nothing suppressed: aliases / capture_id / location /
      -- processing_status are all REST now, so the count rises from 1 to 5.
      assert.are.equal(" + 5 more: aliases, capture_id, location, processing_status, source_url · zi", collapse)
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.5 — refusal predicates, each with a firing control
  -------------------------------------------------------------------------

  describe("§10.5 — refusal predicates", function()
    it("(a) a key in BOTH pinned and hidden is a ConfigError naming it", function()
      local ok, err = pcall(config.setup, {
        ui = { capture = { fields = { pinned = { "tags" }, hidden = { "tags" } } } },
      })
      assert.is_false(ok)
      local message = type(err) == "table" and (err.message or tostring(err)) or tostring(err)
      assert.is_truthy(message:find("tags", 1, true), message)
      assert.is_truthy(message:find("ui.capture.fields", 1, true), message)

      -- FIRING CONTROL: the same key in `pinned` ALONE loads clean.
      assert.has_no.errors(function()
        config.setup({ ui = { capture = { fields = { pinned = { "tags" } } } } })
      end)
      -- …and in `hidden` alone, which is a denylist of POSSIBLE keys: a key
      -- this vault never uses is a no-op, never an error.
      assert.has_no.errors(function()
        config.setup({ ui = { capture = { fields = { hidden = { "a_key_no_vault_has" } } } } })
      end)
    end)

    it("(b) cycle_fields = \"\" unbinds zi everywhere; a rebind moves it", function()
      local unbound = with({ keymaps = { buffer = { cycle_fields = "" } } }, function()
        local bufs = ui.current_bufs()
        return {
          organize_desc = bound_descs(bufs.organize)[CYCLE_DESC],
          organize_zi = bound_lhs(bufs.organize, "zi"),
          capture_zi = bound_lhs(bufs.capture, "zi"),
        }
      end)
      assert.is_nil(unbound.organize_desc)
      assert.is_false(unbound.organize_zi)
      assert.is_false(unbound.capture_zi)

      -- FIRING CONTROL: `<leader>tf` binds, and `zi` is left free.
      local rebound = with({ keymaps = { buffer = { cycle_fields = "<leader>tf" } } }, function()
        local bufs = ui.current_bufs()
        return {
          organize_desc = bound_descs(bufs.organize)[CYCLE_DESC],
          organize_zi = bound_lhs(bufs.organize, "zi"),
        }
      end)
      -- `<leader>` is expanded to mapleader (a space) in the keymap table.
      assert.are.equal(" tf", rebound.organize_desc)
      assert.is_false(rebound.organize_zi)
    end)

    it("(b) ruling R7 — zi binds in the ORGANIZE pane ONLY", function()
      local seen = with(nil, function()
        local bufs = ui.current_bufs()
        return {
          organize = bound_descs(bufs.organize)[CYCLE_DESC],
          capture = bound_descs(bufs.capture)[CYCLE_DESC],
          capture_z_prefix = bound_lhs(bufs.capture, "zi"),
        }
      end)
      assert.are.equal("zi", seen.organize)
      -- ⚠ Load-bearing, not a preference: with `zi` bound in the organize
      -- pane only, `z` is not a prefix in the capture pane at all, so `zo` /
      -- `za` there stay INSTANT and spec 15 §3's fold-recovery claim ("the
      -- user can always open the fold by hand") is true as written.
      assert.is_nil(seen.capture)
      assert.is_false(seen.capture_z_prefix)
    end)

    it("(c) a moved ui.display key names its replacement", function()
      local ok, err = pcall(config.setup, { ui = { display = { show_scores = false } } })
      assert.is_false(ok)
      local message = type(err) == "table" and (err.message or tostring(err)) or tostring(err)
      assert.is_truthy(message:find("ui.organize.show_scores", 1, true), message)

      -- FIRING CONTROL: the NEW spelling loads clean and suppresses the score
      -- column — the key is honored, not merely accepted.
      local lines = with({ ui = { organize = { show_scores = false } } }, function()
        return vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false)
      end)
      local found = false
      for _, line in ipairs(lines) do
        if line:match("^%[P%] alpha$") then
          found = true
        end
        assert.is_nil(line:find("2.40", 1, true), line)
      end
      assert.is_true(found)
    end)

    it("(d) ANY key under ui.display is a ConfigError — the section does not exist", function()
      -- A key no document ever shipped: `MOVED_KEYS` cannot help, and the
      -- error must still fire, because the SECTION is gone (ruling R1).
      local ok, err = pcall(config.setup, { ui = { display = { a_key_no_doc_ever_shipped = 1 } } })
      assert.is_false(ok)
      local message = type(err) == "table" and (err.message or tostring(err)) or tostring(err)
      assert.is_truthy(message:find("ui.display.a_key_no_doc_ever_shipped", 1, true), message)

      -- …and an empty `ui.display = {}` is refused too: a shipped `setup()`
      -- block naming the section fails on a fresh install.
      assert.is_false((pcall(config.setup, { ui = { display = {} } })))

      -- FIRING CONTROL: the replacement section loads clean and is honored.
      local lines = with({ ui = { capture = { show_position = false } } }, function()
        return card()
      end)
      for _, line in ipairs(lines) do
        assert.is_nil(line:find("Capture 1 of", 1, true), line)
      end
      assert.is_true(#lines > 0, "the rest of the card still renders")
    end)

    it("the closed records refuse an unknown leaf, naming the dotted key", function()
      for _, case in ipairs({
        { cfg = { ui = { capture = { show_positions = true } } }, key = "ui.capture.show_positions" },
        { cfg = { ui = { organize = { max_reason = 1 } } }, key = "ui.organize.max_reason" },
        { cfg = { ui = { capture = { fields = { pin = { "x" } } } } }, key = "ui.capture.fields.pin" },
      }) do
        local ok, err = pcall(config.setup, case.cfg)
        assert.is_false(ok, case.key)
        local message = type(err) == "table" and (err.message or tostring(err)) or tostring(err)
        assert.is_truthy(message:find(case.key, 1, true), message)
      end

      -- FIRING CONTROL: the three DELIBERATE free maps take arbitrary keys.
      assert.has_no.errors(function()
        config.setup({
          ui = {
            win_options = { conceallevel = 2 },
            capture = {
              fields = { labels = { any_frontmatter_key_at_all = "Whatever" } },
              formatters = { any_frontmatter_key_at_all = "raw" },
            },
          },
        })
      end)
    end)

    it("types are checked, so a wrong-shaped value cannot reach the renderer", function()
      for _, case in ipairs({
        { cfg = { ui = { capture = { mode = "verbose" } } }, key = "ui.capture.mode" },
        { cfg = { ui = { capture = { frontmatter = "conceal" } } }, key = "ui.capture.frontmatter" },
        { cfg = { ui = { capture = { fields = { pinned = { 1, 2 } } } } }, key = "ui.capture.fields.pinned" },
        { cfg = { ui = { capture = { max_card_lines = -1 } } }, key = "ui.capture.max_card_lines" },
        { cfg = { ui = { capture = { formatters = { tags = 42 } } } }, key = "ui.capture.formatters.tags" },
        { cfg = { ui = { capture = { foldtext = 7 } } }, key = "ui.capture.foldtext" },
        { cfg = { ui = { organize = { score_thresholds = { high = "big" } } } }, key = "ui.organize.score_thresholds.high" },
      }) do
        local ok, err = pcall(config.setup, case.cfg)
        assert.is_false(ok, case.key)
        local message = type(err) == "table" and (err.message or tostring(err)) or tostring(err)
        assert.is_truthy(message:find(case.key, 1, true), message)
      end

      -- FIRING CONTROL: every one of those leaves accepts its documented shape.
      assert.has_no.errors(function()
        config.setup({
          ui = {
            capture = {
              mode = "full",
              frontmatter = "none",
              fields = { pinned = { "tags" } },
              max_card_lines = 0,
              formatters = { tags = { name = "list", sep = " / " } },
              foldtext = function()
                return "x"
              end,
            },
            organize = { score_thresholds = { high = 3.0 } },
          },
        })
      end)
    end)
  end)

  -------------------------------------------------------------------------
  -- §10.15 — the honored-key enumeration: 21 rows owned by this document
  -------------------------------------------------------------------------

  describe("§10.15 — every key this document introduces is honored", function()
    --- Each row: the dotted key, the non-default config, and a probe whose
    --- value must DIFFER between the default and the non-default arm. A
    --- reader that ignores its value (hardcodes the default) turns this red
    --- naming the dotted key.
    local ROWS = {
      {
        key = "ui.win_options",
        cfg = { ui = { win_options = { foldenable = true } } },
        probe = function()
          return tostring(vim.wo[ui.current_wins().organize].foldenable)
        end,
        default_is = "false",
      },
      {
        key = "ui.capture.show_position",
        cfg = { ui = { capture = { show_position = false } } },
        probe = function()
          return tostring(card()[1])
        end,
        default_is = " Capture 1 of 1",
      },
      {
        key = "ui.capture.mode",
        cfg = { ui = { capture = { mode = "full" } } },
        probe = function()
          return tostring(value_of(card(), "processing_status"))
        end,
        default_is = "nil",
      },
      {
        key = "ui.capture.frontmatter",
        cfg = { ui = { capture = { frontmatter = "none" } } },
        probe = function()
          return tostring(vim.api.nvim_win_call(ui.current_wins().capture, function()
            return vim.fn.foldclosed(1)
          end))
        end,
        default_is = "1",
      },
      {
        key = "ui.capture.foldtext",
        cfg = { ui = { capture = { foldtext = "custom {count}" } } },
        probe = function()
          return vim.api.nvim_win_call(ui.current_wins().capture, function()
            return ui.foldtext()
          end)
        end,
        default_is = "▸ frontmatter (10 fields) — zi cycles, zo opens",
      },
      {
        key = "ui.capture.max_card_lines",
        cfg = { ui = { capture = { max_card_lines = 3 } } },
        probe = function()
          return tostring(#card())
        end,
        default_is = "7",
      },
      {
        -- Both arms carry the SAME deliberately slow formatter, so the only
        -- thing that varies is the budget.
        key = "ui.capture.render_budget_ms",
        base = {
          ui = {
            capture = {
              formatters = {
                tags = function(value)
                  vim.uv.sleep(6)
                  return table.concat(value, ", ")
                end,
              },
            },
          },
        },
        cfg = { ui = { capture = { render_budget_ms = 1 } } },
        probe = function()
          return tostring(#ui.render_diagnostics())
        end,
        default_is = "0",
      },
      {
        key = "ui.capture.render",
        cfg = {
          ui = {
            capture = {
              render = function()
                return { "a card of my own" }
              end,
            },
          },
        },
        probe = function()
          return tostring(card()[1])
        end,
        default_is = " Capture 1 of 1",
      },
      {
        key = "ui.capture.formatters",
        cfg = { ui = { capture = { formatters = { tags = "list" } } } },
        probe = function()
          return tostring(value_of(card(), "tags"))
        end,
        default_is = "#impro #blog-idea",
      },
      {
        key = "ui.capture.fields.pinned",
        cfg = { ui = { capture = { fields = { pinned = { "sources" } } } } },
        probe = function()
          return table.concat(labels_of(card()), ",")
        end,
        default_is = "timestamp,context,tags,sources,importance",
      },
      {
        key = "ui.capture.fields.hidden",
        cfg = { ui = { capture = { fields = { hidden = {} } } } },
        probe = function()
          return tostring(collapse_line(card()))
        end,
        default_is = " + 1 more: source_url · zi",
      },
      {
        key = "ui.capture.fields.pin_metadata_fields",
        cfg = { ui = { capture = { fields = { pin_metadata_fields = false } } } },
        probe = function()
          return tostring(value_of(card(), "importance"))
        end,
        default_is = "high   (i)",
      },
      {
        key = "ui.capture.fields.show_empty_pinned",
        cfg = { ui = { capture = { fields = { show_empty_pinned = true } } } },
        probe = function()
          return tostring(value_of(card(), "title"))
        end,
        default_is = "nil",
      },
      {
        key = "ui.capture.fields.show_rest_keys",
        cfg = { ui = { capture = { fields = { show_rest_keys = false } } } },
        probe = function()
          return tostring(collapse_line(card()))
        end,
        default_is = " + 1 more: source_url · zi",
      },
      {
        key = "ui.capture.fields.labels",
        cfg = { ui = { capture = { fields = { labels = { timestamp = "When" } } } } },
        probe = function()
          return table.concat(labels_of(card()), ",")
        end,
        default_is = "timestamp,context,tags,sources,importance",
      },
      {
        key = "ui.organize.show_scores",
        cfg = { ui = { organize = { show_scores = false } } },
        probe = function()
          return table.concat(vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false), "\n")
        end,
      },
      {
        key = "ui.organize.show_reasons",
        cfg = { ui = { organize = { show_reasons = false } } },
        probe = function()
          return table.concat(vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false), "\n")
        end,
      },
      {
        key = "ui.organize.max_reasons",
        cfg = { ui = { organize = { max_reasons = 1 } } },
        probe = function()
          local n = 0
          for _, line in ipairs(vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false)) do
            if line:match("^    %S") then
              n = n + 1
            end
          end
          return tostring(n)
        end,
        default_is = "3",
      },
      {
        key = "ui.organize.score_thresholds",
        cfg = { ui = { organize = { score_thresholds = { high = 3.0, medium = 1.5 } } } },
        -- ⚠ The score buckets emit the plugin's OWN groups now, not the stock
        -- `Diagnostic*` ones — `ui.HL_GROUPS` `default`-links each to the
        -- stock group it used to name, so the rendered COLOR is unchanged
        -- while `ParaOrganizeScoreHigh` becomes a real hook a colorscheme can
        -- take over (spec 14 §5).
        probe = function()
          local groups = {}
          for _, mark in ipairs(
            vim.api.nvim_buf_get_extmarks(ui.current_bufs().organize, -1, 0, -1, { details = true })
          ) do
            local group = (mark[4] or {}).hl_group
            if group and group:match("^ParaOrganizeScore") then
              groups[#groups + 1] = group
            end
          end
          table.sort(groups)
          return table.concat(groups, ",")
        end,
        default_is = "ParaOrganizeScoreHigh",
      },
      {
        key = "ui.organize.render_row",
        cfg = {
          ui = {
            organize = {
              render_row = function(ctx)
                return ("row %d of my own"):format(ctx.index)
              end,
            },
          },
        },
        probe = function()
          return table.concat(vim.api.nvim_buf_get_lines(ui.current_bufs().organize, 0, -1, false), "\n")
        end,
      },
      {
        key = "keymaps.buffer.cycle_fields",
        cfg = { keymaps = { buffer = { cycle_fields = "gZ" } } },
        probe = function()
          return tostring(bound_descs(ui.current_bufs().organize)[CYCLE_DESC])
        end,
        default_is = "zi",
      },
    }

    it("enumerates exactly the 21 rows spec 15 §10.15 counts", function()
      assert.are.equal(21, #ROWS)
      local seen = {}
      for _, row in ipairs(ROWS) do
        assert.is_nil(seen[row.key], "duplicate row: " .. row.key)
        seen[row.key] = true
      end
    end)

    for _, row in ipairs(ROWS) do
      it(("honors `%s`: default ⇒ A, non-default ⇒ B, and B ≠ A"):format(row.key), function()
        local default_value = with(row.base, row.probe)
        local other_value = with(vim.tbl_deep_extend("force", vim.deepcopy(row.base or {}), row.cfg), row.probe)
        assert.are_not.equal(default_value, other_value, row.key .. " is not honored — the reader ignores its value")
        if row.default_is then
          -- A literal pin on the DEFAULT arm, so "different" cannot be
          -- satisfied by two equally wrong values.
          assert.are.equal(row.default_is, default_value, row.key)
        end
      end)
    end

    it("every leaf of the two closed records has a reader (the mirror gate for this document)", function()
      -- Ruling R2: `ui.capture` and `ui.organize` are landed HERE, complete,
      -- so 16 and 17 fill in leaves that already exist. The four leaves whose
      -- BEHAVIOUR doc 16 §3 owns are declared but counted in 16 §8, not here.
      local capture_leaves = {}
      for key in pairs(config.get().ui.capture) do
        capture_leaves[#capture_leaves + 1] = key
      end
      table.sort(capture_leaves)
      assert.are.same({
        "fields",
        "formatters",
        "frontmatter",
        "max_card_lines",
        "mode",
        "render_budget_ms",
        "show_position",
      }, capture_leaves)

      local organize_leaves = {}
      for key in pairs(config.get().ui.organize) do
        organize_leaves[#organize_leaves + 1] = key
      end
      table.sort(organize_leaves)
      assert.are.same({
        "max_reasons",
        "numeric_accept",
        "preview_debounce_ms",
        "preview_notes",
        "score_thresholds",
        "show_progress",
        "show_reasons",
        "show_scores",
      }, organize_leaves)

      -- `foldtext`, `render` and `render_row` ship as `nil` (no default
      -- value to store) but are live keys: the validator types them and the
      -- renderer reads them, which the honored-key rows above prove.
      assert.has_no.errors(function()
        config.setup({
          ui = {
            capture = {
              foldtext = "x",
              render = function()
                return { "x" }
              end,
            },
            organize = {
              render_row = function()
                return nil
              end,
            },
          },
        })
      end)
    end)
  end)

  -------------------------------------------------------------------------
  -- §2 — the mode indicator, and the sticky session mode
  -------------------------------------------------------------------------

  describe("field mode (§2)", function()
    it("starts at ui.capture.mode and is sticky across a capture advance", function()
      with({ ui = { capture = { mode = "full" } } }, function(st)
        assert.are.equal("full", ui.field_mode())
        st.captures[2] = { path = capture_path, frontmatter = vim.deepcopy(FRONTMATTER) }
        st.current = 2
        ui.refresh(st)
        assert.are.equal("full", ui.field_mode())
      end)
    end)

    it("cycles compact → full → raw → compact and mirrors onto the state", function()
      with(nil, function(st)
        assert.are.equal("compact", ui.field_mode())
        assert.are.equal("full", ui.cycle_fields())
        assert.are.equal("raw", ui.cycle_fields())
        assert.are.equal("compact", ui.cycle_fields())
        assert.are.equal("compact", st.field_mode)
      end)
    end)

    it("names the mode in the card when the layout has no border to carry it", function()
      local lines = with({ ui = { layout = "split", capture = { mode = "full" } } }, function()
        return card()
      end)
      assert.are.equal(" -- full --", lines[1])
    end)

    it("names `raw` even in the bordered layout, where there is no card to carry it", function()
      local lines = with({ ui = { capture = { mode = "raw" } } }, function()
        return card()
      end)
      assert.are.same({ " -- raw --", " Capture 1 of 1" }, lines)
    end)
  end)
end)
