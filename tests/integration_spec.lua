-- integration_spec.lua
-- Integration tests for para-organize.nvim

describe("integration", function()
  local para_organize
  local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ":h")
  local test_vault = test_dir .. "/fixtures/vault"
  local test_output = test_dir .. "/output"
  
  before_each(function()
    -- Clear any loaded modules to start fresh
    package.loaded["para-organize"] = nil
    package.loaded["para-organize.config"] = nil
    package.loaded["para-organize.indexer"] = nil
    package.loaded["para-organize.search"] = nil
    package.loaded["para-organize.suggest"] = nil
    package.loaded["para-organize.move"] = nil
    package.loaded["para-organize.learn"] = nil
    
    -- Set up test environment
    vim.env.PARA_ORGANIZE_TEST_MODE = "1"
    
    -- Set up plugin with test configuration
    para_organize = require("para-organize")
    para_organize.setup({
      paths = {
        vault_dir = test_vault,
        capture_folder = "capture/raw_capture",
        para_folders = {
          projects = "projects",
          areas = "areas",
          resources = "resources",
          archives = "archives",
        },
      },
      debug = {
        enabled = true,
        log_level = "debug",
        log_file = test_output .. "/para-organize-integration-test.log",
      }
    })
    
    -- Create test capture with meaningful tags and metadata
    local capture_content = [[---
timestamp: 2024-01-01T12:00:00Z
id: test-integration-capture
aliases:
  - Integration Test
  - Capture for Testing
tags:
  - integration
  - test-project
  - meeting
sources:
  - test-source
modalities:
  - text
processing_status: raw
---

# Integration Test Capture

This is a test capture for integration testing.

## Key Points

- Point 1: Test basic functionality
- Point 2: Ensure modules work together
- Point 3: Verify end-to-end workflow
]]

    local utils = require("para-organize.utils")
    local test_capture = test_vault .. "/capture/raw_capture/integration_test.md"
    utils.write_file_atomic(test_capture, capture_content)
    
    -- Create test project folder that matches one of the tags
    local project_folder = test_vault .. "/projects/test-project"
    if vim.fn.isdirectory(project_folder) == 0 then
      vim.fn.mkdir(project_folder, "p")
    end
  end)
  
  it("runs full indexing process", function()
    local indexer = require("para-organize.indexer")
    
    -- Force full reindex
    local reindex_complete = false
    indexer.full_reindex(function(stats)
      assert.is_true(stats.total > 0)
      reindex_complete = true
    end)
    
    -- Wait a bit for async operation
    vim.wait(1000, function() return reindex_complete end)
    assert.is_true(reindex_complete)
    
    -- Check that our test file was indexed
    local test_capture = test_vault .. "/capture/raw_capture/integration_test.md"
    local note = indexer.get_note(test_capture)
    
    assert.is_not_nil(note)
    assert.equals("Integration Test Capture", note.title)
    assert.same({"integration", "test-project", "meeting"}, note.tags)
  end)
  
  it("generates suggestions based on tags", function()
    local indexer = require("para-organize.indexer")
    local suggest = require("para-organize.suggest")
    
    -- Make sure file is indexed
    local test_capture = test_vault .. "/capture/raw_capture/integration_test.md"
    indexer.index_file(test_capture)
    
    local note = indexer.get_note(test_capture)
    assert.is_not_nil(note)
    
    -- Generate suggestions
    local suggestions = suggest.generate_suggestions(note)
    
    -- Should have test-project as a suggestion since folder exists
    local found_project = false
    for _, suggestion in ipairs(suggestions) do
      if suggestion.name == "test-project" and suggestion.type == "projects" then
        found_project = true
        break
      end
    end
    
    assert.is_true(found_project, "test-project should be suggested")
  end)
  
  it("moves capture to suggested destination", function()
    local indexer = require("para-organize.indexer")
    local move = require("para-organize.move")
    local utils = require("para-organize.utils")
    
    -- Make sure file is indexed
    local test_capture = test_vault .. "/capture/raw_capture/integration_test.md"
    indexer.index_file(test_capture)
    
    -- Destination folder
    local dest_folder = test_vault .. "/projects/test-project"
    
    -- Move the file
    local success, dest_path = move.move_to_destination(test_capture, dest_folder)
    
    -- Check move was successful
    assert.is_true(success)
    assert.is_not_nil(dest_path)
    assert.is_true(vim.fn.filereadable(dest_path) == 1)
    
    -- Check file has project tag added
    local content = utils.read_file(dest_path)
    local frontmatter, _ = utils.extract_frontmatter(content)
    local data = utils.parse_yaml_simple(frontmatter)
    
    local has_project_tag = false
    for _, tag in ipairs(data.tags or {}) do
      if tag == "project/test-project" then
        has_project_tag = true
        break
      end
    end
    
    assert.is_true(has_project_tag)
    
    -- Original should be gone (archived)
    assert.is_true(vim.fn.filereadable(test_capture) == 0)
    
    -- But should be in archives
    local archives = test_vault .. "/archives/capture/raw_capture"
    local found = false
    
    if vim.fn.isdirectory(archives) == 1 then
      local cmd = "find " .. archives .. " -name 'integration_test*.md'"
      local handle = io.popen(cmd)
      if handle then
        for line in handle:lines() do
          if line:find("integration_test") then
            found = true
            break
          end
        end
        handle:close()
      end
    end
    
    assert.is_true(found, "Original should be archived")
    
    -- Clean up the moved file
    os.remove(dest_path)
  end)
  
  it("learns from move operations", function()
    local indexer = require("para-organize.indexer")
    local learn = require("para-organize.learn")
    
    -- Clear learning data
    learn.clear()
    
    -- Make sure file is indexed
    local test_capture = test_vault .. "/capture/raw_capture/integration_test.md"
    indexer.index_file(test_capture)
    local note = indexer.get_note(test_capture)
    
    -- Record a move
    local dest_folder = test_vault .. "/projects/test-project"
    learn.record_move(note, dest_folder)
    
    -- Check learning data is updated
    local stats = learn.get_statistics()
    assert.equals(1, stats.total_moves)
    assert.is_not_nil(stats.destinations[dest_folder])
    
    -- Get association score
    local score = learn.get_association_score(note, dest_folder)
    assert.is_true(score > 0)
  end)
  
  after_each(function()
    -- Clean up test files
    local files = {
      test_vault .. "/capture/raw_capture/integration_test.md",
      test_vault .. "/projects/test-project/integration_test.md",
    }
    
    for _, file in ipairs(files) do
      if vim.fn.filereadable(file) == 1 then
        os.remove(file)
      end
    end
    
    -- Also check archives
    local archives = test_vault .. "/archives/capture/raw_capture"
    if vim.fn.isdirectory(archives) == 1 then
      local cmd = "find " .. archives .. " -name 'integration_test*.md' -delete"
      os.execute(cmd)
    end
  end)
end)
