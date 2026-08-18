--- para-organize.config — the UI-ONLY configuration surface (spec 10 §3).
---
--- > "The nvim `setup()` table keeps only UI concerns (layout, icons,
--- >  highlights, keymaps) plus the socket path; everything behavioral lives
--- >  in the core config so CLI and UI can never disagree."  — spec 10 §3
---
--- Two laws are enforced here, both from spec 03 §1:
---
---   1. **Every config key is honored or deleted.** An unknown key raises a
---      `ConfigError`-shaped Lua error naming the exact dotted key, with a
---      "did you mean" when one is close. This mirrors the core's own
---      unknown-key law (`organize_core.config`), so a user cannot silently
---      configure the wrong door.
---   2. **Keys that MOVED to the core say where they went.** `vault_dir`,
---      `para_folders`, `suggestions`, `indexing`, `metadata_fields` … each
---      names its new home in `~/.config/organize-core/config.toml`
---      (`doc/MIGRATION-from-old-setup.md` is the full table).
---
--- Leaf types, enums and ranges are checked — spec 03 §1 explicitly calls out
--- that the old `validate.lua` "only asserts six sections are tables".
---
--- Owned by the INTEGRATOR.

local M = {}

M.PREFIX = "para-organize: "

---------------------------------------------------------------------------
-- defaults
---------------------------------------------------------------------------

--- UI defaults live with the UI (`ui.DEFAULTS`) so ui/actions stay testable
--- without this module; everything the CORE PROCESS needs to be reached is
--- added here. Requiring `ui` at load is safe: its own `require`s are lazy.
local function ui_defaults()
  local ok, ui = pcall(require, "para-organize.ui")
  if ok and type(ui) == "table" and type(ui.DEFAULTS) == "table" then
    return vim.deepcopy(ui.DEFAULTS)
  end
  return { ui = {}, keymaps = { buffer = {} } }
end

--- `$XDG_RUNTIME_DIR/organize-core.sock` (spec 10 §1). AF_UNIX caps the path
--- at ~104 bytes, so the fallback is deliberately short too.
function M.default_socket_path()
  local runtime = vim.env.XDG_RUNTIME_DIR
  if type(runtime) == "string" and runtime ~= "" then
    return runtime .. "/organize-core.sock"
  end
  return ("/tmp/organize-core-%d.sock"):format(vim.uv.getuid and vim.uv.getuid() or 0)
end

--- AF_UNIX `sun_path` limit. Checked here as well as in `health` because the
--- core reports the overrun as a bare OSError with no errno.
M.MAX_SOCKET_PATH = 104

local function base_defaults()
  local defaults = ui_defaults()
  defaults.socket_path = M.default_socket_path()
  defaults.core_cmd = { "organize", "serve" }
  defaults.core = {
    -- `socket_path` / `core_cmd` are read from the TOP LEVEL by
    -- `para-organize.core` and `health`; the nested forms exist so a user can
    -- keep every core-process knob in one block. Top level wins.
    socket_path = nil,
    core_cmd = nil,
    core_env = {},
    core_cwd = nil,
    core_log = nil,
    spawn = true,
    spawn_timeout_ms = 2000,
    timeout_ms = 5000,
    reconnect = true,
  }
  defaults.telescope = {
    theme = "dropdown",
    layout_strategy = nil,
    layout_config = {},
    previewer = false,
    multi_select = false,
  }
  return defaults
end

--- The shipped defaults (deep copy on every access — never hand out the
--- table the validator compares against).
function M.get_defaults()
  return base_defaults()
end

M.defaults = base_defaults()

---------------------------------------------------------------------------
-- the schema
---------------------------------------------------------------------------

local KEYMAP_NAMES = {}
do
  local defaults = base_defaults()
  for name in pairs((defaults.keymaps or {}).buffer or {}) do
    KEYMAP_NAMES[name] = true
  end
end

