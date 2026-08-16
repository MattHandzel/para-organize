--- CONSOLIDATED into `tests/plugin/helpers.lua` (integrator-owned).
---
--- This seat-scoped helper's surface is preserved verbatim so the
--- pickers/health/commands specs keep working; the two names whose SHAPE
--- differs from the rpc seat's same-named functions are remapped here rather
--- than merged, because the two shapes are both in use:
---
---   phc `build_vault()`      → `helpers.fixture_vault()`  ({root, vault, …})
---   phc `start_core(fix)`    → `helpers.serve(fix)`       (socket under
---                                                          $XDG_RUNTIME_DIR)

local helpers = require("helpers")

local M = {}

M.repo = helpers.repo
M.python = helpers.python
M.organize = helpers.organize

M.socket_dir = helpers.socket_dir
M.connect = helpers.connect
M.capture_notifications = helpers.capture_notifications
M.notified = helpers.notified
M.wait_for = helpers.wait_for

--- A throwaway fixture vault: `{ root, vault, config_dir, state_dir }`.
function M.build_vault()
  return helpers.fixture_vault()
end

--- Spawn `organize serve` against a fixture vault: `{ socket, stop(), fixture }`.
function M.start_core(fixture)
  return helpers.serve(fixture)
end

return M
