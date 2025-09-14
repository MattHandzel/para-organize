# para-organize.nvim Implementation Plan

## Project Overview
A Neovim plugin for organizing notes using the PARA (Projects, Areas, Resources, Archives) method with a focus on rapid processing of captured notes through an intelligent two-pane interface.

## Design Decisions

### Architecture Decisions
1. **Modular Component Design**: Split into focused modules (config, indexer, search, UI, etc.) for maintainability and testability
2. **Lazy Loading**: Use lazy initialization to avoid startup cost - modules loaded only when needed
3. **Async Operations**: Use plenary.nvim's async for file operations to prevent blocking
4. **Plugin Pattern**: Follow Neovim best practices with <Plug> mappings and user commands
5. **Database Choice**: Start with JSON for simplicity, with abstraction layer for future SQLite migration

### UI/UX Decisions
1. **nui.nvim for UI**: Provides polished, native-feeling components without reinventing the wheel
2. **Telescope Integration**: Familiar interface for searching and selecting that users already know
3. **Two-Pane Layout**: Left for editing capture, right for suggestions/navigation - optimal for merge workflows
4. **Keyboard-Centric**: All operations accessible via keyboard with discoverable shortcuts

### Safety Decisions
1. **Never Delete**: Always archive originals instead of deleting
2. **Atomic Operations**: Write-then-rename pattern for all file operations
3. **Operation Logging**: Maintain audit trail for all moves/merges for manual recovery

### Learning System Decisions
1. **Local-Only**: All learning data stored locally for privacy
2. **Weighted Scoring**: Combine recency, frequency, and pattern matching for suggestions
3. **Incremental Learning**: Update weights on each successful move/merge

## User Stories Extracted

1. **As a user**, I want to quickly process my captured notes by seeing intelligent suggestions for where they belong
2. **As a user**, I want to search my notes by tags, sources, modalities, and time windows
3. **As a user**, I want to merge captured notes into existing project/area notes without losing information
4. **As a user**, I want the system to learn from my organization patterns to provide better suggestions over time
5. **As a user**, I want to feel confident that my notes are never lost during organization
6. **As a user**, I want to quickly create new projects/areas/resources while organizing
7. **As a user**, I want keyboard shortcuts that are discoverable and don't conflict with my existing mappings
8. **As a user**, I want to see my unprocessed captures and work through them systematically

## File Structure and Responsibilities

### Core Module Files

#### `lua/para-organize/init.lua`
- **Purpose**: Entry point and plugin initialization
- **Responsibilities**:
  - Lazy load modules on demand
  - Register user commands
  - Set up autocommands
  - Expose public API

#### `lua/para-organize/config.lua`
- **Purpose**: Configuration management
- **Responsibilities**:
  - Define default configuration
  - Validate user configuration
  - Merge user overrides
  - Provide config access API

#### `lua/para-organize/indexer.lua`
- **Purpose**: Note indexing and metadata extraction
- **Responsibilities**:
  - Scan filesystem for notes
  - Parse YAML frontmatter
  - Build and maintain index
  - Handle incremental updates
  - Persist index to disk

#### `lua/para-organize/search.lua`
- **Purpose**: Search functionality
- **Responsibilities**:
  - Filter notes by criteria
  - Create Telescope pickers
  - Handle multi-select operations
  - Manage saved searches

#### `lua/para-organize/suggest.lua`
- **Purpose**: Suggestion engine
- **Responsibilities**:
  - Score potential destinations
  - Integrate learning data
  - Rank suggestions
  - Handle fallback strategies

#### `lua/para-organize/ui.lua`
- **Purpose**: User interface management
- **Responsibilities**:
  - Create two-pane layout
  - Manage buffer content
  - Handle user input
  - Update display state

#### `lua/para-organize/move.lua`
- **Purpose**: File operations
- **Responsibilities**:
  - Safe copy operations
  - Archive management
  - Tag updates
  - Operation logging

#### `lua/para-organize/learn.lua`
- **Purpose**: Learning system
- **Responsibilities**:
  - Record successful moves
  - Calculate pattern weights
  - Decay old patterns
  - Persist learning data

#### `lua/para-organize/utils.lua`
- **Purpose**: Shared utilities
- **Responsibilities**:
  - Path manipulation
  - Date/time formatting
  - String normalization
  - Common helpers

#### `lua/para-organize/health.lua`
- **Purpose**: Health checks
- **Responsibilities**:
  - Validate configuration
  - Check dependencies
  - Verify permissions
  - Report issues

