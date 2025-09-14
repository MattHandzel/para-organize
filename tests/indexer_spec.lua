-- indexer_spec.lua
-- Tests for indexer.lua module

describe("indexer", function()
  local indexer
  local utils
  local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ":h")
  local test_vault = test_dir .. "/fixtures/vault"
  local test_capture = test_vault .. "/capture/raw_capture/test_capture.md"
  
  before_each(function()
    -- Set up test configuration
    require("para-organize.config").setup({
      paths = {
        vault_dir = test_vault,
        capture_folder = "capture/raw_capture",
      }
    })
    
    indexer = require("para-organize.indexer")
    utils = require("para-organize.utils")
    
    -- Ensure test capture exists
    if vim.fn.filereadable(test_capture) == 0 then
      local test_content = [[---
timestamp: 2024-01-01T12:00:00Z
tags:
  - test
  - example
sources:
  - test_source
modalities:
  - text
processing_status: raw
---

# Test Capture

This is a test capture file for testing the para-organize plugin.
]]
      local file = io.open(test_capture, "w")
      if file then
        file:write(test_content)
        file:close()
      end
    end
  end)
  
  describe("get_para_type", function()
    it("detects capture type", function()
      local type = indexer.get_para_type(test_capture)
      assert.equals("capture", type)
    end)
    
    it("detects projects type", function()
      local test_project = test_vault .. "/projects/test_project.md"
      local type = indexer.get_para_type(test_project)
      assert.equals("projects", type)
    end)
    
    it("detects areas type", function()
      local test_area = test_vault .. "/areas/test_area.md"
      local type = indexer.get_para_type(test_area)
      assert.equals("areas", type)
    end)
  end)
  
  describe("extract_metadata", function()
    it("extracts metadata from capture file", function()
      local metadata = indexer.extract_metadata(test_capture)
      
      assert.is_not_nil(metadata)
      assert.equals("2024-01-01T12:00:00Z", metadata.timestamp)
      assert.same({"test", "example"}, metadata.tags)
      assert.same({"test_source"}, metadata.sources)
      assert.same({"text"}, metadata.modalities)
      assert.equals("raw", metadata.processing_status)
      assert.equals("Test Capture", metadata.title)
    end)
    
    it("handles missing frontmatter", function()
      local test_no_fm = test_vault .. "/test_no_frontmatter.md"
      local file = io.open(test_no_fm, "w")
      if file then
        file:write("# No Frontmatter\n\nThis file has no frontmatter.")
        file:close()
      end
      
      local metadata = indexer.extract_metadata(test_no_fm)
      assert.is_not_nil(metadata)
      assert.equals("No Frontmatter", metadata.title)
      assert.is_nil(metadata.timestamp)
      assert.same({}, metadata.tags)
    end)
  end)
  
  describe("index operations", function()
    it("can index a single file", function()
      local success = indexer.index_file(test_capture)
      assert.is_true(success)
      
      local note = indexer.get_note(test_capture)
      assert.is_not_nil(note)
      assert.equals("Test Capture", note.title)
    end)
    
    it("can search by criteria", function()
      -- Make sure file is indexed
      indexer.index_file(test_capture)
      
      -- Search by tag
      local results = indexer.search({tags = {"test"}})
      assert.is_true(#results > 0)
      assert.equals("Test Capture", results[1].title)
      
      -- Search by source
      results = indexer.search({sources = {"test_source"}})
      assert.is_true(#results > 0)
      
      -- Search by status
      results = indexer.search({status = "raw"})
      assert.is_true(#results > 0)
      
      -- Search by query
      results = indexer.search({query = "Test"})
      assert.is_true(#results > 0)
    end)
  end)
  
  describe("index persistence", function()
    it("can save and load index", function()
      -- Clear index first
      indexer.clear()
      
      -- Index test file
      indexer.index_file(test_capture)
      
      -- Save index
      indexer.save_index()
      
      -- Clear in-memory index
      indexer.clear()
      
      -- Load from disk
      indexer.load_index()
      
      -- Verify data was loaded
      local note = indexer.get_note(test_capture)
      assert.is_not_nil(note)
      assert.equals("Test Capture", note.title)
    end)
  end)
  
  after_each(function()
    -- Clean up any test files we created
    local test_no_fm = test_vault .. "/test_no_frontmatter.md"
    if vim.fn.filereadable(test_no_fm) == 1 then
      os.remove(test_no_fm)
    end
  end)
end)
