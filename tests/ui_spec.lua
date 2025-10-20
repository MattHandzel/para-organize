-- tests/ui_spec.lua
local helpers = require("tests.helpers")

describe("UI", function()
  local ui, indexer, config

  before_each(function()
    -- Reset modules for test isolation
    package.loaded["para-organize.ui"] = nil
    package.loaded["para-organize.indexer"] = nil
    package.loaded["para-organize.config"] = nil

    ui = require("para-organize.ui")
    indexer = require("para-organize.indexer")
    config = require("para-organize.config")

    helpers.clean_test_output()
    indexer.clear()
  end)

  after_each(function()
    -- Ensure the UI is closed after each test to prevent state leakage
    pcall(ui.close)
  end)

  it("can open a folder and display its contents without error", function()
    -- 1. ARRANGE: Create a predictable file structure for the test
    local vault = config.get().paths.vault_dir
    local project_dir = vault .. "/projects/UITestProject"
    vim.fn.mkdir(project_dir, "p")
    helpers.create_temp_file({
      dir = project_dir,
      name = "note.md",
      content = "# UI Test Note",
    })

    -- Index the new structure
    local promise = indexer.full_reindex()
    promise:block()

    -- 2. ACT: Launch the UI with a dummy capture file
    local capture_path, _ = helpers.create_temp_file({ name = "ui_test_capture.md" })
    ui.launch(capture_path)

    -- Wait for the UI to be fully rendered
    vim.wait(200, function()
      return ui.is_open() and #vim.api.nvim_buf_get_lines(ui.get_organize_bufnr(), 0, -1, false) > 1
    end, 10, 1000)
    assert.is_true(ui.is_open(), "UI should be open")

    -- Find the line corresponding to our test project
    local org_bufnr = ui.get_organize_bufnr()
    local lines = vim.api.nvim_buf_get_lines(org_bufnr, 0, -1, false)
    local project_line_idx = -1
    for i, line in ipairs(lines) do
      if line:match("UITestProject") then
        project_line_idx = i
        break
      end
    end
    assert.is_not_equal(-1, project_line_idx, "Test project folder should be visible in the UI")

    -- Simulate moving the cursor to the project line and pressing Enter
    local org_winid = ui.get_organize_winid()
    vim.api.nvim_win_set_cursor(org_winid, { project_line_idx, 0 })
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<CR>", true, false, true), "n", false)

    -- Wait for the UI to update with the folder's contents
    vim.wait(200)

    -- 3. ASSERT: Check if the buffer content now shows the sub-note
    local new_lines = vim.api.nvim_buf_get_lines(org_bufnr, 0, -1, false)
    local note_found = false
    for _, line in ipairs(new_lines) do
      if line:match("note.md") then
        note_found = true
        break
      end
    end
    assert.is_true(note_found, "The note inside the project folder should be displayed")

    -- 4. CLEANUP
    os.remove(capture_path)
    vim.fn.delete(project_dir, "rf")
  end)
end)