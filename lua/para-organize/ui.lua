--- para-organize.ui — the two-pane organize UI (spec 03 §3).
---
--- THIN CLIENT LAW (spec 10 §1): this module renders and nothing else. It
--- performs no vault reads and no vault writes. The ONE deliberate exception
--- of spec 10 §4 lives here: the LEFT pane is the real capture file's normal
--- Neovim buffer, so Matt edits and `:w`s the actual note. Everything this
--- module draws around that buffer (session position, tags, metadata
--- summary, merge instructions) is EXTMARK VIRTUAL TEXT — never buffer
--- lines, which could be written into the vault by that same `:w`.
---
--- Public API (module contract):
---   setup(config) / config()
---   mount(state[, opts]) / unmount() / refresh(state) / current_bufs()
---   focus(pane) / show_help(entries) / is_mounted() / merge_content()
local M = {}

local render = require("para-organize.ui.render")
local fields = require("para-organize.ui.fields")

local AUGROUP = "ParaOrganizeUI"
local ORGANIZE_BUFNAME = "para-organize://organize"
local CAPTURE_PLACEHOLDER_BUFNAME = "para-organize://capture"
local HELP_BUFNAME = "para-organize://help"

local NS_ORGANIZE = vim.api.nvim_create_namespace("para_organize_organize")
local NS_CAPTURE = vim.api.nvim_create_namespace("para_organize_capture")

--- UI-only defaults. Per spec 10 §3 the nvim `setup()` table keeps ONLY UI
--- concerns (layout, icons, highlights, keymaps) plus the socket path;
--- everything behavioural lives in the core config. The integrator-owned
--- `para-organize.config` is authoritative when present — these are the
--- fallbacks so ui/actions are testable and never crash on a partial table.
M.DEFAULTS = {
  ui = {
    layout = "float", -- "float" | "split"
    float_opts = { width = 0.8, height = 0.8, border = "rounded" },
    -- Spec 03 binds single-letter actions in BOTH panes ("keep s=skip
    -- globally", "<Esc> closes from either pane"). That shadows `a`/`s`/`p`
    -- in the editable capture buffer, so this key lets Matt trade parity for
    -- an untouched capture buffer: "core" (spec parity, default) |
    -- "navigation" (only pane/session navigation) | "none".
    capture_pane_keymaps = "core",
    auto_move_to_new_folder = false,
    -- Spec 03 §6 closes the UI when the session is exhausted; tests and
    -- "review the counts" workflows turn this off.
    close_on_complete = true,
    -- Spec 15 §7. `ui.display.*` is DELETED as a section (ruling R1); every
    -- one of its keys has a new home here or in `ui.organize`, and
    -- `config.MOVED_KEYS` names the replacement for each.
    --
    -- ⚠ `capture.fields.pinned` and `capture.fields.hidden` are LIST LEAVES:
    -- a user list REPLACES them wholesale (`fields.apply_list_leaves`), never
    -- merges by index.
    capture = {
      show_position = true, -- "Capture 3 of 47"
      mode = "compact", -- "compact" | "full" | "raw" at session start
      frontmatter = "fold", -- "fold" | "none" (spec 15 §3)
      -- foldtext = nil,            -- nil = built-in; string | function(ctx)
      max_card_lines = 40,
      render_budget_ms = 50,
      -- render = nil,              -- function(ctx) -> virt-lines (spec 15 §5)
      fields = {
        pinned = { "title", "summary", "timestamp", "context", "tags", "sources" },
        hidden = {
          "location",
          "processing_status",
          "created_date",
          "last_edited_date",
          "id",
          "aliases",
          "capture_id",
          "modalities",
          "metadata",
        },
        pin_metadata_fields = true,
        show_empty_pinned = false,
        show_rest_keys = true,
        labels = {},
      },
      formatters = {
        timestamp = "calendar",
        created_date = "calendar",
        last_edited_date = "calendar",
        tags = "tags",
      },
    },
    organize = {
      show_scores = true,
      show_reasons = true,
      max_reasons = 3, -- 0 = all
      score_thresholds = { high = 2.0, medium = 1.0 },
      -- render_row = nil,          -- function(row_ctx) (spec 15 §6)
      -- Leaves of this CLOSED record whose behaviour and documented meaning
      -- belong to doc 16 §3.x; declared here only because ruling R2 makes
      -- doc 15 land the record first, so 16 fills leaves that already exist.
      show_progress = true,
      numeric_accept = true,
      preview_notes = 5,
      preview_debounce_ms = 120,
    },
    -- ⚠ These name the plugin's OWN groups, not the stock ones, so that the
    -- `ParaOrganize*` hook spec 14 §5 advertises is what actually renders.
    -- Each is `default`-linked to a stock group in `M.HL_GROUPS`, so the
    -- out-of-the-box colors are unchanged — but a colorscheme (or the user,
    -- via `ui.highlights.selected = "IncSearch"`) can now take them over.
    highlights = {
      selected = "ParaOrganizeSelected",
      header = "ParaOrganizeHeader",
      reason = "ParaOrganizeReason",
      score_high = "ParaOrganizeScoreHigh",
      score_medium = "ParaOrganizeScoreMedium",
      score_low = "ParaOrganizeScoreLow",
      hint = "ParaOrganizeHint",
    },
    -- Empty by default so spec 03's literal `[P]`/`[A]`/`[R]`/`[🗑]` markers
    -- are what render; set e.g. `icons.project = " "` to override.
    icons = {},
    -- Window-local options merged over `M.PANE_WIN_OPTIONS` and applied to
    -- BOTH panes at mount. Free map: any window option name is honored.
    win_options = {},
  },
  keymaps = {
    buffer = {
      accept = "<CR>",
      cancel = "<Esc>",
      next = "<Tab>",
      prev = "<S-Tab>",
      skip = "s",
      sort_cycle = "S", -- spec 03 resolution of the old s/s double-binding
      archive = "a",
      merge = "m",
      search = "/",
      refresh = "r",
      toggle_preview = "p",
      help = "?",
      next_suggestion = "<A-j>",
      prev_suggestion = "<A-k>",
      focus_capture = "<C-h>",
      focus_organize = "<C-l>",
      back = "<BS>",
      new_project = "<leader>np",
      new_area = "<leader>na",
      new_resource = "<leader>nr",
      merge_complete = "<leader>mc",
      merge_cancel = "<leader>mx",
      -- Spec 12 §1's edit modes and review gate. Listed here (and not only in
      -- `actions.CORE_KEYS`) because `config.lua` derives its closed set of
      -- rebindable keymap names from exactly this table — a row missing here
      -- is a key an operator cannot rebind, which is the "every config key is
      -- honored or deleted" rule read from the other direction.
      integrate = "<leader>mi",
      integrate_mode = "<leader>mm",
      integrate_edit = "e",
      -- Spec 15 §2: compact → full → raw. `zi`'s native meaning (toggle
      -- `foldenable`) is the closest vim idiom to what this key does, and it
      -- binds in the ORGANIZE pane only (ruling R7) so `z` is not a prefix in
      -- the capture pane at all — `zo`/`za` there stay instant.
      cycle_fields = "zi",
    },
  },
}