### Support Files

#### `plugin/para-organize.lua`
- Plugin initialization for Neovim

#### `doc/para-organize.txt`
- Vimdoc help documentation

#### `tests/`
- Busted test files for each module

#### `flake.nix`
- Nix flake for development environment

#### `README.md`
- User-facing documentation with full config example

## Implementation Milestones

### Milestone 1: Foundation (Week 1)
- [x] Project structure setup
- [x] Basic configuration module
- [x] Health check implementation
- [x] Utility functions
- [ ] Basic test infrastructure
- [x] Flake.nix for development

### Milestone 2: Indexing & Search (Week 2)
- [x] Frontmatter parser (in utils.lua)
- [x] Filesystem scanner (in indexer.lua)
- [x] Index data structure
- [x] Index persistence
- [x] Basic search functionality
- [ ] Telescope picker integration

### Milestone 3: UI Implementation (Week 3)
- [ ] Two-pane layout with nui.nvim
- [ ] Capture note display
- [ ] Suggestions list rendering
- [ ] Folder navigation
- [ ] Keyboard mappings
- [ ] Which-key integration

### Milestone 4: Core Operations (Week 4)
- [ ] Move operation implementation
- [ ] Archive functionality
- [ ] Merge mode
- [ ] Tag management
- [ ] Operation logging
- [ ] Undo hints

### Milestone 5: Intelligence (Week 5)
- [ ] Suggestion scoring algorithm
- [ ] Learning data structure
- [ ] Pattern recognition
- [ ] Weight calculation
- [ ] Persistence of learning data

### Milestone 6: Polish & Release (Week 6)
- [ ] Comprehensive testing
- [ ] Documentation completion
- [ ] Performance optimization
- [ ] Release packaging
- [ ] CI/CD setup
- [ ] Demo video/screenshots

## Current Status (2025-09-14)

### Completed
- [x] Project structure created
- [x] PLAN.md documentation
- [x] README.md with full configuration example
- [x] flake.nix for NixOS development environment
- [x] Makefile with development commands
- [x] config.lua - Configuration management with validation and debug info
- [x] utils.lua - Comprehensive utility functions (logging, paths, YAML parsing, async)  
  - **2025-09-14**: Fixed bug where YAML front-matter parser returned `nil` for valid notes; added robust delimiter handling and nested map/list support
- [x] health.lua - Health checks for dependencies and configuration
- [x] init.lua - Main entry point with commands, plugin setup, and debug command
- [x] indexer.lua - Note indexing with metadata extraction and search
- [x] search.lua - Telescope integration for searching (fixed 'until' keyword issue)
- [x] suggest.lua - Suggestion engine with scoring
- [x] ui.lua - Two-pane interface with nui.nvim
- [x] move.lua - Safe file operations
- [x] learn.lua - Learning system
- [x] tests/minimal_init.lua - Test initialization
- [x] tests/utils_spec.lua - Unit tests for utils module
- [x] tests/indexer_spec.lua - Unit tests for indexer module
- [x] tests/suggest_spec.lua - Unit tests for suggest module
- [x] tests/move_spec.lua - Unit tests for move module
- [x] tests/learn_spec.lua - Unit tests for learn module
- [x] tests/integration_spec.lua - Integration tests
- [x] MANUAL.md - Comprehensive user manual
- [x] LICENSE - MIT license
- [x] CONTRIBUTING.md - Contributing guidelines
- [x] CHANGELOG.md - Version change tracking
- [x] doc/para-organize.txt - Vimdoc help documentation
- [x] Plugin initialization and comprehensive tests
- [x] GitHub Actions CI integration (test.yml, lint.yml, release.yml)
- [x] Example configurations (minimal and full)
- [x] Debug mode with comprehensive diagnostics

### In Progress
- [ ] Fixing capture path detection issue
- [ ] Enhanced keyboard shortcuts
- [ ] Performance optimizations

### Next Steps
1. Polish UI and keybindings
2. Create example configurations for different workflows
3. Add additional telescope pickers for tag/metadata search
4. Create user documentation with examples and screenshots
5. Add performance optimizations for larger note collections

## Technical Specifications

### Dependencies
- **Required**: Neovim >= 0.9.0, plenary.nvim, telescope.nvim
- **Recommended**: nui.nvim
- **Optional**: which-key.nvim, nvim-web-devicons

### Data Storage
- **Index**: JSON at `vim.fn.stdpath('data') .. '/para-organize/index.json'`
- **Learning**: JSON at `vim.fn.stdpath('data') .. '/para-organize/learning.json'`
- **Config**: In-memory with validation
- **Logs**: Operation log at `vim.fn.stdpath('data') .. '/para-organize/operations.log'`

