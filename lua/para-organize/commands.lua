--- The `:ParaOrganize` command tree (spec 03 §2).
---
--- Pure dispatch: parse argv, decide, then call one function on
--- `para-organize.actions`. This module NEVER touches the vault, the index or
--- any core state — every effect is somebody else's RPC call (spec 10 §1).
---
--- `plugin/para-organize.lua` only registers the command and forwards to
--- `M.execute` / `M.complete`, so the whole surface is unit-testable without a
--- running session.
---
--- Owned by the pickers+health+commands seat.

local M = {}

M.PREFIX = "para-organize: "

--- Filter keys documented in spec 03 §2 "Session filters". Any `k=v` pair is
--- accepted and forwarded; these are the ones offered by completion. Unknown
--- keys are rejected LOUDLY by the core (it answers with the valid list), which
--- is the right place for that check to live — the plugin does not duplicate
--- the core's validation and cannot drift from it.
M.FILTER_KEYS = { "tags", "sources", "modalities", "since", "until_date", "status" }

--- Filters whose value is a scalar, not a comma list. Everything else is
--- canonicalized to a list at this boundary, per spec 03 §2 ("Filter values are
--- lists … resolve by canonicalizing to lists at the boundary").
M.SCALAR_FILTERS = { since = true, ["until"] = true, until_date = true, text = true }

--- Completion candidates only. Any value is forwarded as typed.
M.FILTER_VALUE_HINTS = {
  status = { "raw", "organized" },
  para_type = { "capture", "project", "area", "resource", "archive", "other" },
}

--- Spec 03 §2, in the spec's own order. `action` is the function name looked up
--- on `para-organize.actions` (hyphen → underscore, ruled by the architect).
M.SUBCOMMANDS = {
  { name = "start", action = "start", takes = "filters" },
  { name = "stop", action = "stop", requires_session = true },
  { name = "next", action = "next", requires_session = true },
  { name = "prev", action = "prev", requires_session = true, aliases = { "previous" } },
  { name = "skip", action = "skip", requires_session = true },
  { name = "move", action = "move", takes = "path", requires_session = true },
  { name = "merge", action = "merge", requires_session = true },
  { name = "archive", action = "archive", requires_session = true },
  { name = "reindex", action = "reindex" },
  -- `search_picker`, not `search`: spec 03 §2 says this subcommand "opens the
  -- Telescope search picker over the index", which is a different surface from
  -- the `/` INLINE search of §3 state 3. Routing it to `actions.search` — a
  -- no-argument, session-only prompt — made `:ParaOrganize search <query>` a
  -- silent no-op that discarded the parsed query.
  { name = "search", action = "search_picker", takes = "query" },
  { name = "debug", action = "debug" },
  { name = "help", action = "help" },
  { name = "new-project", action = "new_project", takes = "name" },
  { name = "new-area", action = "new_area", takes = "name" },
  { name = "new-resource", action = "new_resource", takes = "name" },
}

--- Action functions that back keymaps rather than subcommands (spec 03 §3;
--- `r`/`p` must actually be bound — 08 §A34). Health asserts every name in
--- `M.action_names()` resolves, so a rename surfaces at health time instead of
--- at the first keypress (loud-failure law, 09 §1.5).
M.KEYMAP_ONLY_ACTIONS = { "accept", "refresh", "toggle_preview", "sort_cycle", "set_meta", "search" }

local BY_NAME = {}
local ORDERED_NAMES = {}
for _, spec in ipairs(M.SUBCOMMANDS) do
  BY_NAME[spec.name] = spec
  ORDERED_NAMES[#ORDERED_NAMES + 1] = spec.name
  for _, alias in ipairs(spec.aliases or {}) do
    BY_NAME[alias] = spec
  end
end

--- Module overrides for tests (`M.inject("actions", mock)`). Production code
--- never populates this; `require` is the only other resolution path.
M._modules = {}

--- Replace a sibling module for the duration of a test.
---@param name string module basename under `para-organize.`
---@param mod table|nil
function M.inject(name, mod)
  M._modules[name] = mod
end

--- Drop every injected module.
function M.reset_injections()
  M._modules = {}
end

--- Resolve `para-organize.<name>`, preferring an injected stub.
---@return table|nil module, string|nil err
function M.resolve(name)
  local injected = M._modules[name]
  if injected ~= nil then
    -- `M.inject(name, false)` means "pretend this module is not installed",
    -- which is how the degradation paths are tested. It must produce the same
    -- (module=nil, err=string) pair a failed `require` does, or the caller
    -- notifies a nil message.
    if injected == false then
      return nil, ("module para-organize.%s is unavailable (not installed)"):format(name)
    end
    return injected
  end
  local ok, mod = pcall(require, "para-organize." .. name)
  if ok and type(mod) == "table" then
    return mod
  end
  return nil, ("module para-organize.%s is unavailable (%s)"):format(name, tostring(mod))
end

---@return string[] every action name `:ParaOrganize` or a keymap may dispatch
function M.action_names()
  local names, seen = {}, {}
  for _, spec in ipairs(M.SUBCOMMANDS) do
    if not seen[spec.action] then
      seen[spec.action] = true
      names[#names + 1] = spec.action
    end
  end
  for _, name in ipairs(M.KEYMAP_ONLY_ACTIONS) do
    if not seen[name] then
      seen[name] = true
      names[#names + 1] = name
    end
  end
  return names
end

---@return string[] canonical subcommand names, in spec order
function M.subcommand_names()
  return vim.deepcopy(ORDERED_NAMES)
end

--- True when `word` has the `k=v` shape spec 03 §2 parses filters with.
local function is_filter_pair(word)
  return type(word) == "string" and word:match("^([^=]+)=(.+)$") ~= nil
end

--- Canonicalize one filter value: scalars stay strings, everything else
--- becomes a list (comma-separated, trimmed, blanks dropped).
local function canonical_value(key, raw)
  if M.SCALAR_FILTERS[key] then
    return vim.trim(raw)
  end
  local values = {}
  for part in tostring(raw):gmatch("[^,]+") do
    local value = vim.trim(part)
    if value ~= "" then
      values[#values + 1] = value
    end
  end
  return values
end

--- Parse `k=v` words into the filter table sent to `session.start`.
---@param words string[]
---@return table|nil filters, string|nil err
function M.parse_filters(words)
  local filters = {}
  for _, word in ipairs(words) do
    local key, value = word:match("^([^=]+)=(.+)$")
    if not key then
      return nil,
        ("%q is not a filter — session filters are k=v pairs (%s)"):format(
          word,
          table.concat(M.FILTER_KEYS, ", ")
        )
    end
    key = vim.trim(key):lower()
    filters[key] = canonical_value(key, value)
  end
  return filters
end

--- Parse the command's `fargs` into a dispatch decision.
---
--- Spec 03 §2: subcommand = argv[1]; no subcommand ⇒ `start`; `search` joins
--- the remaining argv with spaces. `move` / `new-*` also join, because
--- `nargs="*"` has already thrown away the user's quoting and a PARA folder
--- may legitimately contain spaces (the fixture vault has one).
---@param fargs string[]|nil
---@return table|nil parsed, string|nil err
function M.parse(fargs)
  local argv = {}
  for _, word in ipairs(fargs or {}) do
    if word ~= "" then
      argv[#argv + 1] = word
    end
  end

  local head = argv[1]
  local spec, rest

  if head == nil then
    spec, rest = BY_NAME.start, {}
  elseif BY_NAME[head:lower()] then
    spec = BY_NAME[head:lower()]
    rest = vim.list_slice(argv, 2, #argv)
  elseif is_filter_pair(head) then
    -- `:ParaOrganize tags=foo` — implicit start (03 §2 "No subcommand ⇒ start").
    spec, rest = BY_NAME.start, argv
  else
    return nil,
      ("unknown subcommand %q — valid: %s"):format(head, table.concat(ORDERED_NAMES, ", "))
  end

  local parsed = {
    subcommand = spec.name,
    action = spec.action,
    requires_session = spec.requires_session or false,
    args = rest,
  }

  if spec.takes == "filters" then
    local filters, err = M.parse_filters(rest)
    if not filters then
      return nil, err
    end
    parsed.filters = filters
  elseif spec.takes == "query" then
    parsed.query = table.concat(rest, " ")
  elseif spec.takes == "path" then
    parsed.path = table.concat(rest, " ")
    if parsed.path == "" then
      parsed.path = nil
    end
  elseif spec.takes == "name" then
    parsed.name = table.concat(rest, " ")
    if parsed.name == "" then
      parsed.name = nil
    end
  end

  return parsed
end

--- The single argument each action receives, so dispatch stays declarative.
local function action_argument(parsed)
  if parsed.filters ~= nil then
    return parsed.filters
  end
  if parsed.query ~= nil then
    return parsed.query
  end
  if parsed.path ~= nil then
    return parsed.path
  end
  return parsed.name
end

--- Best-effort read of the session state.
---@return boolean|nil true/false when knowable, nil when the shape is unknown
function M.has_session()
  local state = M.resolve("state")
  if not state then
    return nil
  end
  if type(state.has_session) == "function" then
    local ok, result = pcall(state.has_session)
    if ok then
      return result and true or false
    end
    return nil
  end
  local session = state.session
  if session == nil and type(state.get) == "function" then
    local ok, result = pcall(state.get)
    session = ok and result or nil
  end
  if type(session) ~= "table" then
    return nil
  end
  if session.active ~= nil then
    return session.active and true or false
  end
  if type(session.captures) == "table" then
    return #session.captures > 0
  end
  return nil
end

--- Report a failure the way spec 10 §1 requires: one clear line, no traceback.
function M.notify(message, level)
  -- tostring, not concatenation: the whole point of this function is that the
  -- user sees one clear line, so it must not be able to raise on its own.
  vim.notify(M.PREFIX .. tostring(message), level or vim.log.levels.ERROR)
end

--- Run one parsed command.
---@return boolean ok, string|nil err
function M.dispatch(parsed)
  if parsed.requires_session and M.has_session() == false then
    local err = ("no active session — run :ParaOrganize start first (%s)"):format(parsed.subcommand)
    M.notify(err, vim.log.levels.WARN)
    return false, err
  end

  local actions, resolve_err = M.resolve("actions")
  if not actions then
    M.notify(resolve_err, vim.log.levels.ERROR)
    return false, resolve_err
  end

  local fn = actions[parsed.action]
  if type(fn) ~= "function" then
    local err = ("action %q is not implemented (actions.%s)"):format(
      parsed.subcommand,
      parsed.action
    )
    M.notify(err, vim.log.levels.ERROR)
    return false, err
  end

  -- `move` with no destination opens the destination picker (03 §4:
  -- open_folder_picker is "used by merge method 2 and `move`").
  if parsed.subcommand == "move" and parsed.path == nil then
    local pickers = M.resolve("pickers")
    if pickers and type(pickers.open_folder_picker) == "function" then
      pickers.open_folder_picker(function(folder)
        -- The picker hands back nil when Matt cancels; cancelling a move must
        -- not fire the action with a nil destination.
        if folder == nil then
          return
        end
        fn(type(folder) == "table" and folder.path or folder)
      end)
      return true
    end
    local err = "move needs a destination — :ParaOrganize move <path>"
    M.notify(err, vim.log.levels.ERROR)
    return false, err
  end

  local ok, err = pcall(fn, action_argument(parsed))
  if not ok then
    local message = ("%s failed: %s"):format(parsed.subcommand, tostring(err))
    M.notify(message, vim.log.levels.ERROR)
    return false, message
  end
  return true
end

--- `:ParaOrganize` callback.
---@param opts table nvim user-command opts (only `fargs` is read)
---@return boolean ok, string|nil err
function M.execute(opts)
  local parsed, err = M.parse((opts or {}).fargs)
  if not parsed then
    M.notify(err, vim.log.levels.ERROR)
    return false, err
  end
  return M.dispatch(parsed)
end

local function prefixed(candidates, arglead)
  local out = {}
  for _, candidate in ipairs(candidates) do
    if arglead == "" or candidate:sub(1, #arglead) == arglead then
      out[#out + 1] = candidate
    end
  end
  return out
end

--- `:ParaOrganize` completion.
---@param arglead string
---@param cmdline string
---@return string[]
function M.complete(arglead, cmdline, _)
  arglead = arglead or ""
  local words = vim.split(vim.trim(cmdline or ""), "%s+", { trimempty = true })
  table.remove(words, 1) -- drop the command name itself

  local completing_first = #words == 0 or (#words == 1 and arglead ~= "")
  if completing_first then
    return prefixed(ORDERED_NAMES, arglead)
  end

  local spec = BY_NAME[(words[1] or ""):lower()]
  if not spec then
    -- Implicit start (`:ParaOrganize tags=x <Tab>`) still completes filters.
    spec = is_filter_pair(words[1] or "") and BY_NAME.start or nil
  end
  if not spec or spec.takes ~= "filters" then
    return {}
  end

  local key, partial = arglead:match("^([^=]+)=(.*)$")
  if key then
    local hints = M.FILTER_VALUE_HINTS[key:lower()]
    if not hints then
      return {}
    end
    local out = {}
    for _, value in ipairs(hints) do
      if partial == "" or value:sub(1, #partial) == partial then
        out[#out + 1] = key .. "=" .. value
      end
    end
    return out
  end

  local keys = {}
  for _, name in ipairs(M.FILTER_KEYS) do
    keys[#keys + 1] = name .. "="
  end
  return prefixed(keys, arglead)
end

return M
