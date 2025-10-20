-- tests/integration_spec.lua
local helpers = require("tests.helpers")

describe("integration", function()
  local indexer, suggest, move, learn, config

  -- Reset modules before each test for isolation
  before_each(function()
    package.loaded["para-organize.indexer"] = nil
    package.loaded["para-organize.config"] = nil
    package.loaded["para-organize.suggest"] = nil
    package.loaded["para-organize.move"] = nil
    package.loaded["para-organize.learn"] = nil

    config = require("para-organize.config")
    indexer = require("para-organize.indexer")
    suggest = require("para-organize.suggest")
    move = require("para-organize.move")
    learn = require("para-organize.learn")

    -- Clear index and learning data
    indexer.clear()
    learn.clear()
    helpers.clean_test_output()
  end)

  it("runs full indexing process on the test vault", function()
    -- This test relies on the test vault being populated by minimal_init.lua
    -- It's more of a sanity check for the overall indexing.
    local promise = indexer.full_reindex()
    assert.is_not_nil(promise)

    -- Wait for indexing to complete
    promise:block()

    local stats = indexer.get_statistics()
    -- Since the test vault is a copy of the real vault, we expect many notes.
    assert.is_true(stats.total > 0, "Indexer should find notes in the test vault")
  end)

  it("generates suggestions for a capture note", function()
    -- 1. Create a destination note for the suggestion engine to find
    local dest_path, _ = helpers.create_temp_file({
      dir = config.get().paths.vault_dir .. "/projects/ProjectX",
      name = "target_note.md",
      content = [[
---
tags: [project-x, important]
---
# Project X Main Note
      ]]
    })
    indexer.index_file(dest_path)

    -- 2. Create the capture note we want to organize
    local capture_path, _ = helpers.create_temp_file({
      name = "capture_for_suggestion.md",
      content = [[
---
tags: [project-x, meeting]
---
# Meeting Notes
      ]]
    })
    local capture_note = indexer.extract_metadata(capture_path)
    assert.is_not_nil(capture_note)

    -- 3. Generate suggestions
    local suggestions = suggest.generate_suggestions(capture_note)
    assert.is_not_nil(suggestions)
    assert.is_true(#suggestions > 0, "Should generate at least one suggestion")

    -- 4. Check if the top suggestion is the one we created
    local top_suggestion = suggestions[1]
    assert.are.equal(dest_path, top_suggestion.path)

    -- 5. Clean up
    os.remove(dest_path)
    os.remove(capture_path)
  end)

  it("moves capture to suggested destination and learns from it", function()
    -- 1. Setup: Create destination and capture notes
    local dest_dir = config.get().paths.vault_dir .. "/areas/Productivity"
    local capture_path, _ = helpers.create_temp_file({
      name = "learning_test_capture.md",
      content = [[
---
tags: [gtd, productivity]
---
# GTD Weekly Review
      ]]
    })
    local capture_note = indexer.extract_metadata(capture_path)
    assert.is_not_nil(capture_note)

    -- 2. Perform the move
    local success, final_path = move.move_to_destination(capture_note, dest_dir)
    assert.is_true(success, "Move operation should succeed")
    assert.is_not_nil(final_path)
    assert.is_true(vim.fn.filereadable(final_path) == 1, "Destination file should exist")

    -- 3. Verify that the original capture file was archived
    -- (The move function archives automatically)
    assert.is_true(vim.fn.filereadable(capture_path) == 0, "Original capture file should be gone")

    -- 4. Verify that the learning module recorded the move
    local features = learn.extract_features(capture_note)
    local score = learn.get_association_score(features, dest_dir)
    assert.is_true(score > 0, "Learning module should have a score for the new association")

    -- 5. Clean up
    os.remove(final_path)
  end)
end)
