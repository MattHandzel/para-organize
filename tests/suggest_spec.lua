-- suggest_spec.lua
-- Tests for suggest.lua module

describe("suggest", function()
  local suggest
  local test_dir = vim.fn.fnamemodify(debug.getinfo(1, 'S').source:sub(2), ":h")
  local test_vault = test_dir .. "/fixtures/vault"
  
  before_each(function()
    -- Set up test configuration with customized weights
    require("para-organize.config").setup({
      paths = {
        vault_dir = test_vault,
        capture_folder = "capture/raw_capture",
      },
      suggestions = {
        weights = {
          exact_tag_match = 2.0,
          normalized_tag_match = 1.5,
          learned_association = 1.8,
          source_match = 1.3,
          alias_similarity = 1.1,
          context_match = 1.0,
        },
        learning = {
          recency_decay = 0.9,
          frequency_boost = 1.2,
          min_confidence = 0.1, -- Lower threshold for tests
        },
        max_suggestions = 10,
        always_show_archive = true,
      }
    })
    
    suggest = require("para-organize.suggest")
    
    -- Create test project folder
    local project_path = test_vault .. "/projects/test-project"
    if vim.fn.isdirectory(project_path) == 0 then
      vim.fn.mkdir(project_path, "p")
    end
    
    -- Create test area folder
    local area_path = test_vault .. "/areas/health"
    if vim.fn.isdirectory(area_path) == 0 then
      vim.fn.mkdir(area_path, "p")
    end
  end)
  
  describe("get_subfolders", function()
    it("finds subfolders in projects", function()
      local projects_path = test_vault .. "/projects"
      local subfolders = suggest.get_subfolders(projects_path)
      
      assert.is_true(#subfolders > 0)
      
      -- Check if test-project is in the list
      local found = false
      for _, folder in ipairs(subfolders) do
        if folder.name == "test-project" then
          found = true
          break
        end
      end
      
      assert.is_true(found, "test-project folder should be found")
    end)
  end)
  
  describe("calculate_score", function()
    it("scores exact tag match", function()
      local capture = {
        tags = {"test-project"},
      }
      
      local destination = {
        path = test_vault .. "/projects/test-project",
        name = "test-project",
        normalized_name = "test-project"
      }
      
      local score = suggest.calculate_score(capture, destination, "projects")
      assert.is_true(score > 0, "Score should be positive for exact tag match")
    end)
    
    it("scores source match", function()
      local capture = {
        sources = {"test-project"},
      }
      
      local destination = {
        path = test_vault .. "/projects/test-project",
        name = "test-project",
        normalized_name = "test-project"
      }
      
      local score = suggest.calculate_score(capture, destination, "projects")
      assert.is_true(score > 0, "Score should be positive for source match")
    end)
    
    it("scores context match", function()
      local capture = {
        context = "related to test-project",
      }
      
      local destination = {
        path = test_vault .. "/projects/test-project",
        name = "test-project",
        normalized_name = "test-project"
      }
      
      local score = suggest.calculate_score(capture, destination, "projects")
      assert.is_true(score > 0, "Score should be positive for context match")
    end)
    
    it("scores normalized tag match", function()
      local capture = {
        tags = {"TEST PROJECT"},
        normalized_tags = {"test-project"}
      }
      
      local destination = {
        path = test_vault .. "/projects/test-project",
        name = "test-project",
        normalized_name = "test-project"
      }
      
      local score = suggest.calculate_score(capture, destination, "projects")
      assert.is_true(score > 0, "Score should be positive for normalized tag match")
    end)
    
    it("applies folder type bonus", function()
      local capture = {
        tags = {"health"},
      }
      
      local destination_area = {
        path = test_vault .. "/areas/health",
        name = "health",
        normalized_name = "health"
      }
      
      local destination_project = {
        path = test_vault .. "/projects/health",
        name = "health",
        normalized_name = "health"
      }
      
      local score_area = suggest.calculate_score(capture, destination_area, "areas")
      local score_project = suggest.calculate_score(capture, destination_project, "projects")
      
      -- Projects should get a higher type bonus than areas
      assert.is_true(score_project > score_area)
    end)
  end)
  
  describe("generate_suggestions", function()
    it("generates suggestions for a capture", function()
      local capture = {
        path = test_vault .. "/capture/raw_capture/test.md",
        filename = "test.md",
        title = "Test Capture",
        tags = {"test-project", "health"},
        sources = {"meeting"},
      }
      
      local suggestions = suggest.generate_suggestions(capture)
      
      assert.is_not_nil(suggestions)
      assert.is_true(#suggestions > 0, "Should generate at least one suggestion")
      
      -- Check if suggestions include test-project
      local found_project = false
      local found_area = false
      local found_archive = false
      
      for _, suggestion in ipairs(suggestions) do
        if suggestion.name == "test-project" and suggestion.type == "projects" then
          found_project = true
        elseif suggestion.name == "health" and suggestion.type == "areas" then
          found_area = true
        elseif suggestion.type == "archives" then
          found_archive = true
        end
      end
      
      assert.is_true(found_project, "Should suggest test-project")
      assert.is_true(found_area, "Should suggest health area")
      assert.is_true(found_archive, "Should include archive option")
    end)
    
    it("limits suggestions to max_suggestions", function()
      -- Create a capture with many potential matches
      local capture = {
        tags = {"tag1", "tag2", "tag3", "tag4", "tag5", 
                "tag6", "tag7", "tag8", "tag9", "tag10",
                "tag11", "tag12", "tag15"},
      }
      
      -- Create many test folders
      for i = 1, 15 do
        local folder_path = test_vault .. "/projects/tag" .. i
        if vim.fn.isdirectory(folder_path) == 0 then
          vim.fn.mkdir(folder_path, "p")
        end
      end
      
      local suggestions = suggest.generate_suggestions(capture)
      
      -- Should limit to max_suggestions (10) + archive option
      assert.is_true(#suggestions <= 11)
    end)
  end)
  
  describe("analyze_patterns", function()
    it("finds common patterns in captures", function()
      local captures = {
        {
          tags = {"project", "meeting"},
          sources = {"email"},
        },
        {
          tags = {"project", "task"},
          sources = {"email"},
        },
        {
          tags = {"health", "meeting"},
          sources = {"calendar"},
        },
      }
      
      local patterns = suggest.analyze_patterns(captures)
      
      assert.is_not_nil(patterns.common_tags)
      assert.is_not_nil(patterns.common_sources)
      
      -- "project" and "meeting" both appear twice
      assert.equals(2, patterns.common_tags[1].count)
    end)
  end)
  
  after_each(function()
    -- Clean up test folders (keeping the basic structure)
    -- We'll only remove the folders created during tests
    for i = 1, 15 do
      local folder_path = test_vault .. "/projects/tag" .. i
      if vim.fn.isdirectory(folder_path) == 1 then
        vim.fn.delete(folder_path, "rf")
      end
    end
  end)
end)