--- `free` — an open map whose VALUES are typed but whose keys are the user's
--- (icons per type, telescope layout knobs, environment variables).
--- `fields` — a closed record: any other key is an error.
local SCHEMA = {
  fields = {
    socket_path = { type = "string" },
    core_cmd = { type = "string_or_list" },
    core = {
      fields = {
        socket_path = { type = "string" },
        core_cmd = { type = "string_or_list" },
        core_env = { free = true, value = { type = "scalar" } },
        core_cwd = { type = "string" },
        core_log = { type = "string" },
        spawn = { type = "boolean" },
        spawn_timeout_ms = { type = "number", min = 1 },
        timeout_ms = { type = "number", min = 1 },
        reconnect = { type = "boolean" },
        on_event = { type = "function" },
        on_close = { type = "function" },
      },
    },
    ui = {
      fields = {
        layout = { type = "string", enum = { "float", "split" } },
        float_opts = {
          fields = {
            width = { type = "number", min = 0.05, max = 1.0 },
            height = { type = "number", min = 0.05, max = 1.0 },
            border = { type = "string_or_list" },
          },
        },
        capture_pane_keymaps = { type = "string", enum = { "core", "navigation", "none" } },
        auto_move_to_new_folder = { type = "boolean" },
        close_on_complete = { type = "boolean" },
        -- ⚠ `ui.display` DOES NOT EXIST (spec 15 §2, ruling R1). The section
        -- is deleted, every key has a new home, and `moved_section` makes the
        -- validator report the replacement for each CHILD key rather than a
        -- bare "unknown key `ui.display`". No document may add a key here.
        display = { moved_section = true },

        -- Spec 15 §7 — the two CLOSED records this document lands FIRST
        -- (ruling R2), so docs 16 and 17 append leaves to records that
        -- already exist instead of inventing a second spelling.
        capture = {
          fields = {
            show_position = { type = "boolean" },
            mode = { type = "string", enum = { "compact", "full", "raw" } },
            frontmatter = { type = "string", enum = { "fold", "none" } },
            foldtext = { type = "string_or_function" },
            max_card_lines = { type = "number", min = 0 },
            render_budget_ms = { type = "number", min = 0 },
            render = { type = "function" },
            fields = {
              fields = {
                -- LIST LEAVES: replaced wholesale, never index-merged.
                pinned = { type = "string_list" },
                hidden = { type = "string_list" },
                pin_metadata_fields = { type = "boolean" },
                show_empty_pinned = { type = "boolean" },
                show_rest_keys = { type = "boolean" },
                -- One of the three deliberate free maps: the user's own
                -- frontmatter keys are the key space.
                labels = { free = true, value = { type = "string" } },
              },
            },
            formatters = { free = true, value = { type = "formatter" } },
          },
        },
        organize = {
          fields = {
            show_scores = { type = "boolean" },
            show_reasons = { type = "boolean" },
            max_reasons = { type = "number", min = 0 },
            score_thresholds = {
              fields = {
                high = { type = "number" },
                medium = { type = "number" },
              },
            },
            render_row = { type = "function" },
            -- Leaves owned by doc 16 §3.x; declared here because 15 lands
            -- the record (ruling R2). 16 fills in the behaviour.
            show_progress = { type = "boolean" },
            numeric_accept = { type = "boolean" },
            preview_notes = { type = "number", min = 0 },
            preview_debounce_ms = { type = "number", min = 0 },
          },
        },
        highlights = {
          fields = {
            selected = { type = "string" },
            header = { type = "string" },
            reason = { type = "string" },
            score_high = { type = "string" },
            score_medium = { type = "string" },
            score_low = { type = "string" },
            hint = { type = "string" },
          },
        },
        -- Free map: window-local option names. Merged over the pane defaults
        -- (`ui.PANE_WIN_OPTIONS`) at mount, so `{ foldenable = true }` opts
        -- back into the user's global folds.
        win_options = { free = true, value = { type = "scalar" } },
        icons = {
          fields = {
            project = { type = "string" },
            area = { type = "string" },
            resource = { type = "string" },
            archive = { type = "string" },
            dir = { type = "string" },
            file = { type = "string" },
          },
        },
      },
    },
    keymaps = {
      fields = {
        buffer = { free = true, value = { type = "keymap" }, known = KEYMAP_NAMES },
      },
    },
    telescope = {
      fields = {
        theme = { type = "string", enum = { "dropdown", "cursor", "ivy", "none" } },
        layout_strategy = { type = "string" },
        layout_config = { free = true, value = { type = "any" } },
        previewer = { type = "boolean" },
        multi_select = { type = "boolean" },
      },
    },
  },
}

