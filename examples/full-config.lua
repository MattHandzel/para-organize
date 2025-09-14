-- Full configuration for para-organize.nvim
-- Copy and customize according to your needs

return {
  -- Setup with lazy.nvim
  {
    "para-organize.nvim",
    dependencies = {
      "nvim-lua/plenary.nvim",
      "nvim-telescope/telescope.nvim",
      "MunifTanjim/nui.nvim",
      "folke/which-key.nvim",
      "nvim-tree/nvim-web-devicons",
    },
    config = function()
      require("para-organize").setup({
        -- Path configuration
        paths = {
          -- Base directory for your notes vault
          vault_dir = "~/notes",
          
          -- Relative path from vault_dir where captured notes are stored
          capture_folder = "capture/raw_capture",
          
          -- PARA folder structure (relative to vault_dir)
          para_folders = {
            projects = "projects",
            areas = "areas",
            resources = "resources",
            archives = "archives",
          },
          
          -- Where to archive processed captures (relative to archives folder)
          archive_capture_path = "capture/raw_capture",
        },

        -- File patterns and parsing
        patterns = {
          -- Glob pattern for note files
          file_glob = "**/*.md",
          
          -- Frontmatter delimiters
          frontmatter_delimiters = { "---", "---" },
          
          -- How to extract aliases from frontmatter
          alias_extraction = "aliases",
          
          -- Tag normalization rules
          tag_normalization = {
            ["project"] = "projects",
            ["area"] = "areas",
            ["resource"] = "resources",
          },
          
          -- Case sensitivity for matching
          case_sensitive = false,
        },

        -- Indexing configuration
        indexing = {
          -- Patterns to ignore when scanning
          ignore_patterns = {
            "*.tmp",
            ".git",
            "node_modules",
            ".obsidian",
          },
          
          -- Maximum file size to index (in bytes)
          max_file_size = 1048576, -- 1MB
          
          -- Debounce time for incremental updates (milliseconds)
          incremental_debounce = 500,
          
          -- Backend for storing index
          backend = "json", -- "json" or "sqlite"
          
          -- Auto-reindex on BufWritePost
          auto_reindex = true,
        },

        -- Suggestion engine configuration
        suggestions = {
          -- Weight multipliers for different matching types
          weights = {
            exact_tag_match = 2.0,      -- Tag exactly matches folder name
            normalized_tag_match = 1.5,  -- Tag matches after normalization
            learned_association = 1.8,   -- Previously successful pattern
            source_match = 1.3,          -- Source matches folder
            alias_similarity = 1.1,      -- Alias similar to folder name
            context_match = 1.0,         -- Context matches folder
          },
          
          -- Learning system parameters
          learning = {
            recency_decay = 0.9,       -- How quickly old patterns lose weight
            frequency_boost = 1.2,      -- Boost for frequently used patterns
            min_confidence = 0.3,       -- Minimum confidence to show suggestion
            max_history = 1000,         -- Maximum learning records to keep
          },
          
          -- Maximum number of suggestions to show
          max_suggestions = 10,
          
          -- Always show archive option
          always_show_archive = true,
        },

        -- UI configuration
        ui = {
          -- Layout type: "float" or "split"
          layout = "float",
          
          -- Window dimensions (for float layout)
          float_opts = {
            width = 0.9,      -- Percentage of editor width
            height = 0.8,     -- Percentage of editor height
            border = "rounded",
            position = "center",
          },
          
          -- Split configuration
          split_opts = {
            direction = "vertical",
            size = 0.5, -- 50% split
          },
          
          -- Visual options
          icons = {
            enabled = true,
            project = "",
            area = "",
            resource = "",
            archive = "🗑",
            folder = "",
            file = "",
            tag = "",
          },
          
          -- Display options
          display = {
            show_scores = true,          -- Show confidence scores
            show_counts = true,          -- Show file counts in folders
            show_timestamps = true,      -- Show note timestamps
            timestamp_format = "%b %d, %I:%M %p", -- Sep 10, 11:57 AM
            hide_capture_id = true,      -- Hide capture_id in aliases
            hide_modalities = true,      -- Hide modalities field
            hide_location = true,        -- Hide location field
          },
          
          -- Colors and highlights
          highlights = {
            selected = "Visual",
            project = "Function",
            area = "Keyword",
            resource = "String",
            archive = "Comment",
            tag = "Label",
            score_high = "DiagnosticOk",
            score_medium = "DiagnosticWarn",
            score_low = "DiagnosticHint",
          },
          
          -- Auto-move to newly created folders
          auto_move_to_new_folder = true,
        },

        -- Telescope configuration
        telescope = {
          -- Theme: "dropdown", "ivy", "cursor", or custom
          theme = "dropdown",
          
          -- Layout strategy
          layout_strategy = "horizontal",
          layout_config = {
            horizontal = {
              preview_width = 0.5,
            },
          },
          
          -- Enable multi-select for batch operations
          multi_select = true,
          
          -- Show preview by default
          previewer = true,
        },

        -- File operations configuration
        file_ops = {
          -- Use atomic writes (write to temp, then rename)
          atomic_writes = true,
          
          -- Create backups before operations
          create_backups = true,
          
          -- Backup directory (relative to vault)
          backup_dir = ".backups",
          
          -- Operation log file
          log_operations = true,
          log_file = vim.fn.stdpath("data") .. "/para-organize/operations.log",
          
          -- Auto-create destination folders if they don't exist
          auto_create_folders = true,
        },

        -- Keymaps (set to false to disable default mappings)
        keymaps = {
          -- Global mappings (disabled by default, use <Plug> mappings)
          global = false,
          
          -- Buffer-local mappings in organize UI
          buffer = {
            accept = "<CR>",          -- Accept selected suggestion
            cancel = "<Esc>",         -- Cancel and close
            next = "<Tab>",           -- Next capture
            prev = "<S-Tab>",         -- Previous capture
            skip = "s",               -- Skip current capture
            archive = "a",            -- Archive immediately
            merge = "m",              -- Enter merge mode
            search = "/",             -- Open search
            new_project = "<leader>np", -- Create new project
            new_area = "<leader>na",    -- Create new area
            new_resource = "<leader>nr", -- Create new resource
            refresh = "r",            -- Refresh suggestions
            toggle_preview = "p",     -- Toggle preview pane
            help = "?",               -- Show help
          },
        },

        -- Development and debugging
        debug = {
          -- Enable debug logging
          enabled = false,
          
          -- Log level: "trace", "debug", "info", "warn", "error"
          log_level = "info",
          
          -- Log file location
          log_file = vim.fn.stdpath("cache") .. "/para-organize.log",
        },
      })
      
      -- Custom mappings
      vim.keymap.set('n', '<leader>po', '<cmd>ParaOrganize start<cr>', { desc = "Start PARA organize" })
      vim.keymap.set('n', '<leader>pr', '<cmd>ParaOrganize reindex<cr>', { desc = "Reindex notes" })
      vim.keymap.set('n', '<leader>ps', '<cmd>ParaOrganize search<cr>', { desc = "Search notes" })
      
      -- Register with which-key if available
      local ok, wk = pcall(require, "which-key")
      if ok then
        wk.register({
          ["<leader>p"] = {
            name = "PARA",
            o = { "<cmd>ParaOrganize start<cr>", "Organize notes" },
            r = { "<cmd>ParaOrganize reindex<cr>", "Reindex notes" },
            s = { "<cmd>ParaOrganize search<cr>", "Search notes" },
            f = { "<cmd>ParaOrganize start status=raw<cr>", "Filter raw notes" },
            t = { "<cmd>ParaOrganize start tags=meeting<cr>", "Filter meeting notes" },
          }
        })
      end
    end,
  }
}