### Performance Targets
- Index 1000 notes in < 2 seconds
- Suggestion generation in < 100ms
- UI response time < 50ms
- Incremental index update < 500ms

### Testing Strategy
- Unit tests for each module
- Integration tests for workflows
- Mock filesystem for file operations
- Performance benchmarks for indexing

## Configuration Schema

```lua
{
  -- Paths
  vault_dir = "~/notes",
  capture_folder = "capture/raw_capture",
  para_folders = {
    projects = "projects",
    areas = "areas", 
    resources = "resources",
    archives = "archives"
  },
  
  -- Indexing
  index = {
    ignore_patterns = { "*.tmp", ".git" },
    max_file_size = 1048576, -- 1MB
    incremental_debounce = 500,
    backend = "json" -- or "sqlite"
  },
  
  -- UI
  ui = {
    layout = "float", -- or "split"
    border = "rounded",
    show_icons = true,
    show_scores = true,
    timestamp_format = "%b %d, %I:%M %p",
    auto_move_to_new_folder = true
  },
  
  -- Suggestions
  suggestions = {
    tag_weight = 2.0,
    learned_weight = 1.5,
    recency_decay = 0.9,
    min_confidence = 0.3
  },
  
  -- Telescope
  telescope = {
    theme = "dropdown",
    layout_strategy = "horizontal",
    multi_select = true
  }
}
```

## API Design

### Public Functions
```lua
-- Main API
require('para-organize').setup(config)
require('para-organize').start(filters)
require('para-organize').stop()
require('para-organize').next()
require('para-organize').prev()
require('para-organize').skip()
require('para-organize').move(destination)
require('para-organize').merge(target_note)
require('para-organize').archive()
require('para-organize').reindex()
require('para-organize').search(query)
```

### User Commands
```vim
:ParaOrganize start [filters...]
:ParaOrganize stop
:ParaOrganize next|prev|skip
:ParaOrganize move <dest>
:ParaOrganize merge
:ParaOrganize archive
:ParaOrganize reindex
:ParaOrganize help
:ParaOrganize new-project|new-area|new-resource <name>
```

### <Plug> Mappings
```vim
<Plug>(ParaOrganizeStart)
<Plug>(ParaOrganizeAccept)
<Plug>(ParaOrganizeMerge)
<Plug>(ParaOrganizeArchive)
<Plug>(ParaOrganizeNext)
<Plug>(ParaOrganizePrev)
<Plug>(ParaOrganizeSkip)
<Plug>(ParaOrganizeSearch)
<Plug>(ParaOrganizeNewProject)
<Plug>(ParaOrganizeNewArea)
<Plug>(ParaOrganizeNewResource)
```

## Notes and Considerations

### Frontmatter Schema
All fields are optional to ensure resilience:
- `timestamp`: ISO 8601 datetime
- `id`: Unique identifier
- `aliases`: List of alternative names
- `capture_id`: Original capture identifier
- `modalities`: List of content types
- `context`: Contextual information
- `sources`: List of source references
- `tags`: List of tags
- `location`: Geographic or logical location
- `metadata`: Key-value pairs
- `processing_status`: Current status
- `created_date`: Creation date
- `last_edited_date`: Last modification date

### PARA Method Implementation
1. **Projects**: Active projects with deadlines
2. **Areas**: Ongoing responsibilities  
3. **Resources**: Reference materials
4. **Archives**: Inactive/completed items

### Safety Guarantees
1. Original files always preserved in archives
2. Atomic writes prevent corruption
3. Operation log enables recovery
4. Validation before any destructive operation

## Development Guidelines

### Code Style
- Follow Neovim Lua style guide
- Use snake_case for functions/variables
- PascalCase for classes/modules
- Comprehensive docstrings
- Type hints where beneficial

### Git Workflow
- Feature branches for new functionality
- Conventional commits
- PR with tests required
- Semantic versioning

### Release Process
1. Update version in init.lua
2. Update CHANGELOG.md
3. Run full test suite
4. Create git tag
5. Push to GitHub
6. Publish to LuaRocks