M._ui = nil
M._config = nil

-- --- config ----------------------------------------------------------------

local function integrator_config()
  local ok, mod = pcall(require, "para-organize.config")
  if not ok or type(mod) ~= "table" then
    return nil
  end
  if type(mod.get) == "function" then
    local got, value = pcall(mod.get)
    if got and type(value) == "table" then
      return value
    end
  end
  for _, key in ipairs({ "options", "values", "current" }) do
    if type(mod[key]) == "table" then
      return mod[key]
    end
  end
  return nil
end

--- Merge a user/integrator config over the UI defaults.
---
--- ⚠ `fields.apply_list_leaves` runs after EACH deep-extend. `pinned` and
--- `hidden` are lists, and `vim.tbl_deep_extend` merges array-like tables BY
--- INDEX — `pinned = { "tags" }` over the six-entry default would otherwise
--- yield six rows with `tags` in twice (spec 15 §2, pinned by §10.4).
--- Every `ParaOrganize*` group the plugin can emit, and the stock group each
--- one falls back to. Spec 14 §5 lists these as a STABLE public hook for
--- colorscheme authors; before this table they were NAMED in `render.lua`'s
--- fallbacks and defined nowhere, so styling `ParaOrganizeScoreHigh` in a
--- colorscheme changed nothing (measured: `nvim_get_hl` returned an empty
--- dict, indistinguishable from a group that does not exist).
---
--- ⚠ `default = true` is load-bearing: a colorscheme that defines any of
--- these WINS, and we only fill in the ones it left alone.
M.HL_GROUPS = {
  ParaOrganizeSelected = "Visual",
  ParaOrganizeHeader = "Title",
  ParaOrganizeReason = "Comment",
  ParaOrganizeHint = "Comment",
  ParaOrganizeScoreHigh = "DiagnosticOk",
  ParaOrganizeScoreMedium = "DiagnosticWarn",
  ParaOrganizeScoreLow = "Comment",
}

--- Define the fallback groups. Idempotent, so `setup()` may call it freely.
--- Re-run on `ColorScheme` because `:colorscheme` issues `:hi clear`, which
--- wipes even `default` links — without the autocmd the panes would lose
--- their colors the first time the user switched theme mid-session.
function M.apply_highlights()
  for name, link in pairs(M.HL_GROUPS) do
    pcall(vim.api.nvim_set_hl, 0, name, { default = true, link = link })
  end
end

local function install_highlight_autocmd()
  if M._hl_autocmd then
    return
  end
  local ok, group = pcall(vim.api.nvim_create_augroup, "ParaOrganizeHighlights", { clear = true })
  if not ok then
    return
  end
  local ok_au, id = pcall(vim.api.nvim_create_autocmd, "ColorScheme", {
    group = group,
    pattern = "*",
    callback = function()
      M.apply_highlights()
    end,
  })
  M._hl_autocmd = ok_au and id or nil
end

function M.setup(user_config)
  M.apply_highlights()
  install_highlight_autocmd()
  local base = vim.deepcopy(M.DEFAULTS)
  local merged = base
  local from_integrator = integrator_config()
  if type(from_integrator) == "table" then
    merged = vim.tbl_deep_extend("force", merged, from_integrator)
    fields.apply_list_leaves(merged, from_integrator)
  end
  if type(user_config) == "table" then
    merged = vim.tbl_deep_extend("force", merged, user_config)
    fields.apply_list_leaves(merged, user_config)
  end
  M._config = merged
  return merged
end

--- The effective UI config (lazily initialised).
function M.config()
  if not M._config then
    M.setup(nil)
  end
  return M._config
end

-- --- buffers ---------------------------------------------------------------

local function find_buf_by_name(name)
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buf) and vim.api.nvim_buf_get_name(buf) == name then
      return buf
    end
  end
  return nil
end

local function scratch_buffer(name)
  local existing = find_buf_by_name(name)
  if existing and vim.api.nvim_buf_is_valid(existing) then
    return existing
  end
  local buf = vim.api.nvim_create_buf(false, true)
  pcall(vim.api.nvim_buf_set_name, buf, name)
  vim.bo[buf].buftype = "nofile"
  vim.bo[buf].bufhidden = "hide"
  vim.bo[buf].swapfile = false
  vim.bo[buf].filetype = "markdown"
  return buf
end

--- The spec 10 §4 exception: a REAL file buffer for the capture. We never
--- read or write the file ourselves — `bufadd`/`bufload` is ordinary Neovim
--- buffer editing, and `:w` from this buffer is the only sanctioned client
--- write in the whole plugin.
local function capture_buffer(path)
  if type(path) ~= "string" or path == "" then
    return nil, false
  end
  local existed = find_buf_by_name(path) ~= nil
  local ok, buf = pcall(vim.fn.bufadd, path)
  if not ok or not buf or buf == 0 then
    return nil, false
  end
  pcall(vim.fn.bufload, buf)
  if vim.api.nvim_buf_is_valid(buf) then
    pcall(function()
      vim.bo[buf].swapfile = false
      if vim.bo[buf].filetype == "" then
        vim.bo[buf].filetype = "markdown"
      end
    end)
  end
  return buf, not existed
end

-- --- mounting --------------------------------------------------------------

local function pct(value, fallback)
  local number = tonumber(value) or fallback
  if number <= 1 then
    number = number * 100
  end
  return ("%d%%"):format(math.floor(number + 0.5))
end

local function mount_float(cfg, capture_buf, organize_buf)
  local ok_popup, Popup = pcall(require, "nui.popup")
  local ok_layout, Layout = pcall(require, "nui.layout")
  if not (ok_popup and ok_layout) then
    return nil, "nui.nvim is required for the organize UI"
  end
  local float = (cfg.ui and cfg.ui.float_opts) or {}
  local border = float.border or "rounded"

  local left = Popup({
    bufnr = capture_buf,
    enter = false,
    focusable = true,
    border = { style = border, text = { top = " Capture ", top_align = "center" } },
    buf_options = {},
    win_options = { cursorline = true, wrap = true, number = false, signcolumn = "no" },
  })
  local right = Popup({
    bufnr = organize_buf,
    enter = false,
    focusable = true,
    border = { style = border, text = { top = " Organize ", top_align = "center" } },
    buf_options = {},
    win_options = { cursorline = true, wrap = false, number = false, signcolumn = "no" },
  })

  local layout = Layout({
    relative = "editor",
    position = "50%",
    size = { width = pct(float.width, 0.8), height = pct(float.height, 0.8) },
  }, Layout.Box({
    Layout.Box(left, { size = "50%" }),
    Layout.Box(right, { size = "50%" }),
  }, { dir = "row" }))

  local ok, err = pcall(function()
    layout:mount()
  end)
  if not ok then
    return nil, tostring(err)
  end
  return { mode = "float", layout = layout, left = left, right = right, capture_win = left.winid, organize_win = right.winid }
end

