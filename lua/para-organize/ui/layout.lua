-- para-organize/ui/layout.lua
-- Layout and component creation for PARA organize UI

local Layout = require("nui.layout")
local Popup = require("nui.popup")

local M = {}

local function create_capture_popup(ui_config)
  return Popup({
    enter = true,
    focusable = true,
    border = {
      style = ui_config.float_opts.border,
      text = {
        top = " Capture ",
        top_align = "center",
      },
    },
    win_options = {
      winblend = 0,
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
      cursorline = true,
    },
    buf_options = {
      modifiable = true,
      swapfile = false,
      filetype = "markdown",
    },
  })
end

local function create_organize_popup(ui_config)
  return Popup({
    enter = false,
    focusable = true,
    border = {
      style = ui_config.float_opts.border,
      text = {
        top = " Organize ",
        top_align = "center",
      },
    },
    win_options = {
      winblend = 0,
      winhighlight = "Normal:Normal,FloatBorder:FloatBorder",
      cursorline = true,
    },
    buf_options = {
      modifiable = true,
      swapfile = false,
      filetype = "markdown",
    },
  })
end

function M.create_layout(ui_config)
  local capture_popup = create_capture_popup(ui_config)
  local organize_popup = create_organize_popup(ui_config)
  local layout

  if ui_config.layout == "float" then
    local width = math.floor(vim.o.columns * ui_config.float_opts.width)
    local height = math.floor(vim.o.lines * ui_config.float_opts.height)
    local position = ui_config.float_opts.position or "50%"
    if position == "center" then
      position = { row = "50%", col = "50%" }
    elseif type(position) == "string" then
      position = { row = position, col = position }
    end
    layout = Layout(
      {
        position = position,
        size = {
          width = width,
          height = height,
        },
      },
      Layout.Box({
        Layout.Box(capture_popup, { size = "50%" }),
        Layout.Box(organize_popup, { size = "50%" }),
      }, { dir = "row" })
    )
  else
    layout = Layout(
      {
        position = "0%",
        size = "100%",
      },
      Layout.Box({
        Layout.Box(capture_popup, { size = "50%" }),
        Layout.Box(organize_popup, { size = "50%" }),
      }, { dir = ui_config.split_opts.direction == "vertical" and "row" or "col" })
    )
  end
  return {
    layout = layout,
    capture_popup = capture_popup,
    organize_popup = organize_popup,
  }
end

return M
