-- tests/syntax_spec.lua
-- Minimal test to ensure all modules load without syntax errors

describe("Module syntax", function()
  local modules = {
    "para-organize",
    "para-organize.config",
    "para-organize.utils",
    "para-organize.indexer",
    "para-organize.search",
    "para-organize.suggest",
    "para-organize.learn",
    "para-organize.move",
    "para-organize.ui",
    "para-organize.ui.layout",
    "para-organize.ui.render",
    "para-organize.ui.keymaps",
    "para-organize.ui.actions",
    -- Add additional submodules here as extracted
  }
  for _, mod in ipairs(modules) do
    it("loads " .. mod, function()
      require(mod)
    end)
  end
end)
