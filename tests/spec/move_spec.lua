-- tests/spec/move_spec.lua

local Path = require('plenary.path')

describe('File Operations', function()
  local move
  local test_root = Path:new('/tmp/para-organize-test-root')

  before_each(function()
    test_root:mkdir({ parents = true })
    Path:new(test_root, 'capture'):mkdir()
    Path:new(test_root, 'Projects'):mkdir()
    Path:new(test_root, 'Archives'):mkdir()
    Path:new(test_root, 'capture', 'my-note.md'):write('# My test note', 'w')

    require('para_org.config').options.root_dir = test_root:absolute()
    require('para_org.config').options.folders.Archives = 'Archives'
    require('para_org.config').options.archive_capture_path = 'archived_captures'

    package.loaded['para_org.move'] = nil
    move = require('para_org.move')
  end)

  after_each(function()
    test_root:rmdir_r()
  end)

  it('should archive a note correctly', function()
    local original_path = test_root:joinpath('capture', 'my-note.md'):absolute()
    assert.is_true(Path:new(original_path):exists())

    move.archive_note(original_path)

    local archived_path = test_root:joinpath('Archives', 'archived_captures', 'my-note.md')
    assert.is_false(Path:new(original_path):exists(), 'Original file should not exist after archive')
    assert.is_true(archived_path:exists(), 'Archived file should exist')
  end)

  it('should move a note to a destination and archive the original', function()
    local original_path = test_root:joinpath('capture', 'my-note.md'):absolute()
    local dest_path = test_root:joinpath('Projects', 'new-project'):absolute()

    move.move_to_dest(original_path, dest_path)

    local new_file_path = Path:new(dest_path, 'my-note.md')
    local archived_path = test_root:joinpath('Archives', 'archived_captures', 'my-note.md')

    assert.is_true(new_file_path:exists(), 'New file should exist at destination')
    assert.is_false(Path:new(original_path):exists(), 'Original file should not exist after move')
    assert.is_true(archived_path:exists(), 'Original should be archived after move')
  end)
end)
