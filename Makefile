# Makefile for running tests

# Define a directory for test dependencies
TEST_DIR := $(CURDIR)/tests
DEPS_DIR := $(TEST_DIR)/site/pack/deps/start

# Phony targets don't represent files
.PHONY: test clean deps

# Default target
all: test

# Run the test suite
test: deps
	@echo "Running tests..."
	NVIM_APPNAME=para-organize-test nvim --headless -u tests/minimal_init.lua -c "PlenaryBustedDirectory tests/spec {minimal_init = 'tests/minimal_init.lua'}"

# Manage test dependencies
deps: \
	$(DEPS_DIR)/plenary.nvim \
	$(DEPS_DIR)/telescope.nvim \
	$(DEPS_DIR)/nui.nvim

# Rules for cloning dependencies
$(DEPS_DIR)/plenary.nvim:
	@mkdir -p $(DEPS_DIR)
	@git clone --depth 1 https://github.com/nvim-lua/plenary.nvim $@

$(DEPS_DIR)/telescope.nvim:
	@mkdir -p $(DEPS_DIR)
	@git clone --depth 1 https://github.com/nvim-telescope/telescope.nvim $@

$(DEPS_DIR)/nui.nvim:
	@mkdir -p $(DEPS_DIR)
	@git clone --depth 1 https://github.com/MunifTanjim/nui.nvim $@

# Clean up test dependencies and temporary files
clean:
	@echo "Cleaning up..."
	@rm -rf $(TEST_DIR)/site
