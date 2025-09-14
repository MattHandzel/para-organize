-- tests/move_spec.lua
local helpers = require("tests.helpers")

describe("move", function()
  local move, config, indexer

  before_each(function()
    package.loaded["para-organize.move"] = nil
    package.loaded["para-organize.config"] = nil
    package.loaded["para-organize.indexer"] = nil

    move = require("para-organize.move")
    config = require("para-organize.config")
    indexer = require("para-organize.indexer")

    helpers.clean_test_output()
    -- Clear any previous index data
    indexer.clear()
  end)

  it("updates frontmatter correctly", function()
    local file_path, _ = helpers.create_temp_file({
      content = [[
---
tags: [original]
---
# Original Content
      ]]
    })

    local success = move.update_frontmatter(file_path, {
      tags = { "original", "updated" },
      status = "processed",
    })

    assert.is_true(success, "update_frontmatter should return true")

    local content = require("para-organize.utils").read_file(file_path)
    assert.is_not_nil(content)
    assert.matches("status: processed", content, nil, true)
    assert.matches("tags:", content, nil, true)
    assert.matches("- original", content, nil, true)
    assert.matches("- updated", content, nil, true)

    os.remove(file_path)
  end)

  it("moves a capture note to a destination", function()
    local capture_path, _ = helpers.create_temp_file({ name = "move_test.md" })
    local capture_note = indexer.extract_metadata(capture_path)
    local dest_dir = config.get().paths.vault_dir .. "/projects/MovedNotes"

    local success, final_path = move.move_to_destination(capture_note, dest_dir)

    assert.is_true(success, "move_to_destination should succeed")
    assert.is_not_nil(final_path)
    assert.matches("MovedNotes/move_test.md", final_path, nil, true)
    assert.is_true(vim.fn.filereadable(final_path) == 1, "Final file should exist")
    assert.is_true(vim.fn.filereadable(capture_path) == 0, "Original capture should be removed")

    -- Verify the original was archived
    local archive_path = move.get_last_archived_path()
    assert.is_not_nil(archive_path)
    assert.is_true(vim.fn.filereadable(archive_path) == 1, "Archived file should exist")

    os.remove(final_path)
    os.remove(archive_path)
  end)

  it("merges content into an existing note", function()
    -- 1. Create source and target files
    local source_path, _ = helpers.create_temp_file({
      name = "merge_source.md",
      content = "# Source Content\n\nThis should be merged.",
    })
    local target_path, _ = helpers.create_temp_file({
      name = "merge_target.md",
      content = "# Target Content\n\nOriginal text.",
    })

    -- 2. Perform the merge
    local success = move.merge_into_note(source_path, target_path)
    assert.is_true(success, "merge_into_note should succeed")

    -- 3. Verify content of target file
    local target_content = require("para-organize.utils").read_file(target_path)
    assert.matches("Source Content", target_content, nil, true)
    assert.matches("Original text", target_content, nil, true)

    -- 4. Verify source file was archived
    assert.is_true(vim.fn.filereadable(source_path) == 0, "Source file should be removed")
    local archive_path = move.get_last_archived_path()
    assert.is_not_nil(archive_path)
    assert.is_true(vim.fn.filereadable(archive_path) == 1, "Archived source should exist")

    -- 5. Clean up
    os.remove(target_path)
    os.remove(archive_path)
  end)
end)