--- Telescope pickers (spec 03 §4).
---
--- Two halves: pure rendering/parsing assertions, and a set of tests against a
--- REAL `organize serve` over a REAL unix socket, spawned against a throwaway
--- fixture vault. Nothing here touches ~/Obsidian/Main, ~/notes or any live
--- state, and the core is killed on the way out.

local pickers = require("para-organize.pickers")
local support = require("phc_support")

--------------------------------------------------------------------------
-- pure
--------------------------------------------------------------------------

describe("pickers: rendering", function()
  it("shortens a path against the vault root when it knows one", function()
    assert.equals("projects/blog", pickers.short_path("/vault/projects/blog", "/vault"))
    assert.equals("projects/blog", pickers.short_path("/vault/projects/blog/", "/vault/"))
  end)

  it("falls back to the trailing two segments with no root", function()
    assert.equals("projects/blog", pickers.short_path("/somewhere/deep/projects/blog"))
    assert.equals("projects/blog", pickers.short_path("/vault/projects/blog", "/other-vault"))
  end)

  it("maps both the singular record type and the plural suggestion type", function()
    assert.equals("P", pickers.type_letter("project"))
    assert.equals("P", pickers.type_letter("projects"))
    assert.equals("A", pickers.type_letter("area"))
    assert.equals("R", pickers.type_letter("resources"))
    assert.equals("🗑", pickers.type_letter("archives"))
    assert.equals("?", pickers.type_letter("nonsense"))
  end)

  -- Spec 15 §6 closes a defect: `format_folder` read an `opts.show_scores`
  -- that NO call site ever passed, so the picker's score column was
  -- unreachable — a key neither honored nor deleted, which 03 §1 forbids.
  -- `ui.organize.show_scores` is now the SINGLE source, shared with the pane.
  it("renders a folder line, with the score gated on ui.organize.show_scores", function()
    local config = require("para-organize.config")
    local entry = { path = "/vault/projects/blog", name = "blog", type = "projects", score = 2.1 }

    config.reset()
    local scored = pickers.format_folder(entry, { vault_root = "/vault" })
    assert.equals("[P] blog (projects/blog) 2.10", scored)

    config.setup({ ui = { organize = { show_scores = false } } })
    local plain = pickers.format_folder(entry, { vault_root = "/vault" })
    assert.equals("[P] blog (projects/blog)", plain)
    assert.is_falsy(plain:find("2.10", 1, true))
    config.reset()

    -- An explicit opts value still wins, for a caller rendering a list where
    -- scores make no sense.
    assert.is_falsy(pickers.format_folder(entry, { vault_root = "/vault", show_scores = false }):find("2.10", 1, true))
  end)

  it("marks a route-sourced suggestion distinctly (11 §1)", function()
    local line = pickers.format_folder({
      path = "/vault/resources/answers",
      name = "answers",
      type = "resources",
      route = "question",
    }, { vault_root = "/vault" })
    assert.is_truthy(line:find("route:question", 1, true))
  end)

  it("labels a note by alias, then title, then filename (03 §3)", function()
    local base = { path = "/vault/projects/blog/ideas.md", filename = "ideas.md" }
    assert.is_truthy(
      pickers.format_note(vim.tbl_extend("force", base, {
        aliases = { "Blog ideas" },
        title = "Ideas",
      })):find("Blog ideas", 1, true)
    )
    assert.is_truthy(
      pickers.format_note(vim.tbl_extend("force", base, { aliases = {}, title = "Ideas" }))
        :find("Ideas", 1, true)
    )
    assert.is_truthy(
      pickers.format_note(vim.tbl_extend("force", base, { aliases = {} })):find("ideas.md", 1, true)
    )
  end)
end)

