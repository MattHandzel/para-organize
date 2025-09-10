-- tests/spec/indexer_spec.lua

local Path = require('plenary.path')

describe('Indexer', function()
  local indexer
  local mock_fs = require('plenary.fs')
  local test_root = Path:new('/tmp/para-organize-test-root')

  before_each(function()
    -- Set up a mock file system
    test_root:mkdir({ parents = true })
    Path:new(test_root, 'Projects'):mkdir()
    Path:new(test_root, 'note1.md'):write('---\ntags: project-a, urgent\n---\n# Note 1', 'w')
    Path:new(test_root, 'Projects/note2.md'):write('---\naliases: [My Second Note]\n---\n# Note 2', 'w')

    -- Mock the config to point to our test directory
    require('para_org.config').options.root_dir = test_root:absolute()

    -- Reload the indexer to use the mocked config
    package.loaded['para_org.indexer'] = nil
    indexer = require('para_org.indexer')
  end)

  after_each(function()
    -- Clean up the mock file system
    test_root:rmdir_r()
  end)

  it('should find and parse notes correctly', function()
    -- We need to run this async function synchronously for the test
    local co = coroutine.create(function() indexer.reindex() end)
    coroutine.resume(co) -- Start the async function
    -- This is a simplified way to wait. For real tests, plenary.test_utils is better.
    require('plenary.async').util.sleep(100) -- Give it a moment to run

    local notes = indexer.load_index()
    assert.are.equal(2, #notes)

    local note1_found = false
    local note2_found = false
    for _, note in ipairs(notes) do
      if note.path:find('note1.md') then
        note1_found = true
        assert.are.equal('project-a, urgent', note.frontmatter.tags)
      elseif note.path:find('note2.md') then
        note2_found = true
        assert.are.equal('[My Second Note]', note.frontmatter.aliases)
      end
    end
    assert.is_true(note1_found, 'note1.md was not found in the index')
    assert.is_true(note2_found, 'note2.md was not found in the index')
  end)
end)
