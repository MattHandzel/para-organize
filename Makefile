# para-organize.nvim Makefile

.PHONY: all test lint coverage docs clean watch help install-deps check-health

# Default target
all: lint test

# Install development dependencies
install-deps:
	@echo "Installing dependencies..."
	@echo "Please ensure you have the following installed:"
	@echo "  - Neovim >= 0.9.0"
	@echo "  - Lua 5.1 or LuaJIT"
	@echo "  - busted (luarocks install busted)"
	@echo "  - luacheck (luarocks install luacheck)"
	@echo "  - luacov (luarocks install luacov)"

# Run tests using busted
test:
	@echo "Running tests..."
	@nvim --headless -u tests/minimal_init.lua -c "PlenaryBustedDirectory tests/ { minimal_init = 'tests/minimal_init.lua' }"

# Run specific test file
test-file:
	@echo "Running test file: $(FILE)"
	@nvim -l tests/$(FILE)_spec.lua

# Run linter
lint:
	@echo "Running luacheck..."
	@luacheck lua/ --std lua51 --globals vim --no-unused-args --no-undefined

# Generate test coverage
coverage:
	@echo "Generating test coverage..."
	@rm -f luacov.stats.out luacov.report.out
	@busted --coverage tests/
	@luacov
	@echo "Coverage report generated in luacov.report.out"

# Generate documentation
docs:
	@echo "Generating documentation..."
	@nvim --headless -c "helptags doc" -c "quit"
	@echo "Help tags generated"

# Clean generated files
clean:
	@echo "Cleaning generated files..."
	@rm -f luacov.stats.out luacov.report.out
	@rm -rf tests/output
	@rm -f doc/tags
	@echo "Clean complete"

# Watch files and auto-run tests
watch:
	@echo "Watching for changes..."
	@find lua/ tests/ -name "*.lua" | entr -c make test

# Run health check
check-health:
	@echo "Running health check..."
	@nvim --headless -c "checkhealth para-organize" -c "quit"

# Format code
format:
	@echo "Formatting Lua files..."
	@stylua lua/ tests/ --config-path=.stylua.toml

# Create test fixtures
fixtures:
	@echo "Creating test fixtures..."
	@mkdir -p tests/fixtures/vault/{projects,areas,resources,archives}
	@mkdir -p tests/fixtures/vault/capture/raw_capture
	@echo "---\ntimestamp: 2024-01-01T00:00:00Z\ntags: [test]\n---\n# Test Note" > tests/fixtures/vault/capture/raw_capture/test.md
	@echo "Test fixtures created"

# Development setup
dev-setup: install-deps fixtures
	@echo "Development environment ready!"

# Run minimal Neovim with plugin loaded
run:
	@nvim -u tests/minimal_init.lua

# Help target
help:
	@echo "para-organize.nvim Makefile"
	@echo "============================"
	@echo ""
	@echo "Available targets:"
	@echo "  make test          - Run all tests"
	@echo "  make test-file FILE=<name> - Run specific test file"
	@echo "  make lint          - Run luacheck linter"
	@echo "  make coverage      - Generate test coverage report"
	@echo "  make docs          - Generate documentation"
	@echo "  make clean         - Clean generated files"
	@echo "  make watch         - Watch files and auto-test"
	@echo "  make check-health  - Run Neovim health check"
	@echo "  make format        - Format code with stylua"
	@echo "  make fixtures      - Create test fixtures"
	@echo "  make dev-setup     - Setup development environment"
	@echo "  make run           - Run Neovim with plugin loaded"
	@echo "  make help          - Show this help message"
