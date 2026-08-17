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
    display = {
      show_position = true,
      show_scores = true,
      show_reasons = true,
      show_metadata_summary = true,
      timestamp_format = "%b %d, %I:%M %p",
      hide_capture_id = true,
      hide_modalities = true,
      hide_location = true,
    },
    highlights = {
      selected = "Visual",
      header = "Title",
      reason = "Comment",
      score_high = "DiagnosticOk",
      score_medium = "DiagnosticWarn",
      score_low = "Comment",
      hint = "Comment",
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
function M.setup(user_config)
  local base = vim.deepcopy(M.DEFAULTS)
  local merged = base
  local from_integrator = integrator_config()
  if type(from_integrator) == "table" then
    merged = vim.tbl_deep_extend("force", merged, from_integrator)
  end
  if type(user_config) == "table" then
    merged = vim.tbl_deep_extend("force", merged, user_config)
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
  apply_win_options(handles, cfg)
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

--- Draw the capture pane's header as virtual lines ABOVE line 1 of the real
--- file buffer. Never buffer text (spec 10 §4 + 03 §5).
local function draw_capture_header(buf, state, cfg)
  if not (buf and vim.api.nvim_buf_is_valid(buf)) then
    return
  end
  vim.api.nvim_buf_clear_namespace(buf, NS_CAPTURE, 0, -1)
  local lines = render.capture_header(state, cfg)
  if #lines == 0 then
    return
  end
  local hl = (cfg.ui and cfg.ui.highlights) or {}
  local virt_lines = {}
  for index, text in ipairs(lines) do
    table.insert(virt_lines, { { text, index == 1 and (hl.header or "Title") or (hl.hint or "Comment") } })
  end
  table.insert(virt_lines, { { "", "Normal" } })
  pcall(vim.api.nvim_buf_set_extmark, buf, NS_CAPTURE, 0, 0, {
    virt_lines = virt_lines,
    virt_lines_above = true,
  })
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
    ui.capture_buf = wanted
    if vim.api.nvim_win_is_valid(ui.capture_win) then
      pcall(vim.api.nvim_win_set_buf, ui.capture_win, wanted)
    end
    local ok, actions = pcall(require, "para-organize.actions")
    if ok and type(actions.bind) == "function" then
      pcall(actions.bind, wanted, "capture")
    end
  end
  draw_capture_header(ui.capture_buf, state, cfg)

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
  vim.bo[ui.organize_buf].modifiable = (view == "merge") or editing
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

--- Tear the UI down. Idempotent and reentrancy-safe: `M._ui` is cleared
--- FIRST so the WinClosed autocmd we are about to trigger cannot recurse.
--- Leaves zero para-organize buffers, windows or autocmds behind
--- (spec 09 §2). A real capture buffer is only deleted when this plugin
--- created it AND it has no unsaved changes — never lose Matt's edits.
function M.unmount()
  local ui = M._ui
  M._ui = nil
  pcall(vim.api.nvim_del_augroup_by_name, AUGROUP)
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
