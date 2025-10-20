-- Global objects
globals = {
  "vim",
  "jit",
}

-- Don't report unused self arguments of methods
self = false

-- Don't report issues with unused arguments
unused_args = false

-- Standard globals for Lua 5.1
std = "lua51"

-- Excludes for third party code
exclude_files = {
  "tests/fixtures/**",
  ".luarocks/**",
}

-- Additional configuration
ignore = {
  "212", -- Unused argument
}

-- Files to check
files = {
  "lua/",
  "plugin/",
  "tests/",
}
