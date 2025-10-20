-- tests/indexer_spec.lua
local helpers = require("tests.helpers")

describe("indexer", function()
  local indexer
  local config

  -- Before each test, reset modules to ensure isolation
  before_each(function()
    package.loaded["para-organize.indexer"] = nil
    package.loaded["para-organize.config"] = nil
    indexer = require("para-organize.indexer")
    config = require("para-organize.config")
    -- The test vault is configured in minimal_init.lua
  end)

  -- Clean up any created files
  after_each(function()
    helpers.clean_test_output()
  end)

  describe("get_para_type", function()
    it("detects capture type", function()
      local path = config.get().paths.vault_dir .. "/capture/raw_capture/test.md"
      assert.are.equal("capture", indexer.get_para_type(path))
    end)

    it("detects projects type", function()
      local path = config.get().paths.vault_dir .. "/projects/MyProject/note.md"
      assert.are.equal("projects", indexer.get_para_type(path))
    end)

    it("detects areas type", function()
      local path = config.get().paths.vault_dir .. "/areas/Health/workout.md"
      assert.are.equal("areas", indexer.get_para_type(path))
    end)
  end)

  describe("extract_metadata", function()
    it("extracts metadata from a valid file", function()
      -- Create a temporary file for this test to ensure it's self-contained
      local test_file, err = helpers.create_temp_file({
        content = [[
---
id: spec-test-001
timestamp: 2024-01-01T12:00:00Z
tags:
  - spec
  - test
---
# Spec Test File

This is the body.
]],
      })
      assert.is_not_nil(test_file, "Failed to create temp file: " .. tostring(err))

      local metadata = indexer.extract_metadata(test_file)

      -- Assert that metadata was actually extracted
      assert.is_not_nil(metadata, "extract_metadata should not return nil for a valid file")

      -- Assert specific fields
      assert.are.equal("Spec Test File", metadata.title)
      assert.are.equal("spec-test-001", metadata.id)
      assert.are.same({ "spec", "test" }, metadata.tags)

      -- Clean up the temporary file
      os.remove(test_file)
    end)

    it("handles missing frontmatter gracefully", function()
      local test_file, err = helpers.create_temp_file({
        content = "# No Frontmatter Here",
      })
      assert.is_not_nil(test_file, "Failed to create temp file: " .. tostring(err))

      local metadata = indexer.extract_metadata(test_file)
      assert.is_not_nil(metadata)
      assert.are.equal("No Frontmatter Here", metadata.title)
      assert.are.same({}, metadata.tags)

      os.remove(test_file)
    end)
  end)

  describe("index operations", function()
    it("can index a single file", function()
      local test_file, _ = helpers.create_temp_file()
      local success = indexer.index_file(test_file)
      assert.is_true(success, "index_file should return true on success")
      local note = indexer.get_note(test_file)
      assert.is_not_nil(note)
      assert.are.equal(note.path, test_file)
      os.remove(test_file)
    end)

    it("can search by criteria", function()
      -- Create a specific file to search for
      local test_file, _ = helpers.create_temp_file({
        content = [[
---
tags: [searchable-tag]
---
# Searchable Note
]]
      })
      indexer.index_file(test_file)

      local results = indexer.search({ tags = "searchable-tag" })
      assert.are.equal(1, #results)
      assert.are.equal(test_file, results[1].path)

      os.remove(test_file)
    end)
  end)
end)