--- Window-local options forced on BOTH panes at mount.
---
--- Neither `mount_float` nor `mount_split` used to touch the fold options, so
--- the panes inherited the user's globals — and with the common markdown setup
--- (`foldmethod=expr` + `nvim_treesitter#foldexpr()`) both panes opened
--- FOLDED. That is the one thing this UI must never do: the capture pane
--- exists to be read at a glance, and the organize pane's rows ARE the choice
--- being made. `foldmethod=manual` (not just `foldenable=false`) so a later
--- `zx`/`foldenable` flip from another plugin cannot re-apply the expr folds.
M.PANE_WIN_OPTIONS = {
  foldenable = false,
  foldmethod = "manual",
  foldlevel = 99,
  foldcolumn = "0",
}

--- Apply `PANE_WIN_OPTIONS`, then the user's `ui.win_options` over them, to
--- both panes. A user who wants folds back writes
--- `ui = { win_options = { foldenable = true } }`; every other window-local
--- option is settable through the same door.
local function apply_win_options(handles, cfg)
  local overrides = (cfg.ui and cfg.ui.win_options) or {}
  local options = vim.tbl_extend("force", vim.deepcopy(M.PANE_WIN_OPTIONS), overrides)
  for _, win in ipairs({ handles.capture_win, handles.organize_win }) do
    if win and vim.api.nvim_win_is_valid(win) then
      for name, value in pairs(options) do
        pcall(vim.api.nvim_set_option_value, name, value, { win = win })
      end
      -- ⚠ MEASURED, and the reason `foldmethod = "manual"` alone does NOT
      -- make spec 15 §1's claim ("the only folds that can exist are the ones
      -- this plugin creates") true: switching `foldmethod` from the user's
      -- `expr` to `manual` KEEPS the folds the expr method had already
      -- computed, converted into manual ones. With `foldenable = false` they
      -- are merely invisible — which is why 1686de7 measured clean — but §3
      -- turns `foldenable` back ON in the capture window, and the inherited
      -- whole-buffer fold then swallows the frontmatter fold, the card and
      -- the note: Matt item 5, exactly, arriving through the item 6 fix.
      --
      -- `zE` is safe HERE and nowhere else in this module: both mount paths
      -- create BRAND-NEW windows, so every fold present at this instant was
      -- computed from the user's globals microseconds ago. A fold the user
      -- built with `zf` lives in the user's own window, which this loop never
      -- touches — and teardown still uses a guarded `zd`, never `zE`
      -- (spec 15 §3, §10.13).
      pcall(vim.api.nvim_win_call, win, function()
        local view = vim.fn.winsaveview()
        pcall(vim.cmd, "normal! zE")
        pcall(vim.fn.winrestview, view)
      end)
    end
  end
end

local function mount_split(capture_buf, organize_buf)
  local ok, result = pcall(function()
    local capture_win = vim.api.nvim_open_win(capture_buf, true, { split = "right", win = 0 })
    local organize_win = vim.api.nvim_open_win(organize_buf, false, { split = "right", win = capture_win })
    for _, win in ipairs({ capture_win, organize_win }) do
      vim.wo[win].cursorline = true
      vim.wo[win].number = false
    end
    return { mode = "split", capture_win = capture_win, organize_win = organize_win }
  end)
  if not ok then
    return nil, tostring(result)
  end
  return result
end

--- Mount the two panes for `state`.
---@param state table session state (spec 03 §3)
---@param opts table|nil { config = table }
---@return table|nil handles
function M.mount(state, opts)
  opts = opts or {}
  if opts.config then
    M.setup(opts.config)
  end
  local cfg = M.config()
  if M._ui then
    M.unmount()
  end

  state = state or {}
  local record = (state.captures or {})[state.current or 1]
  local capture_buf, owned = capture_buffer(record and record.path)
  local owned_bufs = {}
  if not capture_buf then
    capture_buf = scratch_buffer(CAPTURE_PLACEHOLDER_BUFNAME)
    owned_bufs[capture_buf] = true
  elseif owned then
    owned_bufs[capture_buf] = true
  end
  local organize_buf = scratch_buffer(ORGANIZE_BUFNAME)
  owned_bufs[organize_buf] = true

  local handles, err
  if (cfg.ui and cfg.ui.layout) == "split" then
    handles, err = mount_split(capture_buf, organize_buf)
  else
    handles, err = mount_float(cfg, capture_buf, organize_buf)
  end
  if not handles then
    vim.notify("para-organize: cannot open the organize UI: " .. tostring(err), vim.log.levels.ERROR)
    return nil
  end

  handles.capture_buf = capture_buf
  handles.organize_buf = organize_buf
  handles.owned_bufs = owned_bufs
  handles.map = {}
  handles.bound = opts.bind ~= false
  -- Spec 15 §2: the field mode is initialised from `ui.capture.mode` at
  -- session start, sticky for the whole session, never persisted.
  handles.field_mode = ((cfg.ui or {}).capture or {}).mode or "compact"
  -- Everything the plugin overwrites in the capture window, so teardown can
  -- put a buffer the plugin did NOT open back exactly as it found it.
  handles.saved_win_options = {}
  if handles.capture_win and vim.api.nvim_win_is_valid(handles.capture_win) then
    for _, name in ipairs({ "foldenable", "foldmethod", "foldlevel", "foldminlines", "foldtext", "fillchars", "foldcolumn" }) do
      local ok_opt, value = pcall(vim.api.nvim_get_option_value, name, { win = handles.capture_win })
      if ok_opt then
        handles.saved_win_options[name] = value
      end
    end
  end
  apply_win_options(handles, cfg)
  fields.reset_session()
  M._ui = handles

  M._install_autocmds()
  M.refresh(state)

  local ok, actions = pcall(require, "para-organize.actions")
  if ok and type(actions.attach) == "function" and opts.bind ~= false then
    pcall(actions.attach, M.current_bufs())
  end

  if vim.api.nvim_win_is_valid(handles.organize_win) then
    pcall(vim.api.nvim_set_current_win, handles.organize_win)
  end
  return handles
end

function M.is_mounted()
  return M._ui ~= nil
end

function M.current_bufs()
  if not M._ui then
    return {}
  end
  return { capture = M._ui.capture_buf, organize = M._ui.organize_buf, help = M._ui.help_buf }
end

function M.current_wins()
  if not M._ui then
    return {}
  end
  return { capture = M._ui.capture_win, organize = M._ui.organize_win, help = M._ui.help_win }
end

-- --- autocmds --------------------------------------------------------------

