-- learn_spec.lua
-- Tests for learn.lua module

describe("learn", function()
  local learn
  local config
  local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ":h")
  local test_output = test_dir .. "/output"
  
  before_each(function()
    -- Set custom data file for testing
    vim.env.PARA_ORGANIZE_TEST_MODE = "1"
    
    -- Set up test configuration
    config = require("para-organize.config")
    config.setup({
      suggestions = {
        learning = {
          recency_decay = 0.9,
          frequency_boost = 1.2,
          min_confidence = 0.3,
          max_history = 100,
        }
      },
      debug = {
        enabled = true,
        log_level = "debug",
        log_file = test_output .. "/para-organize-test.log",
      }
    })
    
    -- Reset the module to start fresh
    package.loaded["para-organize.learn"] = nil
    learn = require("para-organize.learn")
    
    -- Clear any existing learning data
    learn.clear()
  end)
  
  describe("record_move", function()
    it("records a successful move", function()
      local capture = {
        path = "/test/capture/note.md",
        tags = {"project", "meeting"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      learn.record_move(capture, destination)
      
      -- Check statistics
      local stats = learn.get_statistics()
      assert.equals(1, stats.total_moves)
      assert.is_not_nil(stats.destinations[destination])
      assert.equals(1, stats.destinations[destination])
    end)
    
    it("records patterns", function()
      local capture = {
        path = "/test/capture/note.md",
        tags = {"project", "meeting"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      learn.record_move(capture, destination)
      
      -- Export data to check patterns
      local data = learn.export()
      assert.is_not_nil(data.patterns)
      
      local tag_pattern_key = "tag:project->dest:" .. destination
      assert.is_not_nil(data.patterns[tag_pattern_key])
      assert.equals(1, data.patterns[tag_pattern_key].count)
      
      local source_pattern_key = "source:email->dest:" .. destination
      assert.is_not_nil(data.patterns[source_pattern_key])
      assert.equals(1, data.patterns[source_pattern_key].count)
    end)
    
    it("updates existing associations", function()
      local capture = {
        path = "/test/capture/note.md",
        tags = {"project", "meeting"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      -- Record two moves to the same destination
      learn.record_move(capture, destination)
      learn.record_move(capture, destination)
      
      -- Check statistics
      local stats = learn.get_statistics()
      assert.equals(2, stats.total_moves)
      assert.is_not_nil(stats.destinations[destination])
      assert.equals(2, stats.destinations[destination])
      
      -- Export data to check patterns
      local data = learn.export()
      local tag_pattern_key = "tag:project->dest:" .. destination
      assert.is_not_nil(data.patterns[tag_pattern_key])
      assert.equals(2, data.patterns[tag_pattern_key].count)
    end)
  end)
  
  describe("get_association_score", function()
    it("returns 0 for unknown associations", function()
      local capture = {
        tags = {"unknown"},
        sources = {"unknown"},
      }
      
      local score = learn.get_association_score(capture, "/unknown/dest")
      assert.equals(0, score)
    end)
    
    it("scores known associations", function()
      local capture = {
        tags = {"project", "meeting"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      -- Record a move to build association
      learn.record_move(capture, destination)
      
      -- Now check score
      local score = learn.get_association_score(capture, destination)
      assert.is_true(score > 0)
    end)
    
    it("applies recency decay", function()
      local capture = {
        tags = {"project"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      -- Record a move
      learn.record_move(capture, destination)
      
      -- Get score
      local score1 = learn.get_association_score(capture, destination)
      
      -- Simulate older record by modifying last_used
      local data = learn.export()
      for key, assoc in pairs(data.associations) do
        if assoc.destinations[destination] then
          assoc.destinations[destination].last_used = os.time() - (30 * 24 * 60 * 60) -- 30 days ago
          assoc.last_used = os.time() - (30 * 24 * 60 * 60)
        end
      end
      
      -- Re-import modified data
      learn.import(data)
      
      -- Get new score
      local score2 = learn.get_association_score(capture, destination)
      
      -- Older association should have lower score
      assert.is_true(score2 < score1)
    end)
    
    it("applies frequency boost", function()
      local capture = {
        tags = {"project"},
        sources = {"email"},
      }
      
      local destination = "/test/projects/project-x"
      
      -- Record a single move
      learn.record_move(capture, destination)
      local score1 = learn.get_association_score(capture, destination)
      
      -- Record multiple more moves
      for i = 1, 5 do
        learn.record_move(capture, destination)
      end
      
      local score2 = learn.get_association_score(capture, destination)
      
      -- More frequent use should have higher score
      assert.is_true(score2 > score1)
    end)
  end)
  
  describe("get_pattern_score", function()
    it("scores based on tag patterns", function()
      local capture = {
        tags = {"project-x"},
      }
      
      local destination = "/test/projects/project-x"
      
      -- Record a move to build pattern
      learn.record_move(capture, destination)
      
      -- Check pattern score
      local score = learn.get_pattern_score(capture, destination)
      assert.is_true(score > 0)
    end)
    
    it("scores based on source patterns", function()
      local capture = {
        sources = {"meeting"},
      }
      
      local destination = "/test/projects/meetings"
      
      -- Record a move to build pattern
      learn.record_move(capture, destination)
      
      -- Check pattern score
      local score = learn.get_pattern_score(capture, destination)
      assert.is_true(score > 0)
    end)
  end)
  
  describe("persistence", function()
    it("can save and load data", function()
      local capture = {
        tags = {"persistence-test"},
        sources = {"test"},
      }
      
      local destination = "/test/persistence"
      
      -- Record move
      learn.record_move(capture, destination)
      
      -- Export data
      local data = learn.export()
      
      -- Clear
      learn.clear()
      
      -- Verify data is gone
      local stats = learn.get_statistics()
      assert.equals(0, stats.total_moves)
      
      -- Import data back
      local success = learn.import(data)
      assert.is_true(success)
      
      -- Verify data is restored
      stats = learn.get_statistics()
      assert.equals(1, stats.total_moves)
      assert.equals(1, stats.destinations[destination])
    end)
  end)
  
  describe("get_top_destinations", function()
    it("returns top destinations by count", function()
      -- Record moves to multiple destinations
      local captures = {
        { tags = {"dest1"} },
        { tags = {"dest2"} },
        { tags = {"dest3"} }
      }
      
      local destinations = {
        "/test/dest1",
        "/test/dest2",
        "/test/dest3"
      }
      
      -- Record 3 moves to dest1, 2 to dest2, 1 to dest3
      for i = 1, 3 do
        learn.record_move(captures[1], destinations[1])
      end
      
      for i = 1, 2 do
        learn.record_move(captures[2], destinations[2])
      end
      
      learn.record_move(captures[3], destinations[3])
      
      -- Get top 2 destinations
      local top = learn.get_top_destinations(2)
      assert.equals(2, #top)
      assert.equals(destinations[1], top[1].path)
      assert.equals(destinations[2], top[2].path)
    end)
  end)
  
  describe("apply_decay", function()
    it("removes very old associations", function()
      local capture = {
        tags = {"old-test"},
      }
      
      local destination = "/test/old"
      
      -- Record a move
      learn.record_move(capture, destination)
      
      -- Modify the data to make it very old
      local data = learn.export()
      
      for key, assoc in pairs(data.associations) do
        assoc.last_used = os.time() - (100 * 24 * 60 * 60) -- 100 days ago
      end
      
      -- Re-import modified data
      learn.import(data)
      
      -- Apply decay
      learn.apply_decay()
      
      -- Old associations should be gone
      local stats = learn.get_statistics()
      assert.equals(0, stats.total_moves)
    end)
  end)
end)
