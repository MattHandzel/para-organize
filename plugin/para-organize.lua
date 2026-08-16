-- para-organize.nvim — command + <Plug> registration (spec 03 §1-2).
--
-- Registration only. Every decision lives in `para-organize.commands`, so this
-- file stays trivially correct and the command surface is unit-testable without
-- sourcing plugin/.
--
-- Owned by the pickers+health+commands seat.

if vim.g.loaded_para_organize then
  return
end

-- Spec 03 §1 mandates a hard error on an unsupported Neovim; the parenthetical
-- there ("target 0.10+ APIs in the rewrite: vim.bo[buf], extmarks, vim.fs/
-- vim.uv") sets the real floor, and this tree uses vim.uv / vim.system /
-- vim.list_slice. Failing loudly at load beats a stack trace on first use.
if vim.fn.has("nvim-0.10") == 0 then
  vim.g.loaded_para_organize = true
  error("para-organize.nvim requires Neovim 0.10 or newer (spec 03 §1)")
end

vim.g.loaded_para_organize = true

local function commands()
  return require("para-organize.commands")
end

vim.api.nvim_create_user_command("ParaOrganize", function(opts)
  commands().execute(opts)
end, {
  nargs = "*",
  desc = "para-organize: session commands (spec 03 §2)",
  complete = function(arglead, cmdline, cursorpos)
    return commands().complete(arglead, cmdline, cursorpos)
  end,
})

vim.api.nvim_create_user_command("ParaOrganizeHealth", function()
  vim.cmd("checkhealth para-organize")
end, { nargs = 0, desc = "para-organize: run :checkhealth para-organize" })

--- The 13 <Plug> mappings of spec 03 §2. No global keymaps by default.
--- `Accept` has no subcommand (it backs <CR>), so it dispatches directly.
local PLUG_MAPPINGS = {
  { lhs = "ParaOrganizeStart", subcommand = "start" },
  { lhs = "ParaOrganizeStop", subcommand = "stop" },
  { lhs = "ParaOrganizeReindex", subcommand = "reindex" },
  { lhs = "ParaOrganizeSearch", subcommand = "search" },
  { lhs = "ParaOrganizeAccept", action = "accept", requires_session = true },
  { lhs = "ParaOrganizeMerge", subcommand = "merge" },
  { lhs = "ParaOrganizeArchive", subcommand = "archive" },
  { lhs = "ParaOrganizeNext", subcommand = "next" },
  { lhs = "ParaOrganizePrev", subcommand = "prev" },
  { lhs = "ParaOrganizeSkip", subcommand = "skip" },
  { lhs = "ParaOrganizeNewProject", subcommand = "new-project" },
  { lhs = "ParaOrganizeNewArea", subcommand = "new-area" },
  { lhs = "ParaOrganizeNewResource", subcommand = "new-resource" },
}

for _, mapping in ipairs(PLUG_MAPPINGS) do
  vim.keymap.set("n", "<Plug>(" .. mapping.lhs .. ")", function()
    local cmd = commands()
    if mapping.subcommand then
      local parsed, err = cmd.parse({ mapping.subcommand })
      if not parsed then
        return cmd.notify(err)
      end
      return cmd.dispatch(parsed)
    end
    return cmd.dispatch({
      subcommand = mapping.action,
      action = mapping.action,
      requires_session = mapping.requires_session or false,
      args = {},
    })
  end, { desc = "para-organize: " .. mapping.lhs })
end
