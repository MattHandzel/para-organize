-- utils_spec.lua
-- Tests for utils.lua module

describe("utils", function()
  local utils
  
  before_each(function()
    utils = require("para-organize.utils")
  end)
  
  describe("string utilities", function()
    it("trims whitespace", function()
      assert.equal("hello", utils.trim("  hello  "))
      assert.equal("hello", utils.trim("hello"))
      assert.equal("hello world", utils.trim("  hello world  "))
      assert.equal("", utils.trim("   "))
    end)
    
    it("splits strings", function()
      local result = utils.split("a,b,c", ",")
      assert.same({"a", "b", "c"}, result)
      
      result = utils.split("a, b, c", ",")
      assert.same({"a", "b", "c"}, result)
      
      result = utils.split("a|b|c", "|")
      assert.same({"a", "b", "c"}, result)
      
      result = utils.split("single")
      assert.same({"single"}, result)
    end)
    
    it("normalizes tags", function()
      assert.equal("hello-world", utils.normalize_tag("Hello World"))
      assert.equal("test-tag", utils.normalize_tag("test tag"))
      assert.equal("special-chars", utils.normalize_tag("special_chars!"))
      assert.equal("lowercase", utils.normalize_tag("LOWERCASE"))
    end)
    
    it("calculates string similarity", function()
      -- Exact match should be 1.0
      assert.near(1.0, utils.string_similarity("test", "test"), 0.001)
      
      -- Complete mismatch should be low
      assert.near(0.0, utils.string_similarity("test", "xxxx"), 0.5)
      
      -- Partial match
      assert.near(0.75, utils.string_similarity("test", "text"), 0.25)
      
      -- Case sensitivity check - score will be lower but still high
      local score = utils.string_similarity("Test", "test")
      assert.is_true(score > 0.5) -- More permissive threshold
    end)
  end)
  
  describe("path utilities", function()
    it("normalizes paths", function()
      local home = vim.fn.expand("~")
      assert.equal(home, utils.normalize_path("~"))
      
      local current = vim.fn.fnamemodify(".", ":p"):gsub("/$", "")
      assert.equal(current, utils.normalize_path("."))
      
      -- Should remove trailing slash
      assert.equal("/tmp", utils.normalize_path("/tmp/"))
      
      -- But keep root slash
      assert.equal("/", utils.normalize_path("/"))
    end)
    
    it("checks if path exists", function()
      -- Should exist
      assert.is_true(utils.path_exists("."))
      
      -- Should not exist
      assert.is_false(utils.path_exists("/path/that/definitely/does/not/exist"))
    end)
    
    it("checks if path is directory", function()
      -- Current directory should be a directory
      assert.is_true(utils.is_directory("."))
      
      -- This file is not a directory
      local this_file = debug.getinfo(1, 'S').source:sub(2)
      assert.is_false(utils.is_directory(this_file))
    end)
  end)
  
  describe("YAML frontmatter", function()
    it("extracts frontmatter", function()
      local content = [[---
title: Test
tags:
  - test
  - example
---

# Content

Body text]]

      local fm, body = utils.extract_frontmatter(content)
      assert.is_not_nil(fm)
      assert.is_not_nil(body)
      assert.matches("title: Test", fm)
      assert.matches("# Content", body)
    end)
    
    it("handles missing frontmatter", function()
      local content = [[# No Frontmatter

Just content]]

      local fm, body = utils.extract_frontmatter(content)
      assert.is_nil(fm)
      assert.equals(content, body)
    end)
    
    it("parses simple YAML", function()
      local yaml = [[
title: Test Title
number: 42
boolean: true
empty: 
tags:
  - tag1
  - tag2
  - tag3
]]

      local data = utils.parse_yaml_simple(yaml)
      assert.is_not_nil(data)
      assert.equals("Test Title", data.title)
      assert.equals(42, data.number)
      assert.equals(true, data.boolean)
      assert.same({"tag1", "tag2", "tag3"}, data.tags)
    end)
  end)
  
  describe("date/time utilities", function()
    it("parses ISO datetime", function()
      local dt = utils.parse_iso_datetime("2024-01-15T10:30:00Z")
      assert.is_not_nil(dt)
      assert.equals(2024, dt.year)
      assert.equals(1, dt.month)
      assert.equals(15, dt.day)
      assert.equals(10, dt.hour)
      assert.equals(30, dt.min)
      assert.equals(0, dt.sec)
    end)
    
    it("formats datetime", function()
      local dt = {
        year = 2024,
        month = 1,
        day = 15,
        hour = 10,
        min = 30,
        sec = 0
      }
      
      local formatted = utils.format_datetime(dt, "%Y-%m-%d")
      assert.equals("2024-01-15", formatted)
    end)
    
    it("handles nil dates", function()
      assert.equals("", utils.format_datetime(nil))
    end)
  end)
  
  describe("table utilities", function()
    it("deep copies tables", function()
      local original = {
        a = 1,
        b = {
          c = 2,
          d = { 3, 4, 5 }
        }
      }
      
      local copy = utils.deep_copy(original)
      
      -- Should be a different table
      assert.is_not.equal(original, copy)
      
      -- But with same values
      assert.equals(1, copy.a)
      assert.equals(2, copy.b.c)
      assert.same({3, 4, 5}, copy.b.d)
      
      -- Modifying copy shouldn't affect original
      copy.a = 999
      copy.b.d[1] = 888
      assert.equals(1, original.a)
      assert.equals(3, original.b.d[1])
    end)
    
    it("merges tables", function()
      local t1 = {a = 1, b = 2}
      local t2 = {b = 3, c = 4}
      
      local result = utils.merge_tables(t1, t2)
      assert.equals(1, result.a)
      assert.equals(3, result.b) -- t2 should override t1
      assert.equals(4, result.c)
    end)
    
    it("filters lists", function()
      local list = {1, 2, 3, 4, 5, 6}
      
      local even = utils.filter(list, function(x) return x % 2 == 0 end)
      assert.same({2, 4, 6}, even)
      
      local odd = utils.filter(list, function(x) return x % 2 == 1 end)
      assert.same({1, 3, 5}, odd)
    end)
    
    it("maps over lists", function()
      local list = {1, 2, 3}
      
      local doubled = utils.map(list, function(x) return x * 2 end)
      assert.same({2, 4, 6}, doubled)
      
      local strings = utils.map(list, function(x) return tostring(x) end)
      assert.same({"1", "2", "3"}, strings)
    end)
  end)
end)
