{
  description = "Development environment for para-organize.nvim";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    
    # Neovim nightly for testing
    neovim-nightly-overlay = {
      url = "github:nix-community/neovim-nightly-overlay";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, flake-utils, neovim-nightly-overlay }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        overlays = [ neovim-nightly-overlay.overlay ];
        pkgs = import nixpkgs {
          inherit system overlays;
        };

        # Lua packages for testing
        luaPackages = pkgs.lua51Packages;
        
        # Development shell
        devShell = pkgs.mkShell {
          buildInputs = with pkgs; [
            # Neovim (using stable, but nightly available as neovim-nightly)
            neovim
            
            # Lua 5.1 (matching Neovim's LuaJIT compatibility)
            lua5_1
            luaPackages.busted
            luaPackages.luacheck
            luaPackages.luacov
            
            # Development tools
            git
            ripgrep
            fd
            bat
            jq
            curl
            gnumake
            
            # Documentation tools
            pandoc
            
            # Optional: Language servers for development
            lua-language-server
            nodePackages.prettier
            nodePackages.markdownlint-cli
            
            # For running tests
            entr  # File watcher for auto-testing
          ];

          shellHook = ''
            echo "para-organize.nvim Development Environment"
            echo "=========================================="
            echo ""
            echo "Neovim version: $(nvim --version | head -n1)"
            echo "Lua version: $(lua -v)"
            echo ""
            echo "Available commands:"
            echo "  make test      - Run all tests"
            echo "  make lint      - Run linter"
            echo "  make coverage  - Generate test coverage"
            echo "  make watch     - Watch files and auto-test"
            echo "  make docs      - Generate documentation"
            echo ""
            echo "Neovim plugin development tips:"
            echo "  - Use :checkhealth para-organize to verify setup"
            echo "  - Enable debug mode in config for detailed logs"
            echo "  - Run individual test files with: nvim -l tests/<file>_spec.lua"
            echo ""
            
            # Set up test environment variables
            export PARA_ORGANIZE_TEST_MODE=1
            export NVIM_TEST_DIRECTORY="$(pwd)/tests"
            
            # Ensure test directories exist
            mkdir -p tests/fixtures
            mkdir -p tests/output
            
            # Create convenience aliases
            alias nvim-test="nvim --headless -u tests/minimal_init.lua"
            alias watch-tests="find . -name '*.lua' | entr -c make test"
          '';

          # Environment variables
          LUA_PATH = "${luaPackages.busted}/share/lua/5.1/?.lua;${luaPackages.busted}/share/lua/5.1/?/init.lua;./lua/?.lua;./lua/?/init.lua;;";
          LUA_CPATH = "${luaPackages.busted}/lib/lua/5.1/?.so;;";
        };

        # Test runner script
        testRunner = pkgs.writeScriptBin "test-para-organize" ''
          #!${pkgs.bash}/bin/bash
          set -e
          
          echo "Running para-organize.nvim tests..."
          
          # Run busted tests
          ${luaPackages.busted}/bin/busted \
            --lua=${pkgs.lua5_1}/bin/lua \
            --output=utf \
            --verbose \
            tests/
        '';

        # Lint script
        lintRunner = pkgs.writeScriptBin "lint-para-organize" ''
          #!${pkgs.bash}/bin/bash
          set -e
          
          echo "Linting para-organize.nvim..."
          
          # Run luacheck
          ${luaPackages.luacheck}/bin/luacheck \
            --std=lua51 \
            --globals=vim \
            --no-unused-args \
            lua/
        '';

      in
      {
        devShells.default = devShell;
        
        # Provide test runner as package
        packages = {
          test = testRunner;
          lint = lintRunner;
        };

        # Development app for quick testing
        apps.test = {
          type = "app";
          program = "${testRunner}/bin/test-para-organize";
        };

        apps.lint = {
          type = "app";
          program = "${lintRunner}/bin/lint-para-organize";
        };
      });
}