describe("pickers: rpc plumbing", function()
  it("forces an empty params table to encode as a JSON object, not an array", function()
    -- `vim.json.encode({})` is `[]`, which the core rejects with
    -- "'criteria' must be an object of filter=value pairs".
    assert.equals("[]", vim.json.encode({}))
    assert.equals("{}", vim.json.encode(pickers.obj({})))
    assert.equals("{}", vim.json.encode(pickers.obj(nil)))
    assert.equals('{"a":1}', vim.json.encode(pickers.obj({ a = 1 })))
  end)

  it("recognises the core's unknown-method answer in every shape", function()
    assert.is_true(pickers.is_method_not_found({ code = -32601, message = "unknown method 'x'" }))
    assert.is_true(pickers.is_method_not_found({ code = -1, data = { kind = "MethodNotFound" } }))
    assert.is_true(pickers.is_method_not_found("unknown method 'folder.list'"))
    assert.is_false(pickers.is_method_not_found({ code = -32000, message = "note not found" }))
    assert.is_false(pickers.is_method_not_found(nil))
  end)

  it("renders an rpc error with its hint", function()
    assert.equals("boom", pickers.error_message("boom"))
    assert.equals(
      "note not found (check the path)",
      pickers.error_message({ message = "note not found", data = { hint = "check the path" } })
    )
    assert.equals("unknown error", pickers.error_message(nil))
  end)

  it("reports a missing client instead of indexing nil", function()
    local err
    pickers.rpc(nil, "search.query", {}, function(_, e)
      err = e
    end)
    assert.is_truthy(err)
    assert.is_truthy(tostring(err):find("no core client", 1, true))
  end)
end)