--- Old plugin `setup()` keys and where they went (spec 10 §3). The full
--- mapping with examples is `doc/MIGRATION-from-old-setup.md`.
M.MOVED_KEYS = {
  paths = "moved to the core: [vault] root / capture_folder / para_folders / archive_capture_path in ~/.config/organize-core/config.toml (spec 10 §3)",
  vault_dir = "moved to the core: [vault] root in ~/.config/organize-core/config.toml (spec 10 §3)",
  capture_folder = "moved to the core: [vault] capture_folder",
  para_folders = "moved to the core: [vault] para_folders (note: the vault folder is 'archive', SINGULAR — 08 §C2)",
  archive_capture_path = "moved to the core: [vault] archive_capture_path",
  suggestions = "moved to the core: [suggestions] (weights, max_suggestions, learning) — spec 04",
  file_ops = "moved to the core: [file_ops] — spec 05",
  indexing = "moved to the core: [vault] ignore_patterns / max_file_size / incremental_debounce; indexing.backend was never implemented and is deleted (08 §A35)",
  patterns = "deleted: patterns.alias_extraction / patterns.case_sensitive were dead keys (08 §A35); patterns.file_glob is core-side",
  metadata_fields = "moved to the core: [[metadata_fields]] in config.toml — spec 07 + 10 §3, so the CLI and the UI cannot disagree about what fields exist",
  routes = "moved to the core: [[routes]] — spec 11",
  consumers = "moved to the core: [consumers] — spec 06",
  automation = "moved to the core: [consumers] — spec 06",
  llm = "moved to the core: [llm] — spec 06",
  data_dir = "resolved by the core: ~/.local/share/organize-core (override with $ORGANIZE_CORE_STATE_DIR) — spec 10 §3",
  log_dir = "resolved by the core: ~/.local/share/organize-core (override with $ORGANIZE_CORE_STATE_DIR) — spec 10 §3",
  state_dir = "resolved by the core: ~/.local/share/organize-core (override with $ORGANIZE_CORE_STATE_DIR) — spec 10 §3",
  debug = "deleted: debug.profile was a dead key (08 §A35); core logging is [logging] level in config.toml, and :ParaOrganize debug prints client diagnostics",
  ["keymaps.global"] = "deleted: no global keymaps by default (spec 03 §2) — use the <Plug> mappings",
  ["ui.display.show_icons"] = "deleted: set ui.icons.* to empty (the default) for spec 03's literal [P]/[A]/[R]/[🗑] markers",
  -- Old plugin keys that survive only in a changed spelling (`d753672~1`).
  ["ui.split_opts"] = "deleted: split mode has no size/direction knobs — set ui.layout = \"split\" (spec 03 §3)",
  ["ui.float_opts.position"] = "deleted: the float is always centered (spec 03 §3)",
  ["ui.icons.enabled"] = "deleted: an EMPTY ui.icons table (the default) renders spec 03's literal [P]/[A]/[R]/[🗑]; set the per-type keys to opt in",
  ["ui.icons.folder"] = "renamed: use ui.icons.dir (spec 03 §3 renders `[D] name` for directories)",
  ["ui.icons.tag"] = "deleted: tags are rendered in the capture header, not iconified (spec 03 §3)",
  ["ui.display.show_counts"] = "renamed: use ui.capture.show_position (\"Capture i of n\", spec 15 §7)",
  ["ui.display.show_timestamps"] = "deleted: the timestamp is a pinned field — configure it with ui.capture.formatters.timestamp (spec 15 §4)",
  -- Spec 15 §2's relocation table, in full. `ui.display` is DELETED as a
  -- section (ruling R1) and every key names where it went.
  ["ui.display.show_position"] = "moved: use ui.capture.show_position (spec 15 §7)",
  ["ui.display.timestamp_format"] = "moved: use ui.capture.formatters.timestamp = { name = \"datetime\", format = … } — the default is now \"calendar\" (spec 15 §4)",
  ["ui.display.show_metadata_summary"] = "moved: use ui.capture.fields.pin_metadata_fields (spec 15 §2)",
  ["ui.display.hide_capture_id"] = "moved: use ui.capture.fields.hidden, which ships with capture_id, id and aliases (spec 15 §2)",
  ["ui.display.hide_modalities"] = "moved: use ui.capture.fields.hidden, which ships with modalities (spec 15 §2)",
  ["ui.display.hide_location"] = "moved: use ui.capture.fields.hidden, which ships with location (spec 15 §2)",
  ["ui.display.show_scores"] = "moved: use ui.organize.show_scores (spec 15 §6)",
  ["ui.display.show_reasons"] = "moved: use ui.organize.show_reasons (spec 15 §6)",
  ["ui.display.show_progress"] = "moved: use ui.organize.show_progress (spec 15 §2; never shipped under the old name)",
  ["ui.display.score_high"] = "moved: use ui.organize.score_thresholds.high — a NUMBER; ui.highlights.score_high is the highlight-GROUP name and is unchanged (ruling R3)",
  ["ui.display.score_medium"] = "moved: use ui.organize.score_thresholds.medium — a NUMBER; ui.highlights.score_medium is the highlight-GROUP name and is unchanged (ruling R3)",
  ["ui.highlights.project"] = "deleted: per-PARA-type highlight groups were dead keys (08 §A35); scores use score_high/medium/low",
  ["ui.highlights.area"] = "deleted: per-PARA-type highlight groups were dead keys (08 §A35)",
  ["ui.highlights.resource"] = "deleted: per-PARA-type highlight groups were dead keys (08 §A35)",
  ["ui.highlights.archive"] = "deleted: per-PARA-type highlight groups were dead keys (08 §A35)",
  ["ui.highlights.tag"] = "deleted: per-PARA-type highlight groups were dead keys (08 §A35)",
  ["telescope.picker_opts"] = "deleted: telescope.picker_opts was a dead key (08 §A35)",
}

