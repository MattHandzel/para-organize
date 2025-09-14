# Contributing to para-organize.nvim

Thank you for considering contributing to para-organize.nvim! We welcome contributions of all kinds, including bug reports, feature requests, documentation improvements, and code changes.

## Setting Up the Development Environment

### Prerequisites

- Neovim >= 0.9.0
- Lua 5.1 or LuaJIT
- Git
- (Optional) NixOS for the development environment

### Development Setup

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/para-organize.nvim
   cd para-organize.nvim
   ```

2. Install dependencies:
   - If using NixOS, simply run `nix develop` to enter the development shell
   - Otherwise, ensure you have the following plugins available:
     - plenary.nvim
     - telescope.nvim
     - nui.nvim

3. Set up a test environment:
   ```bash
   mkdir -p tests/fixtures/vault/{projects,areas,resources,archives,capture/raw_capture}
   ```

## Development Workflow

### Running Tests

We use busted for testing:

```bash
make test        # Run all tests
make test-file FILE=utils_spec  # Run specific test file
```

### Code Style

We follow these code style guidelines:

- Use 2 spaces for indentation
- Use snake_case for variables and functions
- Use PascalCase for "classes" and modules
- Add comments for complex logic
- Include docstrings for public functions

### Commit Guidelines

We follow conventional commits for commit messages:

- `feat:` for new features
- `fix:` for bug fixes
- `docs:` for documentation changes
- `test:` for test changes
- `refactor:` for code refactoring
- `chore:` for maintenance tasks

Example:
```
feat(ui): add keyboard shortcut to toggle preview
```

### Pull Request Process

1. Fork the repository
2. Create a new branch for your changes
3. Make your changes and add tests if applicable
4. Run the test suite to ensure all tests pass
5. Submit a pull request with a clear description of the changes

## Project Structure

- `lua/para-organize/` - Core plugin code
  - `init.lua` - Entry point and API
  - `config.lua` - Configuration management
  - `indexer.lua` - Note indexing and metadata extraction
  - `search.lua` - Search functionality with Telescope
  - `suggest.lua` - Suggestion engine
  - `ui.lua` - Two-pane interface with nui.nvim
  - `move.lua` - Safe file operations
  - `learn.lua` - Learning system
  - `utils.lua` - Utility functions
  - `health.lua` - Health checks
- `plugin/` - Plugin initialization
- `doc/` - Documentation
- `tests/` - Test files

## Feature Development Guidelines

### Adding a New Feature

1. First, create an issue to discuss the feature
2. Design the feature to be modular and maintainable
3. Implement the feature with appropriate tests
4. Update documentation to reflect the new feature
5. Submit a pull request

### UI Changes

When making UI changes:

1. Follow the existing UI patterns
2. Use nui.nvim components for consistency
3. Ensure keyboard navigation works properly
4. Test on different terminal sizes

## Release Process

Releases are managed by the core team:

1. Update version in `lua/para-organize/init.lua`
2. Update CHANGELOG.md with changes
3. Create a Git tag for the version
4. Push to GitHub
5. Publish to LuaRocks (if applicable)

## Getting Help

If you need help, you can:

- Open an issue on GitHub
- Join our community discussions
- Reach out to maintainers directly

Thank you for contributing to para-organize.nvim!