describe("pickers: derived data", function()
  it("keeps only PARA folders, deduped and sorted, with plural types", function()
    local records = {
      { path = "/v/projects/blog/a.md", para_type = "project" },
      { path = "/v/projects/blog/b.md", para_type = "project" },
      { path = "/v/areas/health/c.md", para_type = "area" },
      { path = "/v/capture/raw_capture/d.md", para_type = "capture" },
      { path = "/v/dailies/e.md", para_type = "other" },
      { path = "/v/archive/capture/raw_capture/f.md", para_type = "archive" },
    }
    local folders = pickers.folders_from_records(records)
    assert.equals(2, #folders)
    assert.equals("/v/areas/health", folders[1].path)
    assert.equals("areas", folders[1].type)
    assert.equals("health", folders[1].name)
    assert.equals("/v/projects/blog", folders[2].path)
    assert.equals("projects", folders[2].type)

    local with_archive = pickers.folders_from_records(records, { include_archive = true })
    assert.equals(3, #with_archive)
  end)

  it("offers the nine saved searches of spec 03 §4, with core-valid criteria", function()
    local searches = pickers.saved_searches()
    local names = vim.tbl_map(function(s)
      return s.name
    end, searches)
    assert.same({
      "Unprocessed Captures",
      "Today's Notes",
      "This Week",
      "With Audio",
      "Meeting Notes",
      "No Tags",
      "Projects",
      "Areas",
      "Resources",
    }, names)

    -- index.py `_VALID_FILTER_KEYS`; anything else is a hard error at the core.
    local valid = {
      tags = true, sources = true, modalities = true, status = true,
      para_type = true, since = true, ["until"] = true, until_date = true, text = true,
    }
    for _, search in ipairs(searches) do
      for key in pairs(search.criteria) do
        assert.is_truthy(valid[key], ("%s uses invalid filter %q"):format(search.name, key))
      end
    end
  end)

  it('expresses "No Tags" as a post-filter, since no core filter can', function()
    local no_tags
    for _, search in ipairs(pickers.saved_searches()) do
      if search.name == "No Tags" then
        no_tags = search
      end
    end
    assert.is_function(no_tags.post)
    assert.is_true(no_tags.post({ tags = {} }))
    assert.is_true(no_tags.post({}))
    assert.is_false(no_tags.post({ tags = { "health" } }))
  end)
end)

describe("pickers: backend selection", function()
  after_each(function()
    pickers.telescope_enabled = nil
  end)

  it("uses telescope when it is installed", function()
    assert.is_true(pickers.has_telescope())
    assert.equals("telescope", pickers.backend({}))
  end)

  it("degrades to vim.ui.select when telescope is unavailable or refused", function()
    assert.equals("ui.select", pickers.backend({ telescope = false }))
    pickers.telescope_enabled = false
    assert.equals("ui.select", pickers.backend({}))
  end)

  it("routes a selection through vim.ui.select, formatted", function()
    local seen_items, seen_opts
    local original = vim.ui.select
    vim.ui.select = function(items, opts, on_choice)
      seen_items, seen_opts = items, opts
      on_choice(items[2])
    end

    local chosen
    local backend = pickers.select(
      { { name = "one" }, { name = "two" } },
      {
        telescope = false,
        prompt = "Pick",
        format_item = function(item)
          return "» " .. item.name
        end,
      },
      function(choice)
        chosen = choice
      end
    )
    vim.ui.select = original

    assert.equals("ui.select", backend)
    assert.equals(2, #seen_items)
    assert.equals("Pick", seen_opts.prompt)
    assert.equals("» one", seen_opts.format_item(seen_items[1]))
    assert.equals("two", chosen.name)
  end)

  it("says so, once, when there is nothing to pick", function()
    local called_with_nil = false
    local backend
    local seen = support.capture_notifications(function()
      backend = pickers.select({}, {
        telescope = false,
        empty_message = "no destination folders found",
      }, function(choice)
        called_with_nil = choice == nil
      end)
    end)
    assert.equals("empty", backend)
    assert.is_true(called_with_nil)
    assert.equals(1, #seen)
    assert.is_true(support.notified(seen, "no destination folders found"))
  end)
end)

--------------------------------------------------------------------------
-- against a real core
--------------------------------------------------------------------------

describe("pickers: against a real organize core", function()
  local core = support.start_core()
  local client = assert(support.connect(core.socket))
  local vault = core.fixture.vault

  vim.api.nvim_create_autocmd("VimLeavePre", {
    callback = function()
      pcall(client.close, client)
      pcall(core.stop)
    end,
  })

  --- A SYNC-only view of the real client, over the real socket.
  ---
  --- `pickers.rpc` prefers `client:request` (async) when the client offers it,
  --- which defers the callback past the end of a busted `it` body — the test
  --- then asserts on nils and, worse, a stubbed `vim.ui.select` gets restored
  --- before the picker reaches it, so the REAL one runs and blocks headless
  --- nvim on `inputlist`. Exposing only `request_sync` makes every assertion
  --- below deterministic; the async path has its own test.
  local function sync(counts)
    return {
      _counts = counts,
      request_sync = function(_, method, params, timeout)
        if counts then
          counts[method] = (counts[method] or 0) + 1
        end
        return client:request_sync(method, params, timeout)
      end,
      close = function() end,
    }
  end

  local function counting()
    return sync({})
  end

  local function a_capture_path()
    local records = client:request_sync("search.query", { criteria = { para_type = { "capture" } } })
    assert.is_truthy(records and #records > 0, "fixture vault has no captures")
    return records[1].path
  end

  --- Any unstubbed `vim.ui.select` in a headless run would block forever on
  --- `inputlist`. Fail loudly instead.
  local real_ui_select = vim.ui.select
  vim.ui.select = function()
    error("vim.ui.select reached the real implementation — a test left it unstubbed")
  end
  vim.api.nvim_create_autocmd("VimLeavePre", {
    callback = function()
      vim.ui.select = real_ui_select
    end,
  })

  before_each(function()
    pickers.invalidate_cache()
    pickers.telescope_enabled = nil
    pickers.client_provider = nil
  end)

  it("handshakes at apiVersion 1", function()
    assert.equals(1, client.api_version)
  end)

  it("lists destinations from folder.list, PARA folders only", function()
    local counted = counting()
    local items, err
    pickers.fetch_destinations(counted, { vault_root = vault, no_cache = true }, function(i, e)
      items, err = i, e
    end)
    assert.is_nil(err)
    assert.is_truthy(items and #items > 0)
    -- The core serves folder.list, so the derived fallback must not run.
    assert.equals(1, counted._counts["folder.list"])
    assert.is_nil(counted._counts["search.query"])

    local paths = vim.tbl_map(function(item)
      return pickers.short_path(item.path, vault)
    end, items)
    assert.is_truthy(vim.tbl_contains(paths, "projects/blog"))
    assert.is_truthy(vim.tbl_contains(paths, "areas/health"))
    assert.is_truthy(vim.tbl_contains(paths, "resources/answers"))
    -- projects/kms holds no notes at all: only a disk-enumerated listing sees
    -- it, and a folder you cannot pick is a folder you cannot file into.
    assert.is_truthy(vim.tbl_contains(paths, "projects/kms"), vim.inspect(paths))
    -- Archiving is its own action (03 §6), and captures/dailies are not
    -- destinations.
    for _, path in ipairs(paths) do
      assert.is_falsy(path:find("archive", 1, true), "archive offered as a destination: " .. path)
      assert.is_falsy(path:find("capture", 1, true), "capture offered as a destination: " .. path)
      assert.is_falsy(path:find("dailies", 1, true), "dailies offered as a destination: " .. path)
    end
  end)

  it("includes archive folders only when asked", function()
    local items
    pickers.fetch_destinations(
      sync(),
      { vault_root = vault, no_cache = true, include_archive = true },
      function(i)
        items = i
      end
    )
    local types = vim.tbl_map(function(item)
      return pickers.type_letter(item.type)
    end, items)
    assert.is_truthy(vim.tbl_contains(types, pickers.TYPE_LETTER.archives), vim.inspect(types))
  end)

  it("scopes folder.list by para_type", function()
    local items
    pickers.fetch_destinations(sync(), { vault_root = vault, no_cache = true, para_type = "projects" }, function(i)
      items = i
    end)
    assert.is_truthy(#items > 0)
    for _, item in ipairs(items) do
      assert.equals("P", pickers.type_letter(item.type))
    end
  end)

  it("falls back to suggestions + index folders on a core with no folder.list", function()
    -- An older core answers -32601. The picker must still produce a list
    -- rather than an error, with suggestion-sourced entries keeping their
    -- scores. This path is documented BEST-EFFORT for empty folders
    -- (`fetch_destinations`' own contract): `suggest.for_note` returns real
    -- matches only — it does not pad with unmatched folders — so a folder
    -- with no notes AND no match (projects/kms) is invisible without
    -- `folder.list`. Only the modern-core path (previous test) sees it.
    local counts = {}
    local legacy = {
      request_sync = function(_, method, params, timeout)
        counts[method] = (counts[method] or 0) + 1
        if method == "folder.list" then
          return nil, {
            code = -32601,
            message = "unknown method 'folder.list'",
            data = { kind = "MethodNotFound" },
          }
        end
        return client:request_sync(method, params, timeout)
      end,
    }

    local without
    pickers.fetch_destinations(legacy, { vault_root = vault, no_cache = true }, function(items)
      without = vim.tbl_map(function(i)
        return pickers.short_path(i.path, vault)
      end, items)
    end)
    assert.equals(1, counts["folder.list"])
    assert.equals(1, counts["search.query"])
    assert.is_truthy(vim.tbl_contains(without, "projects/blog"))
    -- No note lives in projects/kms, so search.query alone cannot see it.
    assert.is_falsy(vim.tbl_contains(without, "projects/kms"))

    local with
    pickers.fetch_destinations(
      legacy,
      { vault_root = vault, no_cache = true, note = a_capture_path() },
      function(items)
        with = items
      end
    )
    local paths = vim.tbl_map(function(i)
      return pickers.short_path(i.path, vault)
    end, with)
    assert.equals(1, counts["suggest.for_note"])
    -- The suggestion union keeps every index-derived folder…
    assert.is_truthy(vim.tbl_contains(paths, "projects/blog"), vim.inspect(paths))
    assert.is_truthy(vim.tbl_contains(paths, "areas/health"), vim.inspect(paths))
    -- …but an empty, unmatched folder stays invisible on a legacy core
    -- (the documented best-effort caveat).
    assert.is_falsy(vim.tbl_contains(paths, "projects/kms"))

    local scored = 0
    for _, item in ipairs(with) do
      if type(item.score) == "number" then
        scored = scored + 1
      end
    end
    assert.is_true(scored > 0, "suggestion-sourced entries lost their scores")
  end)

  it("caches the destination list and invalidates on demand", function()
    local counted = counting()
    local opts = { vault_root = vault }
    pickers.fetch_destinations(counted, opts, function() end)
    pickers.fetch_destinations(counted, opts, function() end)
    assert.equals(1, counted._counts["folder.list"])

    pickers.invalidate_cache()
    pickers.fetch_destinations(counted, opts, function() end)
    assert.equals(2, counted._counts["folder.list"])
  end)

  it("scopes notes to one folder", function()
    local notes
    pickers.fetch_folder_notes(sync(), vault .. "/projects/blog", function(n)
      notes = n
    end)
    assert.is_truthy(notes and #notes > 0)
    for _, record in ipairs(notes) do
      assert.equals(vault .. "/projects/blog", vim.fs.dirname(record.path))
    end

    local none
    pickers.fetch_folder_notes(sync(), vault .. "/projects/kms", function(n)
      none = n
    end)
    assert.same({}, none)
  end)

  it("browses one directory level through folder.children (03 §3)", function()
    local level
    pickers.fetch_folder_children(sync(), vault .. "/projects", function(children)
      level = children
    end)
    local dirs = vim.tbl_map(function(d)
      return d.name
    end, level.dirs)
    assert.is_truthy(vim.tbl_contains(dirs, "blog"))
    assert.is_truthy(vim.tbl_contains(dirs, "kms"))
    assert.same({}, level.notes)

    local leaf
    pickers.fetch_folder_children(sync(), vault .. "/projects/blog", function(children)
      leaf = children
    end)
    assert.same({}, leaf.dirs)
    assert.is_true(#leaf.notes > 0)
    -- A browse note has no `filename`; the renderer must not concatenate nil.
    local line = pickers.format_note(leaf.notes[1], { vault_root = vault })
    assert.is_truthy(line:find("[F]", 1, true))
  end)

  it("reports a folder the core does not know instead of an empty list", function()
    local children, err
    pickers.fetch_folder_children(sync(), vault .. "/does-not-exist", function(c, e)
      children, err = c, e
    end)
    assert.is_nil(children)
    assert.is_truthy(err:find("folder not found", 1, true))
  end)

  it("works with the REAL para-organize.rpc client, over its async path", function()
    -- The two rpc entry points use OPPOSITE argument orders:
    -- `Client:request` calls back `cb(err, result)` while `request_sync`
    -- returns `result, err`. Only the real client proves pickers.rpc gets
    -- that right; a stand-in that picked one order would hide the bug.
    local rpc = require("para-organize.rpc")
    local real, cerr = rpc.connect(core.socket, { timeout_ms = 3000 })
    assert.is_truthy(real, vim.inspect(cerr))
    assert.is_function(real.request)
    assert.equals(1, real.api_version)

    local items, err
    pickers.fetch_destinations(real, { vault_root = vault, no_cache = true }, function(i, e)
      items, err = i, e
    end)
    assert.is_nil(items, "async client answered synchronously — the test proves nothing")
    assert.is_true(
      vim.wait(5000, function()
        return items ~= nil or err ~= nil
      end, 10),
      "the async callback never fired"
    )
    assert.is_nil(err, vim.inspect(err))
    assert.is_true(#items > 0)
    local paths = vim.tbl_map(function(i)
      return pickers.short_path(i.path, vault)
    end, items)
    assert.is_truthy(vim.tbl_contains(paths, "projects/blog"), vim.inspect(paths))
    real:close()
  end)

  it("surfaces a real rpc error object as one readable line", function()
    local rpc = require("para-organize.rpc")
    local real = assert(rpc.connect(core.socket, { timeout_ms = 3000 }))
    local records, err
    pickers.fetch_notes(real, { nonsense = { "x" } }, function(r, e)
      records, err = r, e
    end)
    assert.is_true(vim.wait(5000, function()
      return records ~= nil or err ~= nil
    end, 10))
    assert.is_nil(records)
    -- rpc.lua returns an error TABLE ({kind, message, hint, code, data});
    -- pickers must render it, not vim.inspect it into the notification.
    assert.is_string(err)
    assert.is_truthy(err:find("unknown session filter", 1, true), err)
    assert.is_truthy(err:find("valid filters", 1, true), err)
    real:close()
  end)

  it("queries the index with session-filter criteria", function()
    local projects
    pickers.fetch_notes(sync(), { para_type = { "project" } }, function(records)
      projects = records
    end)
    assert.is_truthy(projects and #projects > 0)
    for _, record in ipairs(projects) do
      assert.equals("project", record.para_type)
    end

    local everything
    pickers.fetch_notes(sync(), {}, function(records)
      everything = records
    end)
    assert.is_true(#everything > #projects)
  end)

  it("reports a bad criterion instead of showing an empty list", function()
    local records, err
    pickers.fetch_notes(sync(), { nonsense = { "x" } }, function(r, e)
      records, err = r, e
    end)
    assert.is_nil(records)
    assert.is_truthy(err:find("unknown session filter", 1, true))
    assert.is_truthy(err:find("valid filters", 1, true))
  end)

  it("lists filter values from meta.values", function()
    local tags
    pickers.fetch_filter_values(sync(), "tags", function(values)
      tags = values
    end)
    assert.is_truthy(vim.tbl_contains(tags, "health"))

    local statuses
    pickers.fetch_filter_values(sync(), "status", function(values)
      statuses = values
    end)
    assert.is_truthy(vim.tbl_contains(statuses, "raw"))

    local types
    pickers.fetch_filter_values(sync(), "para_type", function(values)
      types = values
    end)
    assert.is_truthy(vim.tbl_contains(types, "capture"))

    -- `since` has no value list; that is not an error.
    local none
    pickers.fetch_filter_values(sync(), "since", function(values)
      none = values
    end)
    assert.same({}, none)
  end)

  it("drives the destination picker end to end", function()
    pickers.client_provider = function()
      return sync()
    end
    local original = vim.ui.select
    local rendered
    vim.ui.select = function(items, opts, on_choice)
      rendered = vim.tbl_map(opts.format_item, items)
      for _, item in ipairs(items) do
        if item.name == "blog" then
          return on_choice(item)
        end
      end
      on_choice(nil)
    end

    local chosen
    pickers.open_folder_picker(function(entry)
      chosen = entry
    end, { telescope = false, vault_root = vault, no_cache = true })
    vim.ui.select = original

    assert.is_truthy(chosen, "picker never selected anything")
    assert.equals(vault .. "/projects/blog", chosen.path)
    assert.is_truthy(vim.tbl_contains(rendered, "[P] blog (projects/blog)"), vim.inspect(rendered))
  end)

  it("drives the generic filter picker end to end", function()
    pickers.client_provider = function()
      return sync()
    end
    local original = vim.ui.select
    local step = 0
    vim.ui.select = function(items, _, on_choice)
      step = step + 1
      if step == 1 then
        for _, item in ipairs(items) do
          if item == "tags" then
            return on_choice(item)
          end
        end
      end
      for _, item in ipairs(items) do
        if item == "health" then
          return on_choice(item)
        end
      end
      on_choice(nil)
    end

    local key, value
    pickers.open_filter_picker(function(k, v)
      key, value = k, v
    end, { telescope = false })
    vim.ui.select = original

    assert.equals("tags", key)
    assert.equals("health", value)
  end)

  it("notifies clearly when the core is unreachable, and does not crash", function()
    pickers.client_provider = function()
      return nil, "connection refused: /run/user/1000/nope.sock"
    end
    local result
    local seen = support.capture_notifications(function()
      result = pickers.open_folder_picker(function() end, { telescope = false })
    end)
    assert.is_false(result)
    assert.equals(1, #seen)
    assert.is_true(support.notified(seen, "connection refused"))
    assert.is_falsy(seen[1].msg:find("stack traceback", 1, true))
  end)

  it("surfaces a dead socket as one message rather than a lua error", function()
    local dead = support.socket_dir() .. "/porg-definitely-not-here.sock"
    local conn, err = support.connect(dead, { timeout = 300 })
    assert.is_nil(conn)
    assert.is_truthy(err)

    pickers.client_provider = function()
      return nil, "cannot connect to " .. dead .. ": " .. tostring(err)
    end
    local seen = support.capture_notifications(function()
      assert.is_false(pickers.open_search_picker("anything", { telescope = false }))
    end)
    assert.is_true(support.notified(seen, "cannot connect to"))
  end)

  it("stops the core and leaves no socket behind", function()
    client:close()
    core.stop()
    assert.is_nil(vim.uv.fs_stat(core.socket))
  end)
end)
