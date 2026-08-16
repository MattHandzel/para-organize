-- CONSOLIDATED. This per-seat bootstrap is now a shim onto the shared
-- `tests/plugin/minimal_init.lua` (integrator-owned), so the command lines
-- documented by each seat keep working while there is exactly ONE
-- implementation of the environment.
local here = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h")
dofile(here .. "/minimal_init.lua")