---------------------------------------------------------------------------
-- validation
---------------------------------------------------------------------------

local function config_error(message, hint)
  local text = M.PREFIX .. "invalid setup(): " .. message
  if hint then
    text = text .. "\n  " .. hint
  end
  return text
end

local function dotted(prefix, key)
  if prefix == "" then
    return tostring(key)
  end
  return prefix .. "." .. tostring(key)
end

local function is_list(value)
  if type(value) ~= "table" then
    return false
  end
  if next(value) == nil then
    return true
  end
  return vim.islist and vim.islist(value) or vim.tbl_islist(value)
end

--- Closest known key, for "did you mean". Pure Lua — no external deps.
local function suggest(name, candidates)
  local best, best_score = nil, 0
  local lowered = tostring(name):lower()
  for candidate in pairs(candidates) do
    local other = candidate:lower()
    local common = 0
    for i = 1, math.min(#lowered, #other) do
      if lowered:sub(i, i) == other:sub(i, i) then
        common = common + 1
      else
        break
      end
    end
    local score = common / math.max(#lowered, #other, 1)
    if other:find(lowered, 1, true) or lowered:find(other, 1, true) then
      score = math.max(score, 0.6)
    end
    if score > best_score then
      best, best_score = candidate, score
    end
  end
  if best_score >= 0.45 then
    return best
  end
  return nil
end

local function check_leaf(path, rule, value, errors)
  local kind = rule.type
  local actual = type(value)

  if kind == "any" then
    return
  end
  if kind == "scalar" then
    if actual ~= "string" and actual ~= "number" and actual ~= "boolean" then
      errors[#errors + 1] = ("`%s` must be a string, number or boolean (got %s)"):format(path, actual)
    end
    return
  end
  if kind == "string_or_list" then
    if actual == "string" then
      return
    end
    if is_list(value) then
      for i, item in ipairs(value) do
        if type(item) ~= "string" then
          errors[#errors + 1] = ("`%s[%d]` must be a string (got %s)"):format(path, i, type(item))
        end
      end
      return
    end
    errors[#errors + 1] = ("`%s` must be a string or a list of strings (got %s)"):format(path, actual)
    return
  end
  if kind == "string_list" then
    -- ⚠ A LIST LEAF (spec 15 §2). The validator only types it; the
    -- wholesale-replacement half is `fields.apply_list_leaves`, run after the
    -- deep-extend in both `config.setup` and `ui.setup`.
    if not is_list(value) then
      errors[#errors + 1] = ("`%s` must be a list of strings (got %s)"):format(path, actual)
      return
    end
    for i, item in ipairs(value) do
      if type(item) ~= "string" then
        errors[#errors + 1] = ("`%s[%d]` must be a string (got %s)"):format(path, i, type(item))
      end
    end
    return
  end
  if kind == "string_or_function" then
    if actual ~= "string" and actual ~= "function" then
      errors[#errors + 1] = ("`%s` must be a string or a function (got %s)"):format(path, actual)
    end
    return
  end
  if kind == "formatter" then
    -- `"calendar"` | `{ name = "datetime", format = … }` | function (§4).
    if actual == "string" or actual == "function" then
      return
    end
    if actual == "table" then
      if type(value.name) ~= "string" then
        errors[#errors + 1] = ("`%s.name` must be a string naming a formatter (got %s)"):format(path, type(value.name))
      end
      return
    end
    errors[#errors + 1] =
      ("`%s` must be a formatter name, a { name = … } table, or a function (got %s)"):format(path, actual)
    return
  end
  if kind == "keymap" then
    -- `false` / `""` disable a binding — spec 03 §3 "all rebindable".
    if value == false or value == nil then
      return
    end
    if actual ~= "string" then
      errors[#errors + 1] = ("`%s` must be a string (a key sequence) or false to unbind (got %s)"):format(path, actual)
    end
    return
  end
  if actual ~= kind then
    errors[#errors + 1] = ("`%s` must be a %s (got %s)"):format(path, kind, actual)
    return
  end
  if rule.enum then
    if not vim.tbl_contains(rule.enum, value) then
      errors[#errors + 1] = ("`%s` must be one of %s (got %q)"):format(
        path,
        table.concat(rule.enum, ", "),
        tostring(value)
      )
    end
  end
  if rule.min and value < rule.min then
    errors[#errors + 1] = ("`%s` must be >= %s (got %s)"):format(path, rule.min, tostring(value))
  end
  if rule.max and value > rule.max then
    errors[#errors + 1] = ("`%s` must be <= %s (got %s)"):format(path, rule.max, tostring(value))
  end
end

local function check_node(prefix, node, value, errors)
  if type(value) ~= "table" then
    errors[#errors + 1] = ("`%s` must be a table (got %s)"):format(prefix, type(value))
    return
  end

  -- A section that no longer exists. Every CHILD key is reported with its
  -- replacement, so `ui.display.show_scores = false` names
  -- `ui.organize.show_scores` instead of a bare "unknown key `ui.display`" —
  -- and a key no doc ever shipped still fails, because the section is gone
  -- (spec 15 §10.5(c)+(d)).
  if node.moved_section then
    for key in pairs(value) do
      local path = dotted(prefix, key)
      local moved = M.MOVED_KEYS[path]
      if moved then
        errors[#errors + 1] = ("`%s` is no longer a plugin setting — %s"):format(path, moved)
      else
        errors[#errors + 1] = ("unknown key `%s` — the `%s` section was deleted by spec 15; %s"):format(
          path,
          prefix,
          "per-pane display keys live under `ui.capture.*` and `ui.organize.*`"
        )
      end
    end
    if next(value) == nil then
      errors[#errors + 1] = ("unknown key `%s` — the section was deleted by spec 15; %s"):format(
        prefix,
        "use `ui.capture.*` / `ui.organize.*`"
      )
    end
    return
  end

  if node.free then
    for key, item in pairs(value) do
      local path = dotted(prefix, key)
      if node.known and not node.known[key] then
        local hint = suggest(key, node.known)
        errors[#errors + 1] = ("unknown key `%s`%s"):format(
          path,
          hint and (" — did you mean `" .. dotted(prefix, hint) .. "`?") or ""
        )
      else
        check_leaf(path, node.value or { type = "any" }, item, errors)
      end
    end
    return
  end

  for key, item in pairs(value) do
    local path = dotted(prefix, key)
    local rule = node.fields[key]
    if rule ~= nil and rule.moved_section then
      check_node(path, rule, item, errors)
    elseif rule == nil then
      local moved = M.MOVED_KEYS[path] or M.MOVED_KEYS[tostring(key)]
      if moved then
        errors[#errors + 1] = ("`%s` is no longer a plugin setting — %s"):format(path, moved)
      else
        local known = {}
        for name in pairs(node.fields) do
          known[name] = true
        end
        local hint = suggest(key, known)
        errors[#errors + 1] = ("unknown key `%s`%s"):format(
          path,
          hint and (" — did you mean `" .. dotted(prefix, hint) .. "`?") or ""
        )
      end
    elseif rule.fields or rule.free then
      check_node(path, rule, item, errors)
    else
      check_leaf(path, rule, item, errors)
    end
  end
end

--- Validate a user `setup()` table WITHOUT applying it.
---@param opts table|nil
---@return boolean ok, string|nil error_text
function M.validate(opts)
  if opts == nil then
    return true
  end
  if type(opts) ~= "table" then
    return false, config_error(("setup() takes a table (got %s)"):format(type(opts)))
  end

  local errors = {}
  check_node("", SCHEMA, opts, errors)

  -- A key in BOTH `pinned` and `hidden` is always a mistake, and 09 §1.5
  -- forbids guessing — so there is no precedence rule to remember, only an
  -- error naming the dotted key and the field (spec 15 §2).
  local capture_fields = (((opts.ui or {}).capture or {}).fields) or {}
  if type(capture_fields.pinned) == "table" and type(capture_fields.hidden) == "table" then
    local pinned = {}
    for _, key in ipairs(capture_fields.pinned) do
      pinned[key] = true
    end
    local both = {}
    for _, key in ipairs(capture_fields.hidden) do
      if pinned[key] then
        both[#both + 1] = tostring(key)
      end
    end
    table.sort(both)
    for _, key in ipairs(both) do
      errors[#errors + 1] = ("`ui.capture.fields` lists `%s` in BOTH `pinned` and `hidden` — pick one"):format(key)
    end
  end

  -- The one non-UI value the plugin still owns has a hard OS limit.
  local socket = opts.socket_path or (type(opts.core) == "table" and opts.core.socket_path)
  if type(socket) == "string" and #vim.fn.expand(socket) > M.MAX_SOCKET_PATH then
    errors[#errors + 1] = ("`socket_path` is %d bytes; AF_UNIX allows about %d — the core cannot bind it"):format(
      #vim.fn.expand(socket),
      M.MAX_SOCKET_PATH
    )
  end

  if #errors == 0 then
    return true
  end
  table.sort(errors)
  return false,
    config_error(
      table.concat(errors, "\n  "),
      "spec 10 §3: the nvim setup() table holds UI concerns + the socket path; everything behavioral lives in ~/.config/organize-core/config.toml (see doc/MIGRATION-from-old-setup.md)"
    )
end

---------------------------------------------------------------------------
-- the resolved config
---------------------------------------------------------------------------

M.options = base_defaults()
M._user = {}
M._setup_done = false

--- Apply a user table over the defaults. RAISES on any validation failure —
--- loud failure (spec 09 §1.5); a misconfigured plugin must not start.
---@param opts table|nil
---@return table resolved
function M.setup(opts)
  local ok, err = M.validate(opts)
  if not ok then
    error(err, 0)
  end
  M._user = vim.deepcopy(opts or {})
  local merged = vim.tbl_deep_extend("force", base_defaults(), M._user)
  -- ⚠ `ui.capture.fields.pinned` / `.hidden` are LIST LEAVES. Without this,
  -- `pinned = { "tags" }` over the six-entry default index-merges into
  -- `{ "tags", "summary", "timestamp", "context", "tags", "sources" }`: the
  -- user asked for one row and got six, with `tags` in it twice (spec 15 §2,
  -- pinned by §10.4).
  require("para-organize.ui.fields").apply_list_leaves(merged, M._user)

  -- NORMALISE AT THE BOUNDARY. `socket_path` and `core_cmd` are accepted both
  -- at the top level and inside `core` (core.lua reads either). Both spellings
  -- have a top-level DEFAULT, so without this a user's `core.core_cmd` would
  -- be silently beaten by the default `{"organize","serve"}` — the exact class
  -- of silent-wrong-answer bug spec 09 §1.5 forbids. After this there is
  -- exactly ONE home for each in the resolved table, and `health`, `core` and
  -- `pickers` cannot read different values.
  local nested = type(merged.core) == "table" and merged.core or {}
  for _, key in ipairs({ "socket_path", "core_cmd" }) do
    if M._user[key] == nil and nested[key] ~= nil then
      merged[key] = vim.deepcopy(nested[key])
    end
    nested[key] = nil
  end

  M.options = merged
  M._setup_done = true
  return M.options
end

--- The resolved config. Always a table, even before `setup()` runs (the
--- defaults), so no caller has to branch on "has setup happened yet".
function M.get()
  return M.options
end

--- Exactly what the user passed to `setup()` (for `:ParaOrganize debug`).
function M.user()
  return M._user
end

function M.is_configured()
  return M._setup_done
end

--- Back to shipped defaults. Tests only.
function M.reset()
  M._user = {}
  M.options = base_defaults()
  M._setup_done = false
  return M.options
end

--- The subset `para-organize.core` / `para-organize.rpc` consume: the `core`
--- block plus the two top-level keys (`setup()` has already normalised a
--- nested spelling of those up to the top level).
---@param cfg table|nil defaults to `M.get()`
---@return table
function M.core_options(cfg)
  cfg = cfg or M.get()
  local nested = type(cfg.core) == "table" and cfg.core or {}
  local out = vim.deepcopy(nested)
  for _, key in ipairs({ "socket_path", "core_cmd" }) do
    if cfg[key] ~= nil then
      out[key] = vim.deepcopy(cfg[key])
    elseif out[key] == nil and M.defaults[key] ~= nil then
      out[key] = vim.deepcopy(M.defaults[key])
    end
  end
  if type(out.socket_path) == "string" and out.socket_path ~= "" then
    out.socket_path = vim.fn.expand(out.socket_path)
  end
  if type(out.core_env) == "table" and next(out.core_env) == nil then
    out.core_env = nil
  end
  return out
end

--- The socket path the plugin will actually dial.
function M.socket_path(cfg)
  cfg = cfg or M.get()
  local path = cfg.socket_path
  if type(path) ~= "string" or path == "" then
    path = type(cfg.core) == "table" and cfg.core.socket_path or nil
  end
  if type(path) ~= "string" or path == "" then
    return nil
  end
  return vim.fn.expand(path)
end

return M
