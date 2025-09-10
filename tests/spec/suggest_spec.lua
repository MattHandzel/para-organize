-- tests/spec/suggest_spec.lua

describe('Suggestion Engine', function()
  local suggest
  local test_root = require('plenary.path'):new('/tmp/para-organize-test-root')

  before_each(function()
    test_root:mkdir({ parents = true })
    test_root:joinpath('Projects'):mkdir()
    test_root:joinpath('Projects', 'project-a'):mkdir()
    test_root:joinpath('Resources'):mkdir()

    require('para_org.config').options.root_dir = test_root:absolute()

    package.loaded['para_org.suggest'] = nil
    suggest = require('para_org.suggest')
  end)

  after_each(function()
    test_root:rmdir_r()
  end)

  it('should suggest folders based on tags', function()
    local note = {
      path = '/tmp/note.md',
      frontmatter = { tags = 'project-a, other' },
    }

    local suggestions = suggest.get_suggestions(note)
    
    assert.is_true(#suggestions > 1, 'Should have at least one suggestion plus archive')
    local top_suggestion = suggestions[1]
    assert.are.equal('P', top_suggestion.type)
    assert.is_true(top_suggestion.path:find('project-a') ~= nil)
  end)
end)
