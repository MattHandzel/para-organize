-- move_spec.lua
-- Tests for move.lua module

describe("move", function()
  local move
  local utils
  local config
  local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ":h")
  local test_vault = test_dir .. "/fixtures/vault"
  local test_output = test_dir .. "/output"
  
  before_each(function()
    -- Set up test configuration
    config = require("para-organize.config")
    config.setup({
      paths = {
        vault_dir = test_vault,
        capture_folder = "capture/raw_capture",
      },
      file_ops = {
        atomic_writes = true,
        create_backups = true,
        backup_dir = ".backups",
        log_operations = true,
        log_file = test_output .. "/operations.log",
        auto_create_folders = true,
      }
    })
    
    move = require("para-organize.move")
    utils = require("para-organize.utils")
    
    -- Create test capture file
    local test_capture = test_vault .. "/capture/raw_capture/move_test.md"
    local test_content = [[---
timestamp: 2024-01-01T12:00:00Z
tags:
  - test
  - move
sources:
  - test_source
---

# Move Test

This is a test file for testing move operations.
]]
    utils.write_file_atomic(test_capture, test_content)
  end)
  
  describe("create_backup", function()
    it("creates a backup of a file", function()
      local test_file = test_vault .. "/capture/raw_capture/backup_test.md"
      utils.write_file_atomic(test_file, "Test content for backup")
      
      local success, backup_path = move.create_backup(test_file)
      assert.is_true(success)
      assert.is_not_nil(backup_path)
      assert.is_true(vim.fn.filereadable(backup_path) == 1)
      
      -- Check backup content
      local content = utils.read_file(backup_path)
      assert.equals("Test content for backup", content)
      
      -- Clean up
      os.remove(backup_path)
    end)
  end)
  
  describe("update_tags", function()
    it("adds tags to frontmatter", function()
      local test_file = test_vault .. "/capture/raw_capture/tag_test.md"
      local test_content = [[---
timestamp: 2024-01-01T12:00:00Z
tags:
  - existing
---

# Tag Test
]]
      utils.write_file_atomic(test_file, test_content)
      
      local new_tags = {"project/test", "added-tag"}
      local success = move.update_tags(test_file, new_tags)
      
      assert.is_true(success)
      
      -- Read file and check tags
      local content = utils.read_file(test_file)
      local frontmatter, _ = utils.extract_frontmatter(content)
      local data = utils.parse_yaml_simple(frontmatter)
      
      assert.is_not_nil(data.tags)
      assert.is_true(#data.tags >= 3)
      
      local has_project_tag = false
      local has_existing_tag = false
      
      for _, tag in ipairs(data.tags) do
        if tag == "project/test" then has_project_tag = true end
        if tag == "existing" then has_existing_tag = true end
      end
      
      assert.is_true(has_project_tag)
      assert.is_true(has_existing_tag)
      
      -- Clean up
      os.remove(test_file)
    end)
  end)
  
  describe("move_to_destination", function()
    it("moves file to destination and archives original", function()
      local source_file = test_vault .. "/capture/raw_capture/move_test.md"
      local dest_folder = test_vault .. "/projects/test-move"
      
      -- Ensure source exists
      assert.is_true(vim.fn.filereadable(source_file) == 1)
      
      -- Destination folder may not exist yet
      if vim.fn.isdirectory(dest_folder) == 0 then
        vim.fn.mkdir(dest_folder, "p")
      end
      
      local success, dest_path = move.move_to_destination(source_file, dest_folder)
      
      assert.is_true(success)
      assert.is_not_nil(dest_path)
      
      -- Check destination file exists
      assert.is_true(vim.fn.filereadable(dest_path) == 1)
      
      -- Original should be gone (archived)
      assert.is_true(vim.fn.filereadable(source_file) == 0)
      
      -- Check content has correct tags
      local content = utils.read_file(dest_path)
      local frontmatter, _ = utils.extract_frontmatter(content)
      local data = utils.parse_yaml_simple(frontmatter)
      
      local has_project_tag = false
      for _, tag in ipairs(data.tags or {}) do
        if tag == "project/test-move" then 
          has_project_tag = true
          break
        end
      end
      
      assert.is_true(has_project_tag)
      
      -- Clean up
      os.remove(dest_path)
    end)
  end)
  
  describe("archive_capture", function()
    it("archives a capture file", function()
      local test_file = test_vault .. "/capture/raw_capture/archive_test.md"
      utils.write_file_atomic(test_file, "Test content for archiving")
      
      local archive_path = move.archive_capture(test_file)
      
      assert.is_not_nil(archive_path)
      assert.is_true(vim.fn.filereadable(archive_path) == 1)
      assert.is_true(vim.fn.filereadable(test_file) == 0)
      
      -- Clean up
      os.remove(archive_path)
    end)
  end)
  
  describe("merge_into_note", function()
    it("merges content into existing note", function()
      local source_file = test_vault .. "/capture/raw_capture/merge_source.md"
      local target_file = test_vault .. "/projects/merge_target.md"
      
      -- Create source file
      utils.write_file_atomic(source_file, [[---
title: Source Note
tags:
  - source
  - merge
---

# Source Content

This is source content.]])
      
      -- Create target file
      utils.write_file_atomic(target_file, [[---
title: Target Note
tags:
  - target
---

# Target Content

This is target content.]])
      
      local success, _ = move.merge_into_note(source_file, target_file)
      
      assert.is_true(success)
      
      -- Check target file has merged content
      local content = utils.read_file(target_file)
      assert.is_true(content:find("Source Content") ~= nil)
      assert.is_true(content:find("Target Content") ~= nil)
      
      -- Check merged tags
      local frontmatter, _ = utils.extract_frontmatter(content)
      local data = utils.parse_yaml_simple(frontmatter)
      
      local has_source_tag = false
      local has_target_tag = false
      
      for _, tag in ipairs(data.tags or {}) do
        if tag == "source" then has_source_tag = true end
        if tag == "target" then has_target_tag = true end
      end
      
      assert.is_true(has_source_tag)
      assert.is_true(has_target_tag)
      
      -- Source file should be gone (archived)
      assert.is_true(vim.fn.filereadable(source_file) == 0)
      
      -- Clean up
      os.remove(target_file)
    end)
  end)
  
  describe("operation_log", function()
    it("logs operations", function()
      local source = test_vault .. "/capture/raw_capture/log_test.md"
      local destination = test_vault .. "/projects/log_test"
      
      -- Create test file
      utils.write_file_atomic(source, "Test content")
      
      -- Log an operation
      move.log_operation("test", source, destination, true, nil)
      
      -- Get recent operations
      local operations = move.get_recent_operations(1)
      assert.is_not_nil(operations)
      assert.equals(1, #operations)
      
      local op = operations[1]
      assert.equals("test", op.type)
      assert.equals(source, op.source)
      assert.equals(destination, op.destination)
      assert.is_true(op.success)
      
      -- Clean up
      os.remove(source)
    end)
  end)
  
  after_each(function()
    -- Clean up any remaining test files
    local test_files = {
      test_vault .. "/capture/raw_capture/move_test.md",
      test_vault .. "/capture/raw_capture/backup_test.md",
      test_vault .. "/capture/raw_capture/archive_test.md",
      test_vault .. "/capture/raw_capture/merge_source.md",
      test_vault .. "/projects/merge_target.md",
      test_vault .. "/capture/raw_capture/tag_test.md",
      test_vault .. "/capture/raw_capture/log_test.md",
    }
    
    for _, file in ipairs(test_files) do
      if vim.fn.filereadable(file) == 1 then
        os.remove(file)
      end
    end
    
    -- Clean up test folders
    local folders = {
      test_vault .. "/projects/test-move"
    }
    
    for _, folder in ipairs(folders) do
      if vim.fn.isdirectory(folder) == 1 then
        vim.fn.delete(folder, "rf")
      end
    end
  end)
end)
