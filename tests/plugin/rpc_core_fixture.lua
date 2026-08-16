-- CONSOLIDATED into `tests/plugin/helpers.lua` (integrator-owned).
--
-- Every name this seat-scoped helper exported — root/organize_bin/python_bin/
-- sandbox/build_vault/env/core_config/start_core/read_log/cleanup/wait_for —
-- exists on `helpers` with identical semantics, so this file is a shim and the
-- rpc/core specs keep loading it with `dofile` unchanged.
return require("helpers")
