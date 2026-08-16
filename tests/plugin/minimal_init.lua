-- The ONE headless bootstrap for every `tests/plugin/*_spec.lua`.
--
-- Consolidates the three per-seat variants (minimal_init_rpc,
-- minimal_init_ui_actions, minimal_init_pickers_health_commands); those files
-- are now one-line shims onto this one, so both the documented per-seat
-- commands and `make test-plugin` run the identical environment.
--
--   nvim --headless --noplugin -u tests/plugin/minimal_init.lua \
--        -c "lua require('plenary.busted').run('<ABSOLUTE spec path>')"
--
-- or, equivalently (the :PlenaryBustedFile override below makes it work):
--
--   nvim --headless --noplugin -u tests/plugin/minimal_init.lua \
--        -c "PlenaryBustedFile tests/plugin/ui_spec.lua"
--
-- Owned by the INTEGRATOR.

local this = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p")
local repo = vim.fn.fnamemodify(this, ":h:h:h")

vim.opt.swapfile = false
vim.opt.shadafile = "NONE"
vim.opt.more = false
vim.opt.shortmess:append("filnxtToOFI")
vim.g.mapleader = " "
vim.g.maplocalleader = " "

-- Take the developer's real config off the runtimepath. Without this the
-- `runtime plugin/…` below reaches into ~/.config/nvim and drags a whole
-- personal distro into the test process (observed: nvchad erroring at
-- startup, and specs then failing for reasons that have nothing to do with
-- this repo).
local user_config = vim.fn.stdpath("config")
vim.opt.rtp:remove(user_config)
vim.opt.rtp:remove(user_config .. "/after")
vim.opt.packpath = {}

-- Required deps of spec 03 §1. telescope is required by the plugin but
-- optional for a spec that never opens a picker, hence the isdirectory guard
-- rather than an assert here (`:checkhealth` is where a missing dep is a
-- reported error).
local lazy = vim.fn.expand("~/.local/share/nvim/lazy")
for _, plugin in ipairs({ "plenary.nvim", "nui.nvim", "telescope.nvim" }) do
  local path = lazy .. "/" .. plugin
  if vim.uv.fs_stat(path) then
    vim.opt.rtp:append(path)
  end
end

vim.opt.rtp:prepend(repo)

-- `tests/plugin/?.lua` so specs can `require("helpers")`.
package.path = table.concat({
  repo .. "/lua/?.lua",
  repo .. "/lua/?/init.lua",
  repo .. "/tests/plugin/?.lua",
  package.path,
}, ";")

-- The repo root, for the venv binary and the python fixture builder. Both
-- spellings are carried because the seats' helpers grew up reading different
-- ones; consolidating the NAME would have broken specs for no gain.
vim.env.PARA_ORGANIZE_REPO_ROOT = repo
_G.PARA_ORGANIZE_REPO = repo

-- `--noplugin` deliberately keeps para-organize's own plugin/ file out of the
-- picture (specs that want :ParaOrganize source it themselves, so it is always
-- clear what has run) — but that also suppresses plenary's, which is where
-- :PlenaryBustedFile comes from.
vim.cmd("runtime plugin/plenary.vim")
require("plenary.busted")

-- HARNESS DEFECT (verified in plenary/lua/plenary/test_harness.lua:84-101):
-- `:PlenaryBustedFile` calls `test_harness.test_file(path)` with NO options,
-- so the CHILD nvim it spawns gets `--noplugin` but no `-u` and loads the
-- developer's real init.lua — never seeing this file's runtimepath or
-- package.path. Running the spec IN-PROCESS keeps the documented command
-- line working, keeps the environment exactly the one built above, and still
-- exits 0/1/2 the way the headless runner does (busted.run ends in `Ncq`).
vim.api.nvim_create_user_command("PlenaryBustedFile", function(opts)
  require("plenary.busted").run(vim.fn.fnamemodify(vim.fn.expand(opts.args), ":p"))
end, { nargs = 1, complete = "file", force = true })

-- Same defect, for anything that calls the harness function directly rather
-- than the command. `test_directory` accepts a single file path: its
-- `find <path> -type f -name '*_spec.lua'` matches the file itself.
local ok_harness, harness = pcall(require, "plenary.test_harness")
if ok_harness then
  harness.test_file = function(filepath)
    return harness.test_directory(filepath, { minimal_init = this })
  end
end
