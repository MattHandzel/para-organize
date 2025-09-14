-- Minimal configuration for para-organize.nvim
-- Copy and paste this into your Neovim config

return {
  -- Basic setup with lazy.nvim
  {
    "para-organize.nvim",
    dependencies = {
      "nvim-lua/plenary.nvim",
      "nvim-telescope/telescope.nvim",
      "MunifTanjim/nui.nvim",
      -- Optional
      "folke/which-key.nvim",
      "nvim-tree/nvim-web-devicons",
    },
    config = function()
      require("para-organize").setup({
        -- Only necessary configuration
        paths = {
          -- Set this to your notes vault directory
          vault_dir = "~/notes",
          
          -- Where your captured notes are stored (relative to vault_dir)
          capture_folder = "capture/raw_capture",
          
          -- PARA folder structure (relative to vault_dir)
          para_folders = {
            projects = "projects",
            areas = "areas",
            resources = "resources",
            archives = "archives",
          },
        },
      })
      
      -- Optional: Add global mappings
      vim.keymap.set('n', '<leader>po', '<cmd>ParaOrganize start<cr>', { desc = "Start PARA organize" })
      vim.keymap.set('n', '<leader>pr', '<cmd>ParaOrganize reindex<cr>', { desc = "Reindex notes" })
    end,
  }
}