## Resources and References
- [Neovim Lua Guide](https://neovim.io/doc/user/lua-guide.html)
- [Neovim Best Practices](https://github.com/nvim-neorocks/nvim-best-practices)
- [nui.nvim Documentation](https://github.com/MunifTanjim/nui.nvim)
- [Telescope.nvim API](https://github.com/nvim-telescope/telescope.nvim)
- [PARA Method](https://fortelabs.co/blog/para/)

## Project Plan for para-organize.nvim

## Overview
This document outlines the plan for implementing and updating the `para-organize.nvim` Neovim plugin for PARA-based note organization. It includes the steps taken, current progress, and future tasks.

## Completed Tasks

- **Core Modules Implementation**
  - `config.lua`: Configuration management with validation.
  - `utils.lua`: Utility functions for path handling, frontmatter parsing, etc.
  - `indexer.lua`: Note indexing and metadata extraction.
  - `search.lua`: Telescope integration for searching notes.
  - `suggest.lua`: Suggestion engine for destinations.
  - `learn.lua`: Learning system to improve suggestions over time.
  - `move.lua`: Safe file operations ensuring no deletion, only archiving.
  - `ui.lua`: Two-pane interface using `nui.nvim`.
  - `health.lua`: Health checks for dependencies and configuration.

- **Documentation**
  - `README.md`: Comprehensive project documentation with configuration examples.
  - `MANUAL.md`: Detailed user guide.
  - `doc/para-organize.txt`: Vimdoc help documentation.
  - `PLAN.md`: Implementation plan and progress tracking.
  - `CONTRIBUTING.md`: Guidelines for contributors.
  - `CHANGELOG.md`: Version history.

- **Test Infrastructure**
  - `tests/minimal_init.lua`: Test environment setup.
  - `tests/utils_spec.lua`: Unit tests for the utils module.

- **Development Environment**
  - `flake.nix`: NixOS development environment setup.
  - `Makefile`: Development commands for testing and building.
  - `.stylua.toml`: Lua formatting configuration.
  - `.luacheckrc`: Lua linting configuration.

## Current Task: UI Refactoring

- **Objective**: Update the UI to meet new specifications for both left and right panes.

- **Left Pane (Capture Pane) Updates**
  - Display the number of notes that need to be organized.
  - Allow full editing capabilities in the left pane as a normal Neovim buffer.
  - Improve timestamp readability to show month, day, and time.
  - Display aliases (excluding capture_id), tags, and sources.
  - Exclude modalities and location from display.

- **Right Pane (Organization Pane) Updates**
  - Refactor to show a list of directories and files instead of just suggestions.
  - Implement sorting options:
    - Simple alphabetical order (directories first, case insensitive).
    - Last recently modified (directories first).
    - Intelligent suggestions (based on `learn.lua` module).
  - Indicate type (Project, Area, Resource, Archive) with letters (P, A, R, and a trash can icon for Archive).
  - Enable navigation:
    - Press 'Enter' on a directory to view subfolders and files.
    - Use '/' to search for specific projects/areas/resources.
    - Display aliases for notes instead of filenames or IDs.
  - Implement 'Merge to Existing Note' mode:
    - Press 'Enter' on a file to open it in the right pane alongside the original note in the left pane.
    - Allow editing of the right pane note to merge content.
    - Upon closing the buffer, archive the original capture note to `archive/capture/raw_capture/{filename}` without moving it to the destination folder.

- **Technical Implementation**
  - Update `ui.lua` to handle new pane content and interactions.
  - Enhance `indexer.lua` to support directory and file listing with alias extraction.
  - Modify `move.lua` to ensure proper archiving without destination move during manual merging.

- **Testing and Validation**
  - Test UI interactions for both panes to ensure responsiveness and correctness.
  - Validate sorting functionalities and navigation in the right pane.
  - Confirm that merging notes manually archives the original without moving.

## Upcoming Tasks

- **Bug Fixes and Enhancements**
  - Address any issues arising from the UI refactoring.
  - Optimize performance for large note collections in the indexer and UI.

- **User Feedback Integration**
  - Collect user feedback on the new UI for further improvements.
  - Adjust suggestion engine based on user interactions and feedback.

- **Documentation Updates**
  - Update `README.md`, `MANUAL.md`, and help docs to reflect new UI features and workflows.

- **Release Preparation**
  - Prepare release notes for the updated version in `CHANGELOG.md`.
  - Ensure all tests pass and perform a final code review.

## Version Control Strategy

- Create a new branch for UI refactoring: `feature/ui-refactor`.
- Commit changes with descriptive messages for each major update to the UI components.
- Push commits to the repository after significant progress or completion of the refactoring.

This plan will be updated as tasks are completed or new requirements are identified. If you have any specific additions or modifications to this plan, please let me know.