function M._install_autocmds()
  local group = vim.api.nvim_create_augroup(AUGROUP, { clear = true })

  -- `:w` in the organize pane. The pane is `acwrite` exactly while it holds
  -- editable text, so this fires instead of E382, and it dispatches on the
  -- VIEW rather than on what the text looks like (the 08 §A19 discipline).
  -- Saving is what a person does when they mean "apply what I just wrote".
  -- Buffer-scoped ONLY. A nil `buffer` here would register a GLOBAL
  -- `BufWriteCmd` and hijack `:w` for every buffer in the editor.
  local organize_buf = M._ui and M._ui.organize_buf
  if organize_buf and vim.api.nvim_buf_is_valid(organize_buf) then
    vim.api.nvim_create_autocmd("BufWriteCmd", {
      group = group,
      buffer = organize_buf,
      callback = function()
        local ui = M._ui
        if not ui then
          return
        end
        vim.bo[ui.organize_buf].modified = false
        local ok, actions = pcall(require, "para-organize.actions")
        if not ok then
          return
        end
        -- `merge_complete` already splits on the view: in `merge` it commits
        -- through `op.merge_commit`, in `integrate` it carries the review
        -- gate's accept verdict. `:w` means the same thing in both — "apply
        -- what I just wrote" — so one call covers both editable views.
        if ui.view == "merge" or ui.view == "integrate" then
          actions.merge_complete()
        else
          vim.notify(
            "para-organize: nothing to save here — this pane is a view of the vault, not a file. "
              .. "Press ? for the keys that change it.",
            vim.log.levels.INFO
          )
        end
      end,
    })
  end
  vim.api.nvim_create_autocmd("WinClosed", {
    group = group,
    callback = function(event)
      local win = tonumber(event.match)
      local ui = M._ui
      if not (ui and win) then
        return
      end
      if win == ui.capture_win or win == ui.organize_win then
        -- Closing either window tears the whole SESSION down cleanly, not
        -- just the windows (spec 03 §3 "Closing either window (WinClosed)
        -- tears the session down cleanly"; spec 09 §2 "teardown on every exit
        -- path (WinClosed, <Esc>, stop)").
        --
        -- `M.unmount()` alone left the state machine ACTIVE: `:q` on either
        -- pane produced a session with no UI that still dispatched
        -- vault-mutating commands, and an in-flight `op.move` reply still
        -- advanced it. Routing through `actions.quit()` — the same function
        -- `<Esc>` calls — unmounts AND discards the session. It is safe to
        -- re-enter: `M._ui` is already nil by then, so `unmount` is a no-op,
        -- and `teardown()` does nothing when no session is live.
        vim.schedule(function()
          M.unmount()
          local ok, actions = pcall(require, "para-organize.actions")
          if ok and type(actions.quit) == "function" then
            pcall(actions.quit)
          end
        end)
      elseif win == ui.help_win then
        vim.schedule(function()
          M.close_help()
        end)
      end
    end,
  })
  vim.api.nvim_create_autocmd("VimLeavePre", {
    group = group,
    callback = function()
      M.unmount()
    end,
  })
  M._install_capture_autocmds(M._ui and M._ui.capture_buf)
end

--- Buffer-local autocmds on the REAL capture buffer (spec 15 §3's failure
--- table). All of them live in the `ParaOrganizeUI` augroup, so teardown's
--- single `nvim_del_augroup_by_name` removes every one — leaving a live
--- `TextChanged` autocmd on someone else's buffer after `stop` is Matt item 5
--- arriving from the other end (§10.13).
function M._install_capture_autocmds(buf)
  local ui = M._ui
  if not (ui and buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  for _, id in ipairs(ui._capture_autocmds or {}) do
    pcall(vim.api.nvim_del_autocmd, id)
  end
  ui._capture_autocmds = {}
  local ok_group, group = pcall(vim.api.nvim_create_augroup, AUGROUP, { clear = false })
  if not ok_group then
    return
  end

  local function redraw()
    local live = M._ui
    if live and live.state then
      M.refresh(live.state)
    end
  end

  --- ⚠ The FOLD is recomputed on `BufReadPost`/`BufWritePost` ONLY, never on
  --- `TextChanged`: manual folds self-extend as lines are inserted inside
  --- them, and re-closing a fold mid-insert is a hostile edit experience that
  --- would contradict the never-re-close-a-user-opened-fold rule. Only the
  --- CARD re-renders here, debounced.
  --- The pending debounce timer lives on the UI handle rather than in a
  --- closure upvalue for two reasons: `unmount` must CLOSE it (a live libuv
  --- handle keeps the loop alive and leaks across specs), and
  --- `_flush_capture_debounce` must be able to fire it synchronously.
  local function stop_timer()
    local t = ui._capture_timer
    ui._capture_timer = nil
    if t then
      pcall(function()
        t:stop()
        t:close()
      end)
    end
  end

  local function debounce()
    local delay = 150
    stop_timer()
    local timer = (vim.uv or vim.loop).new_timer()
    if not timer then
      return redraw()
    end
    ui._capture_timer = timer
    timer:start(
      delay,
      0,
      vim.schedule_wrap(function()
        stop_timer()
        redraw()
      end)
    )
  end

  --- TEST SEAM — run a pending debounced re-render NOW, skipping the timer.
  --- Lets a spec assert the post-debounce state without spending the 150ms
  --- of wall clock, and without depending on timer scheduling at all.
  ui.flush_capture_debounce = function()
    if ui._capture_timer then
      stop_timer()
      redraw()
      return true
    end
    return false
  end

  local ids = ui._capture_autocmds
  ids[#ids + 1] = vim.api.nvim_create_autocmd({ "TextChanged", "TextChangedI" }, {
    group = group,
    buffer = buf,
    callback = function()
      local live = M._ui
      if not live then
        return
      end
      -- Values in the card come from the last `note.get`; between the edit
      -- and `:w` the card says so rather than showing values it can no
      -- longer vouch for.
      live._capture_dirty = true
      debounce()
    end,
  })

  ids[#ids + 1] = vim.api.nvim_create_autocmd("BufWritePost", {
    group = group,
    buffer = buf,
    callback = function()
      local live = M._ui
      if not live then
        return
      end
      live._capture_dirty = false
      live.fold = nil -- recompute the region from the written bytes
      -- Re-issue `note.get` through the action layer, which owns the RPC
      -- client. Skipped while an EDITABLE organize view is open, so a `:w` of
      -- the capture cannot discard a half-written merge.
      local view = live.view
      if view ~= "merge" and view ~= "integrate" then
        local ok, actions = pcall(require, "para-organize.actions")
        if ok and type(actions.load_current) == "function" then
          pcall(actions.load_current)
        end
      end
      redraw()
    end,
  })

  ids[#ids + 1] = vim.api.nvim_create_autocmd("BufReadPost", {
    group = group,
    buffer = buf,
    callback = function()
      local live = M._ui
      if not live then
        return
      end
      -- MEASURED: a reload destroys both the extmarks and the manual fold
      -- (doc/FEEDBACK-EVIDENCE-2026-08-16.md §4b). This is the re-apply that
      -- fact makes load-bearing.
      live._capture_dirty = false
      live.fold = nil
      vim.schedule(redraw)
    end,
  })
end

-- --- rendering -------------------------------------------------------------

