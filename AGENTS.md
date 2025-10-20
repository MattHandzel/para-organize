# Repository Guidelines

## Project Structure & Module Organization
The Neovim plugin logic lives in `lua/para-organize/`, split into focused modules such as `ui/` for the two-pane interface, `config.lua` for defaults, and `suggest.lua` for the learning engine. Entry points sit in `plugin/` for lazy loading, while help docs reside in `doc/`. Integration samples and fixtures are kept under `examples/` and `tests/fixtures/`; automated specs live alongside helpers in `tests/`. Keep newly added assets scoped to these directories so the Makefile and luacheck manifests pick them up automatically.

## Build, Test, and Development Commands
Use `make lint` to run luacheck with the project globals and exclusions. `make test` executes Plenary+Busted through `tests/minimal_init.lua`; `make test-file FILE=name` targets a single `_spec.lua`. `make format` applies the shared Stylua rules, and `make coverage` generates `luacov.report.out`. For interactive debugging launch `make run` to enter Neovim with the plugin preloaded. Developers on Nix can enter a fully provisioned shell via `nix develop`.

## Coding Style & Naming Conventions
Stylua enforces 2-space indentation, 100-character lines, and double quotes where practical; run `make format` before pushing. Follow the existing Lua style: snake_case for locals and helpers, PascalCase for module tables, and descriptive module filenames (e.g., `ui/keymaps.lua`). Keep side-effectful requires out of tests, and prefer module-returning tables. Luacheck ignores unused arguments, so explicitly prefix intentionally unused variables with `_` for clarity.

## Testing Guidelines
Write Busted specs ending with `_spec.lua` and place shared fixtures under `tests/fixtures/`. Specs should boot through the minimal Neovim config (`tests/minimal_init.lua`) to ensure plugin APIs load correctly. Use `make fixtures` if you need the default PARA vault scaffold, and clean ephemeral outputs with `make clean`. Aim to cover new behaviours with async-safe specs and run `make coverage` when adding significant features to confirm hit rates.

## Commit & Pull Request Guidelines
Match the conventional commit style used in history, e.g., `refactor(ui): split renderer` or `fix(suggestions): handle empty capture`. Fill PR descriptions with the motivation, testing evidence (`make lint`/`make test`), and any UI screenshots. Link related issues and call out breaking changes or migration steps explicitly. Ensure linters and tests pass locally before requesting review.

## Environment & Tooling Tips
Run `make dev-setup` the first time to create fixtures and echo prerequisites. `make check-health` confirms the plugin registers correctly in Neovim. Keep dependencies documented in `deps/` and update help tags via `make docs` whenever editing `doc/para-organize.txt`.