local function set_lines(buf, lines)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  local was_modifiable = vim.bo[buf].modifiable
  vim.bo[buf].modifiable = true
  vim.api.nvim_buf_set_lines(buf, 0, -1, false, lines)
  vim.bo[buf].modifiable = was_modifiable
  -- The organize pane is `acwrite` while the merge editor is open (so `:w`
  -- reaches `BufWriteCmd` instead of failing E382). An `acwrite` buffer that
  -- believes it is modified blocks `:q` with E37 — and every render here is
  -- the PLUGIN writing, never the user, so nothing here is unsaved work.
  vim.bo[buf].modified = false
end

local function apply_marks(buf, marks, lines)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  vim.api.nvim_buf_clear_namespace(buf, NS_ORGANIZE, 0, -1)
  for _, mark in ipairs(marks or {}) do
    local row = mark.line - 1
    local text = lines[mark.line] or ""
    local end_col = mark.end_col
    if end_col == nil or end_col < 0 then
      end_col = #text
    end
    end_col = math.min(end_col, #text)
    local col = math.min(mark.col or 0, end_col)
    pcall(vim.api.nvim_buf_set_extmark, buf, NS_ORGANIZE, row, col, {
      end_row = row,
      end_col = end_col,
      hl_group = mark.hl,
      hl_eol = true,
    })
  end
end

-- --- the capture card + the frontmatter fold (spec 15 §2/§3) ---------------

--- The lhs `cycle_fields` is ACTUALLY bound to, resolved at DRAW TIME through
--- the live keymap table so a rebind reads back verbatim in the card hint and
--- in the foldtext. ⚠ Neither may ever contain a hardcoded key literal
--- (spec 15 §3.4); `nil` means the user unbound it, and the hint is dropped
--- rather than pointing at a key that does nothing.
---@return string|nil
function M.cycle_key()
  local ok, actions = pcall(require, "para-organize.actions")
  if ok and type(actions.keymap_table) == "function" then
    local got, entries = pcall(actions.keymap_table)
    if got and type(entries) == "table" then
      for _, entry in ipairs(entries) do
        if entry.name == "cycle_fields" then
          return entry.lhs
        end
      end
      -- The row is in the table only while it is bound; an explicit `""`
      -- removes it, and that is the "unbound" answer.
      return nil
    end
  end
  local lhs = ((M.config().keymaps or {}).buffer or {}).cycle_fields
  if type(lhs) == "string" and lhs ~= "" then
    return lhs
  end
  return nil
end

--- `foldtext` for the ONE fold this plugin creates. Wired as the window-local
--- option `v:lua.require'para-organize.ui'.foldtext()`.
function M.foldtext()
  local ui = M._ui
  local fold = ui and ui.fold or nil
  return fields.foldtext({
    config = M.config(),
    -- The field count comes from the `note.get` payload, so an unparseable
    -- file can never produce a lying count (spec 15 §3.4).
    count = (fold and fold.count) or 0,
    cycle_key = M.cycle_key(),
    lines = (vim.v.foldend or 0) - (vim.v.foldstart or 0) + 1,
  })
end

--- REGION DETECTION IS A DELIMITER SCAN, NEVER A YAML PARSE (spec 15 §3.1).
--- The client parses nothing: values come from `note.get`'s frontmatter dict.
---@return integer|nil close_line 1-based line of the closing delimiter
local function frontmatter_close(buf)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return nil
  end
  local lines = vim.api.nvim_buf_get_lines(buf, 0, -1, false)
  if (lines[1] or "") ~= "---" then
    return nil
  end
  for lnum = 2, #lines do
    local text = lines[lnum]
    if text == "---" or text == "..." then
      return lnum
    end
  end
  return nil
end

local function fold_closed(win)
  if not (win and vim.api.nvim_win_is_valid(win)) then
    return -1
  end
  local ok, result = pcall(vim.api.nvim_win_call, win, function()
    return vim.fn.foldclosed(1)
  end)
  return ok and result or -1
end

local function in_win(win, fn)
  if not (win and vim.api.nvim_win_is_valid(win)) then
    return
  end
  pcall(vim.api.nvim_win_call, win, fn)
end

--- Remove the plugin's frontmatter fold.
---
--- ⚠ `zd` on line 1, NEVER `zE`: `zE` eliminates every fold in the window,
--- including ones the user built by hand with `zf` (spec 15 §3, obligation
--- §10.13). `owned` skips the closed-fold guard for the one case where the
--- plugin knows the fold is its own — a capture advance, where a manual fold
--- would otherwise survive the buffer swap because folds are window-local.
local function remove_fold(win, owned)
  in_win(win, function()
    if not owned and vim.fn.foldclosed(1) == -1 then
      return
    end
    if vim.fn.foldlevel(1) == 0 then
      return
    end
    local view = vim.fn.winsaveview()
    pcall(vim.api.nvim_win_set_cursor, 0, { 1, 0 })
    pcall(vim.cmd, "normal! zd")
    pcall(vim.fn.winrestview, view)
  end)
end

--- Collapse the raw frontmatter with a window-local MANUAL fold.
---
--- ⚠ This is the ONE place `foldenable` is turned back on (spec 15 §1's
--- table and §3 step 3). It is safe only because `foldmethod` is `manual`:
--- the sole foldable region in this window is the one created here.
local function create_fold(win, close_line)
  in_win(win, function()
    pcall(vim.api.nvim_set_option_value, "foldmethod", "manual", { win = win })
    pcall(vim.api.nvim_set_option_value, "foldenable", true, { win = win })
    pcall(vim.api.nvim_set_option_value, "foldlevel", 0, { win = win })
    pcall(vim.api.nvim_set_option_value, "foldminlines", 0, { win = win })
    pcall(
      vim.api.nvim_set_option_value,
      "foldtext",
      "v:lua.require'para-organize.ui'.foldtext()",
      { win = win }
    )
    local ok, fcs = pcall(vim.api.nvim_get_option_value, "fillchars", { win = win })
    if ok and not tostring(fcs):find("fold:", 1, true) then
      pcall(vim.api.nvim_set_option_value, "fillchars", (fcs ~= "" and fcs .. "," or "") .. "fold: ", { win = win })
    end
    local view = vim.fn.winsaveview()
    pcall(vim.cmd, ("%d,%dfold"):format(1, close_line))
    pcall(vim.fn.winrestview, view)
  end)
end

local function set_fold_closed(win, closed)
  in_win(win, function()
    local view = vim.fn.winsaveview()
    pcall(vim.api.nvim_win_set_cursor, 0, { 1, 0 })
    pcall(vim.cmd, closed and "normal! zc" or "normal! zo")
    pcall(vim.fn.winrestview, view)
  end)
end

--- Apply / recompute the frontmatter fold for the current capture.
---@return integer|nil close_line
local function apply_frontmatter_fold(ctx)
  local ui = M._ui
  local buf, win = ui.capture_buf, ui.capture_win
  local capture_cfg = ((ctx.config.ui or {}).capture) or {}

  -- `"none"` leaves the YAML visible and creates no fold; an UNPARSEABLE file
  -- gets no fold either, because hiding the broken YAML would hide the thing
  -- that needs fixing (spec 15 §3 failure table).
  local wanted = capture_cfg.frontmatter ~= "none" and ctx.parse_error ~= true
  local close_line = wanted and frontmatter_close(buf) or nil

  if not close_line then
    if ui.fold then
      remove_fold(win, true)
      ui.fold = nil
    end
    return nil
  end

  local rec = ui.fold
  if not (rec and rec.buf == buf) then
    remove_fold(win, true)
    create_fold(win, close_line)
    rec = { buf = buf, state = "closed" }
    ui.fold = rec
  end
  rec.close_line = close_line
  rec.count = vim.tbl_count(ctx.frontmatter or {})

  -- Who opened it? A USER-opened fold (`zo`, `za`, `foldopen` as the cursor
  -- enters) is never re-closed — fighting the user's `zo` is a bug. The fold
  -- `raw` opens is the PLUGIN's, and the `zi` that leaves `raw` re-closes it,
  -- because a closed frontmatter block is the whole difference between `raw`
  -- and the two modes either side of it (spec 15 §3).
  if fold_closed(win) == -1 then
    if rec.state == "closed" then
      rec.state = "user_open"
    end
  elseif rec.state == "user_open" then
    rec.state = "closed"
  end

  if ctx.mode == "raw" then
    if fold_closed(win) ~= -1 then
      set_fold_closed(win, false)
      rec.state = "plugin_open"
    end
  elseif rec.state == "plugin_open" then
    set_fold_closed(win, true)
    rec.state = "closed"
  end
  return close_line
end

--- The `ui.capture.render` contract of spec 15 §5, built once per draw and
--- handed unchanged to the built-in card and to any user override.
local function capture_context(state, cfg)
  local ui = M._ui
  local captures = state.captures or {}
  local index = state.current or 1
  local record = captures[index]
  local capture_cfg = (cfg.ui or {}).capture or {}
  local meta_fields = state.meta_fields or {}
  local mode = (ui and ui.field_mode) or capture_cfg.mode or "compact"
  local frontmatter = record and record.frontmatter or nil
  if frontmatter == vim.NIL then
    frontmatter = nil
  end
  local width = 80
  if ui and ui.capture_win and vim.api.nvim_win_is_valid(ui.capture_win) then
    width = vim.api.nvim_win_get_width(ui.capture_win)
  end

  local ctx = {
    record = record,
    frontmatter = frontmatter,
    parse_error = record and record.parse_error == true or false,
    index = index,
    total = #captures,
    mode = mode,
    meta_fields = meta_fields,
    width = width,
    config = cfg,
    cycle_key = M.cycle_key(),
    dirty = ui and ui._capture_dirty == true or false,
  }
  ctx.fields = fields.classify(frontmatter, capture_cfg, meta_fields)
  ctx.format = function(key, value)
    return fields.format(key, value, {
      capture_config = capture_cfg,
      meta_fields = meta_fields,
      record = record,
      frontmatter = frontmatter,
      mode = mode,
      width = width,
      config = cfg,
    })
  end
  return ctx
end

--- Draw the capture card as EXTMARK VIRTUAL LINES around the real note.
---
--- ⚠ ANCHORING (ruling R13). The card is anchored to the first buffer line
--- AFTER the frontmatter close delimiter, with `virt_lines_above = true` —
--- never to `(0,0)`. Virtual lines attached to a line inside a CLOSED fold
--- are never drawn (Neovim forces `w_topfill = 0` over a closed fold), so a
--- card at `(0,0)` under the shipped `frontmatter = "fold"` renders NOTHING
--- while every extmark-count assertion still passes. When the card does
--- anchor to line 1 (no frontmatter, `frontmatter = "none"`, or `raw`) the
--- window fill must be re-established after EVERY draw, because `topfill` is
--- 0 after `:edit`, after `nvim_open_win` and after ordinary cursor motion.
local function draw_capture(state, cfg)
  local ui = M._ui
  local buf, win = ui.capture_buf, ui.capture_win
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  vim.api.nvim_buf_clear_namespace(buf, NS_CAPTURE, 0, -1)

  local ctx = capture_context(state, cfg)
  local close_line = apply_frontmatter_fold(ctx)

  -- The mode lives in the border title when there is one (spec 15 §2); in
  -- `split` layout there is no border, and the card carries it instead.
  if ui.left and ui.left.border and type(ui.left.border.set_text) == "function" then
    local title = ctx.mode == "compact" and " Capture " or (" Capture — %s "):format(ctx.mode)
    pcall(function()
      ui.left.border:set_text("top", title, "center")
    end)
  end

  local virt = fields.render_card(ctx)
  if #virt == 0 then
    return
  end

  local line_count = vim.api.nvim_buf_line_count(buf)
  local row, above = 0, true
  if close_line and ctx.mode ~= "raw" then
    if close_line >= line_count then
      -- The only case where the card renders BELOW its anchor: there is no
      -- body line to hang it above.
      row, above = close_line - 1, false
    else
      row, above = close_line, true
    end
  end
  pcall(vim.api.nvim_buf_set_extmark, buf, NS_CAPTURE, row, 0, {
    virt_lines = virt,
    virt_lines_above = above,
  })

  if row == 0 and above then
    in_win(win, function()
      vim.fn.winrestview({ topline = 1, topfill = #virt })
    end)
  end
end

--- `zi` — step the field policy `compact → full → raw → compact`.
---
--- Sticky across capture advance for the whole session, never persisted
--- (spec 15 §2). Mirrored onto `state.field_mode` so `:ParaOrganize debug`
--- and any `state.on` listener can see it.
---@return string|nil mode
function M.cycle_fields()
  local ui = M._ui
  if not ui then
    return nil
  end
  local current = ui.field_mode or ((M.config().ui or {}).capture or {}).mode or "compact"
  local index = 1
  for i, name in ipairs(fields.MODES) do
    if name == current then
      index = i
    end
  end
  local next_mode = fields.MODES[(index % #fields.MODES) + 1]
  ui.field_mode = next_mode
  if type(ui.state) == "table" then
    ui.state.field_mode = next_mode
  end
  M.refresh(ui.state)
  return next_mode
end

--- The current field mode (tests and `:ParaOrganize debug`).
function M.field_mode()
  return M._ui and M._ui.field_mode or nil
end

--- `:ParaOrganize debug` rows for spec 15's failure modes.
function M.render_diagnostics()
  return fields.diagnostics()
end

--- Draw one instruction line ABOVE an editable pane, as virtual text.
---
--- Used by the merge editor and by the spec 12 §1 review gate's edit mode.
--- Both put a MODIFIABLE buffer in the organize pane whose contents are sent
--- straight to the core, so their instructions may never be buffer lines
--- (spec 03 §5) — a `<leader>mc` would otherwise ship the header as part of
--- the merged note or the final diff.
local function draw_hint(buf, text, cfg)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  local hl = (cfg.ui and cfg.ui.highlights) or {}
  pcall(vim.api.nvim_buf_set_extmark, buf, NS_CAPTURE, 0, 0, {
    virt_lines = { { { text, hl.header or "Title" } }, { { "", "Normal" } } },
    virt_lines_above = true,
  })
end

--- Re-render both panes from `state`. Safe (and a no-op) when not mounted.
---@return boolean rendered
function M.refresh(state)
  if not M._ui then
    return false
  end
  state = state or {}
  local cfg = M.config()
  local ui = M._ui
  ui.state = state

  -- Left pane: swap in the real buffer for the current capture.
  local record = (state.captures or {})[state.current or 1]
  local wanted = ui.capture_buf
  if record and record.path then
    local buf, owned = capture_buffer(record.path)
    if buf then
      wanted = buf
      if owned then
        ui.owned_bufs[buf] = true
      end
    end
  end
  if wanted ~= ui.capture_buf then
    if vim.api.nvim_buf_is_valid(ui.capture_buf) then
      vim.api.nvim_buf_clear_namespace(ui.capture_buf, NS_CAPTURE, 0, -1)
    end
    -- The previous capture's manual fold lives in the WINDOW, not the buffer,
    -- so it would survive the swap and mis-fold the next note.
    remove_fold(ui.capture_win, true)
    ui.fold = nil
    ui._capture_dirty = false
    ui.capture_buf = wanted
    if vim.api.nvim_win_is_valid(ui.capture_win) then
      pcall(vim.api.nvim_win_set_buf, ui.capture_win, wanted)
    end
    M._install_capture_autocmds(wanted)
    local ok, actions = pcall(require, "para-organize.actions")
    if ok and type(actions.bind) == "function" then
      pcall(actions.bind, wanted, "capture")
    end
  end
  draw_capture(state, cfg)

  -- Right pane.
  local view = state.view or "suggestions"
  -- The spec 12 §1 review gate is the pane's fifth state. It renders through
  -- the module that owns it rather than through `render.right_pane`, so the
  -- integrate flow's rendering, keymaps and wire handling stay in one file —
  -- and a tree with no `integrate` module still renders the other four.
  local integrate = view == "integrate" and select(2, pcall(require, "para-organize.integrate")) or nil
  local editing = view == "integrate" and ((state.integrate or {}).editing == true)
  local rendered
  if type(integrate) == "table" and type(integrate.render) == "function" then
    rendered = integrate.render(state, cfg)
  else
    rendered = render.right_pane(state, cfg)
  end
  set_lines(ui.organize_buf, rendered.lines)
  local writable = (view == "merge") or editing
  vim.bo[ui.organize_buf].modifiable = writable
  -- `nofile` makes `:w` fail with "E382: Cannot write, 'buftype' option is
  -- set" — which is what Matt hit the first time he edited a merge and
  -- reached for the one key every Vim user reaches for. A pane that presents
  -- editable text must accept the write; `acwrite` routes it to the
  -- `BufWriteCmd` below, which commits through the CORE (thin-client law:
  -- the client still never writes the vault itself).
  vim.bo[ui.organize_buf].buftype = writable and "acwrite" or "nofile"

  -- Entering or leaving an editable view RE-BINDS the pane: while the user is
  -- editing, the single-letter action keymaps must not shadow their own
  -- editing commands (`s` substitute, `a` append, `p` paste, `r` replace, `/`
  -- search — and `<Esc>` closing the whole session mid-merge). Only on the
  -- transition; rebinding on every refresh would churn maps under the cursor.
  if ui._organize_editable ~= writable then
    ui._organize_editable = writable
    -- `mount(state, { bind = false })` means "do not touch keymaps" — the
    -- contract the whole render suite relies on — so a view transition may
    -- not quietly bind them either.
    local ok_actions, actions = pcall(require, "para-organize.actions")
    if ui.bound and ok_actions and type(actions.attach) == "function" then
      pcall(actions.attach, M.current_bufs(), { editable = writable })
    end
  end

  apply_marks(ui.organize_buf, rendered.marks, rendered.lines)
  vim.api.nvim_buf_clear_namespace(ui.organize_buf, NS_CAPTURE, 0, -1)
  if view == "merge" then
    draw_hint(ui.organize_buf, render.merge_hint(state, cfg), cfg)
  elseif editing and type(integrate) == "table" and type(integrate.hint) == "function" then
    draw_hint(ui.organize_buf, integrate.hint(state, cfg), cfg)
  end
  ui.map = rendered.map
  ui.view = view

  -- Keep the cursor on the selected item so `<CR>`/`j`/`k` agree.
  if view ~= "merge" and view ~= "integrate" and vim.api.nvim_win_is_valid(ui.organize_win) then
    local selected = state.selected or 1
    local target = rendered:line_of(function(item)
      return item.index == selected
    end) or rendered:line_of()
    if target then
      pcall(vim.api.nvim_win_set_cursor, ui.organize_win, { target, 0 })
    end
  end
  return true
end

--- Item mapped to a 1-based line of the organize pane (nil when the line is
--- a header/reason/blank).
function M.item_at(lnum)
  if not M._ui then
    return nil
  end
  return (M._ui.map or {})[lnum]
end

--- Item under the organize pane's cursor, when that pane holds the cursor.
function M.item_at_cursor()
  if not M._ui or not vim.api.nvim_win_is_valid(M._ui.organize_win) then
    return nil
  end
  local ok, pos = pcall(vim.api.nvim_win_get_cursor, M._ui.organize_win)
  if not ok then
    return nil
  end
  return M.item_at(pos[1])
end

--- The organize pane's current buffer text, verbatim.
---
--- The one read a thin client is allowed to make of its own pane: it is what
--- Matt just typed, not a vault byte. `merge_content` is the spec 03 §5
--- specialisation of it; the spec 12 §1 review gate uses the general form
--- because what it collects is a hand-edited DIFF, not a note body.
---@return string|nil
function M.organize_content()
  if not (M._ui and vim.api.nvim_buf_is_valid(M._ui.organize_buf)) then
    return nil
  end
  return table.concat(vim.api.nvim_buf_get_lines(M._ui.organize_buf, 0, -1, false), "\n")
end

--- Contents of the merge editor (spec 03 §5 step 3 sends this to
--- `op.merge_commit`).
function M.merge_content(state)
  if M._ui and M._ui.view == "merge" and vim.api.nvim_buf_is_valid(M._ui.organize_buf) then
    return table.concat(vim.api.nvim_buf_get_lines(M._ui.organize_buf, 0, -1, false), "\n")
  end
  return ((state or {}).merge or {}).content
end

function M.focus(pane)
  if not M._ui then
    return false
  end
  local win = pane == "capture" and M._ui.capture_win or M._ui.organize_win
  if win and vim.api.nvim_win_is_valid(win) then
    pcall(vim.api.nvim_set_current_win, win)
    return true
  end
  return false
end

-- --- help overlay ----------------------------------------------------------

--- Toggle the help popup. Content is generated from the live keymap table.
function M.show_help(entries)
  if M._ui and M._ui.help_win and vim.api.nvim_win_is_valid(M._ui.help_win) then
    M.close_help()
    return nil
  end
  local cfg = M.config()
  local rendered = render.help(entries or {}, cfg)
  local ok_popup, Popup = pcall(require, "nui.popup")
  local buf = scratch_buffer(HELP_BUFNAME)
  set_lines(buf, rendered.lines)
  apply_marks(buf, rendered.marks, rendered.lines)
  vim.bo[buf].modifiable = false

  local win
  if ok_popup then
    local popup = Popup({
      bufnr = buf,
      enter = true,
      focusable = true,
      border = { style = "rounded", text = { top = " Keymaps ", top_align = "center" } },
      relative = "editor",
      position = "50%",
      size = { width = math.min(78, math.max(40, vim.o.columns - 8)), height = math.min(#rendered.lines + 2, math.max(10, vim.o.lines - 6)) },
      buf_options = {},
      win_options = { cursorline = false, wrap = false },
    })
    local mounted = pcall(function()
      popup:mount()
    end)
    if mounted then
      win = popup.winid
      if M._ui then
        M._ui.help_popup = popup
      end
    end
  end
  if not win then
    win = vim.api.nvim_open_win(buf, true, { split = "below", win = 0 })
  end
  if M._ui then
    M._ui.help_buf = buf
    M._ui.help_win = win
  else
    M._help_orphan = { buf = buf, win = win }
  end
  for _, lhs in ipairs({ "q", "<Esc>", "?" }) do
    vim.keymap.set("n", lhs, function()
      M.close_help()
    end, { buffer = buf, nowait = true, silent = true, desc = "para-organize: close help" })
  end
  return win
end

function M.close_help()
  local buf, win, popup
  if M._ui then
    buf, win, popup = M._ui.help_buf, M._ui.help_win, M._ui.help_popup
    M._ui.help_buf, M._ui.help_win, M._ui.help_popup = nil, nil, nil
  elseif M._help_orphan then
    buf, win = M._help_orphan.buf, M._help_orphan.win
    M._help_orphan = nil
  end
  if popup then
    pcall(function()
      popup:unmount()
    end)
  end
  if win and vim.api.nvim_win_is_valid(win) then
    pcall(vim.api.nvim_win_close, win, true)
  end
  if buf and vim.api.nvim_buf_is_valid(buf) then
    pcall(vim.api.nvim_buf_delete, buf, { force = true })
  end
  if M._ui and vim.api.nvim_win_is_valid(M._ui.organize_win) then
    pcall(vim.api.nvim_set_current_win, M._ui.organize_win)
  end
end

-- --- teardown --------------------------------------------------------------

--- Undo everything this plugin did to the capture WINDOW.
---
--- Exposed (rather than inlined into `unmount`) because it is the teardown
--- primitive spec 15 §10.13 pins: the plugin's frontmatter fold is deleted
--- with a guarded `zd` on line 1 — ⚠ never `zE`, which would take the user's
--- own `zf` folds with it — and the fold options the plugin overwrote are put
--- back. Safe to call on a window the plugin never touched.
---@param win integer|nil
---@param buf integer|nil
---@param saved table|nil window-local options captured at mount
function M.release_capture_view(win, buf, saved)
  if buf and vim.api.nvim_buf_is_valid(buf) then
    pcall(vim.api.nvim_buf_clear_namespace, buf, NS_CAPTURE, 0, -1)
  end
  if not (win and vim.api.nvim_win_is_valid(win)) then
    return false
  end
  -- Only the plugin's own region: a fold the USER created is not the
  -- plugin's to delete.
  remove_fold(win, false)
  for name, value in pairs(saved or {}) do
    pcall(vim.api.nvim_set_option_value, name, value, { win = win })
  end
  return true
end

--- Tear the UI down. Idempotent and reentrancy-safe: `M._ui` is cleared
--- FIRST so the WinClosed autocmd we are about to trigger cannot recurse.
--- Leaves zero para-organize buffers, windows or autocmds behind
--- (spec 09 §2). A real capture buffer is only deleted when this plugin
--- created it AND it has no unsaved changes — never lose Matt's edits.
--- TEST SEAM — fire a pending debounced capture re-render synchronously.
--- Returns true if one was pending. See the comment on
--- `ui.flush_capture_debounce` for why specs must not `vim.wait` this out.
function M._flush_capture_debounce()
  local ui = M._ui
  if ui and ui.flush_capture_debounce then
    return ui.flush_capture_debounce()
  end
  return false
end

function M.unmount()
  local ui = M._ui
  M._ui = nil
  pcall(vim.api.nvim_del_augroup_by_name, AUGROUP)
  -- A live libuv timer keeps the event loop alive and leaks into the next
  -- spec; the augroup delete above stops new ones being armed.
  if ui and ui._capture_timer then
    local t = ui._capture_timer
    ui._capture_timer = nil
    pcall(function()
      t:stop()
      t:close()
    end)
  end
  if not ui then
    if M._help_orphan then
      M._ui = nil
      local orphan = M._help_orphan
      M._help_orphan = nil
      if orphan.win and vim.api.nvim_win_is_valid(orphan.win) then
        pcall(vim.api.nvim_win_close, orphan.win, true)
      end
      if orphan.buf and vim.api.nvim_buf_is_valid(orphan.buf) then
        pcall(vim.api.nvim_buf_delete, orphan.buf, { force = true })
      end
    end
    return false
  end

  if ui.help_popup then
    pcall(function()
      ui.help_popup:unmount()
    end)
  end
  for _, win in ipairs({ ui.help_win }) do
    if win and vim.api.nvim_win_is_valid(win) then
      pcall(vim.api.nvim_win_close, win, true)
    end
  end
  if ui.help_buf and vim.api.nvim_buf_is_valid(ui.help_buf) then
    pcall(vim.api.nvim_buf_delete, ui.help_buf, { force = true })
  end

  -- Give the capture WINDOW back before it closes: the plugin's fold and the
  -- fold options it overwrote are window-local, and a buffer the plugin did
  -- not open must be left exactly as it was found (spec 15 §3, §10.13).
  M.release_capture_view(ui.capture_win, ui.capture_buf, ui.saved_win_options)

  if ui.layout then
    pcall(function()
      ui.layout:unmount()
    end)
  end
  for _, win in ipairs({ ui.capture_win, ui.organize_win }) do
    if win and vim.api.nvim_win_is_valid(win) then
      pcall(vim.api.nvim_win_close, win, true)
    end
  end

  if ui.capture_buf and vim.api.nvim_buf_is_valid(ui.capture_buf) then
    pcall(vim.api.nvim_buf_clear_namespace, ui.capture_buf, NS_CAPTURE, 0, -1)
    pcall(vim.api.nvim_buf_clear_namespace, ui.capture_buf, NS_ORGANIZE, 0, -1)
  end
  for buf in pairs(ui.owned_bufs or {}) do
    if vim.api.nvim_buf_is_valid(buf) then
      local modified = vim.bo[buf].modified
      local name = vim.api.nvim_buf_get_name(buf)
      local is_ours = name:match("^para%-organize://") ~= nil
      if is_ours or not modified then
        pcall(vim.api.nvim_buf_delete, buf, { force = is_ours })
      end
    end
  end
  return true
end

return M
