--[[
plugin_spec.lua — executable specification for the Kestrel Lightroom plug-in.

The real plug-in files (Info.lua's loader, KestrelCore, KestrelJson, KestrelApply,
KestrelWorkflow, KestrelBatch, KestrelDialogs, KestrelMenus, PluginInfoProvider
and every MenuItem*.lua) are executed against:

  * a real Kestrel engine on a throw-away data directory — HTTP is genuinely
    exercised through the documented LrHttp contract;
  * mock Lr* namespaces that record what the plug-in asked Lightroom to do.

Injected by tests/lightroom/lua_harness.py:
  TEST_BASE_URL, TEST_PLUGIN_DIR, TEST_SAMPLE, TEST_SAMPLE2, TEST_WORKDIR,
  py_http(method, url, body, headers, timeout) -> status, body, headers
  py_sleep(seconds)
]]

SYNC_DEPTH = 0
local results = { cases = {}, passed = 0, failed = 0 }
local calls = {
    dialogs = {}, added = {}, applies = 0, writeAccess = 0,
    logs = {}, opened = {}, scopes = 0, tasks = 0,
}
local prefsTable = {}
local modalAnswer = 'ok'
local selection = {}
local collectionPhotos = {}
local failApplies = 0

local function record(kind, title, text)
    calls.dialogs[#calls.dialogs + 1] = { kind = kind, title = title or '', text = text or '' }
end

-- ------------------------------------------------------------------ Lr mocks
local modules = {}
local viewFactory

local function isYieldable()
    if coroutine.isyieldable then
        local ok, value = pcall(coroutine.isyieldable)
        if ok then return value end
    end
    return false
end

modules.LrTasks = {
    startAsyncTask = function(fn)
        calls.tasks = calls.tasks + 1
        local co = coroutine.create(fn)
        local ok, err = coroutine.resume(co)
        if not ok then error('background task failed: ' .. tostring(err), 0) end
        return co
    end,
    sleep = function(seconds)
        if SYNC_DEPTH > 0 then
            py_sleep(math.min(tonumber(seconds) or 0.1, 0.5))
        elseif isYieldable() then
            coroutine.yield('sleep')
        end
    end,
}

modules.LrFunctionContext = {
    callWithContext = function(name, fn) return fn({}) end,
    postAsyncTaskWithContext = function(name, fn)
        SYNC_DEPTH = SYNC_DEPTH + 1
        local ok, err = pcall(fn, {})
        SYNC_DEPTH = SYNC_DEPTH - 1
        if not ok then error(err, 0) end
    end,
}

local Scope = {}
Scope.__index = Scope
function Scope:setCaption(text) calls.caption = text end
function Scope:setPortionComplete(done, total) calls.portion = { done, total } end
function Scope:isCanceled() return calls.cancelScope == true end
function Scope:done() calls.scopeDone = true end

modules.LrProgressScope = setmetatable({}, {
    __call = function(_, opts)
        calls.scopes = calls.scopes + 1
        calls.cancelScope = false
        return setmetatable({ title = opts and opts.title }, Scope)
    end,
})

modules.LrDialogs = {
    message = function(title, text, severity)
        record('message', title, text)
        return 'ok'
    end,
    confirm = function(opts)
        record('confirm', opts and opts.title, opts and opts.message)
        return modalAnswer
    end,
    presentModalDialog = function(opts)
        record('modal', opts and opts.title, opts and opts.actionVerb)
        if opts and type(opts.contents) == 'function' then
            opts.contents(viewFactory)
        end
        if modalAnswer == 'ok' then return 'ok' end
        if modalAnswer == 'other' then return 'other' end
        return 'cancel'
    end,
}

local function makePhoto(path, uuid)
    local photo = {
        _path = path,
        _uuid = uuid or ('uuid-' .. tostring(path)),
        _settings = {
            Exposure2012 = 0, Contrast2012 = 0, Sharpness = 40,
            WhiteBalance = 'As Shot', ProcessVersion = '15.4',
        },
    }
    function photo:getRawMetadata(key)
        if key == 'path' then return self._path end
        if key == 'uuid' then return self._uuid end
        return nil
    end
    function photo:getDevelopSettings()
        local copy = {}
        for key, value in pairs(self._settings) do copy[key] = value end
        return copy
    end
    function photo:applyDevelopSettings(settings)
        if failApplies > 0 then
            failApplies = failApplies - 1
            error('Lightroom rejected the settings', 0)
        end
        local replaced = {}
        for key, value in pairs(settings or {}) do replaced[key] = value end
        self._settings = replaced
        calls.applies = calls.applies + 1
        return true
    end
    return photo
end

local catalog = {
    withWriteAccessDo = function(self, name, fn)
        calls.writeAccess = calls.writeAccess + 1
        return fn()
    end,
    getTargetPhotos = function(self) return selection end,
    getActiveCollection = function(self)
        return {
            getName = function() return 'Game 3' end,
            getPhotos = function() return collectionPhotos end,
        }
    end,
    addPhoto = function(self, path, props)
        calls.added[#calls.added + 1] = { path = path, props = props }
        return makePhoto(path, 'added-' .. tostring(#calls.added))
    end,
}

modules.LrApplication = { activeCatalog = function() return catalog end }
modules.LrPrefs = { prefsForPlugin = function() return prefsTable end }

local logger = {}
function logger:enable() end
function logger:info(text) calls.logs[#calls.logs + 1] = tostring(text) end
logger.warn = logger.info
logger.error = logger.info
logger.trace = logger.info
modules.LrLogger = function() return logger end

modules.LrPathUtils = {
    leafName = function(path) return tostring(path):match('[^/\\]+$') or tostring(path) end,
}
modules.LrFileUtils = {
    exists = function(path)
        local handle = io.open(path, 'rb')
        if handle then handle:close(); return true end
        return false
    end,
}
modules.LrDate = {
    currentTime = function() return os.time() end,
    timeToUserFormat = function(value, fmt) return os.date(fmt or '%Y-%m-%d', value) end,
}
modules.LrShell = { revealInShell = function(path) calls.opened[#calls.opened + 1] = path end }

modules.LrHttp = {
    get = function(url, headers, timeout)
        local ok, status, body = pcall(py_http, 'GET', url, nil, headers, timeout or 30)
        if not ok then
            return nil, { error = { errorCode = 'cannotConnectToHost', name = tostring(status) } }
        end
        return body, { status = status }
    end,
    post = function(url, body, headers, method, timeout)
        local ok, status, response = pcall(py_http, method or 'POST', url, body, headers, timeout or 30)
        if not ok then
            return nil, { error = { errorCode = 'cannotConnectToHost', name = tostring(status) } }
        end
        return response, { status = status }
    end,
    openUrlInBrowser = function(url)
        calls.opened[#calls.opened + 1] = url
        return true
    end,
}

-- A tiny LrView stand-in: every control returns the table it was given, with the
-- control kind recorded so tests can assert the dialogs are really built.
viewFactory = {}
local CONTROL_KINDS = { 'column', 'row', 'static_text', 'edit_field', 'popup_menu',
                        'slider', 'checkbox', 'push_button', 'view', 'separator' }
for _, kind in ipairs(CONTROL_KINDS) do
    viewFactory[kind] = function(self, props)
        props = props or {}
        props._kind = kind
        return props
    end
end
viewFactory.bind = function(self, key) return { _bound = key } end
viewFactory.control_spacing = function() return 5 end

modules.LrView = { osFactory = function() return viewFactory end,
                   bind = function(key) return { _bound = key } end }
modules.LrBinding = { makePropertyTable = function() return {} end }

import = function(name)
    local module = modules[name]
    if not module then error('unknown Lightroom namespace: ' .. tostring(name), 0) end
    return module
end

_PLUGIN = { path = TEST_PLUGIN_DIR, id = 'com.kestrelimaging.lightroom', version = '1.0.0' }

-- The loader lives in the real Info.lua, exactly as Lightroom would load it.
local info = dofile(TEST_PLUGIN_DIR .. '/Info.lua')

local Core = KestrelRequire('KestrelCore')
local Json = KestrelRequire('KestrelJson')
local Apply = KestrelRequire('KestrelApply')
local Workflow = KestrelRequire('KestrelWorkflow')
local Batch = KestrelRequire('KestrelBatch')
local Dialogs = KestrelRequire('KestrelDialogs')
local Menus = KestrelRequire('KestrelMenus')

Core.saveSettings({ base_url = TEST_BASE_URL, default_quality = 'standard' })

local photoA = makePhoto(TEST_SAMPLE, 'uuid-photo-a')
local photoB = makePhoto(TEST_SAMPLE2, 'uuid-photo-b')
selection = { photoA }

-- ------------------------------------------------------------------ assertions
local function test(name, fn)
    local ok, err = pcall(fn)
    results.cases[#results.cases + 1] = { name = name, ok = ok, error = ok and nil or tostring(err) }
    if ok then results.passed = results.passed + 1 else results.failed = results.failed + 1 end
end

local function assertTrue(value, message)
    if not value then error(message or 'expected a true value', 2) end
end

local function assertEqual(actual, expected, message)
    if actual ~= expected then
        error(string.format('%s: expected %s, got %s', message or 'value',
            tostring(expected), tostring(actual)), 2)
    end
end

local function assertMatch(value, pattern, message)
    if not tostring(value):find(pattern) then
        error(string.format('%s: %q does not match %q', message or 'value', tostring(value), pattern), 2)
    end
end

local function deepEqual(a, b)
    if type(a) ~= type(b) then return false end
    if type(a) ~= 'table' then return a == b end
    for key, value in pairs(a) do
        if not deepEqual(value, b[key]) then return false end
    end
    for key in pairs(b) do
        if a[key] == nil then return false end
    end
    return true
end

local function lastDialog()
    return calls.dialogs[#calls.dialogs]
end

-- ------------------------------------------------------------------------ tests
test('information file declares a real plug-in', function()
    assertEqual(info.LrToolkitIdentifier, 'com.kestrelimaging.lightroom', 'toolkit id')
    assertTrue(info.LrPluginName and info.LrPluginName:find('Kestrel') ~= nil, 'plugin name')
    assertEqual(info.LrPluginInfoProvider, 'PluginInfoProvider.lua', 'info panel')
    assertTrue(#info.LrLibraryMenuItems >= 12, 'menu items declared')
    for _, item in ipairs(info.LrLibraryMenuItems) do
        assertTrue(io.open(TEST_PLUGIN_DIR .. '/' .. item.file, 'rb') ~= nil,
            'menu file exists: ' .. tostring(item.file))
    end
end)

test('json round-trips real payloads', function()
    local payload = {
        name = 'Kestrel — café',
        values = { 1, 2, 3.5, -0.25 },
        nested = { ok = true, missing = false, empty = {} },
    }
    local encoded = Json.encode(payload)
    local decoded = Json.decode(encoded)
    assertEqual(decoded.name, payload.name, 'unicode string')
    assertEqual(decoded.values[4], -0.25, 'float preserved')
    assertEqual(decoded.nested.ok, true, 'boolean')
    assertEqual(Json.decode('{"a":null}').a, nil, 'null becomes nil')
    local ok = pcall(Json.decode, '{broken')
    assertTrue(not ok, 'invalid json raises')
end)

test('http get follows the LrHttp contract', function()
    local health = Core.health(5)
    assertTrue(health ~= nil, 'engine answered')
    assertEqual(health.app, 'Kestrel', 'app name')
    assertEqual(tonumber(health.schema), 3, 'engine schema')
end)

test('http post round-trips through the engine', function()
    local created, err = Core.call('/api/collections', {
        method = 'POST', payload = { name = 'Spec probe collection' },
    })
    assertTrue(created ~= nil, 'POST answered: ' .. tostring(err))
    assertTrue(created.collection.id ~= nil, 'collection created')
    local removed = Core.call('/api/collections/' .. created.collection.id, { method = 'DELETE' })
    assertTrue(removed ~= nil, 'probe cleaned up')
end)

test('an unreachable engine produces a usable message', function()
    Core.saveSettings({ base_url = 'http://127.0.0.1:1' })
    local health = Core.health(1)
    assertTrue(health == nil, 'no health')
    local _, message = Core.requireEngine()
    assertMatch(message, 'kestrel serve', 'start command in the message')
    assertMatch(message, '127%.0%.0%.1:1', 'address shown')
    Core.saveSettings({ base_url = TEST_BASE_URL })
end)

test('settings persist in Lightroom preferences and can be reset', function()
    Core.saveSettings({ default_intent = 'natural', max_records = 120 })
    assertEqual(Core.settings().default_intent, 'natural', 'stored intent')
    assertEqual(Core.settings().max_records, 120, 'stored number')
    assertTrue(type(prefsTable.settings) == 'table', 'written to LrPrefs')
    Core.resetSettings()
    assertEqual(Core.settings().default_intent, 'automatic', 'default after reset')
    Core.saveSettings({ base_url = TEST_BASE_URL, default_quality = 'standard' })
end)

test('a photo is registered with the engine once and remembered', function()
    local first = Core.imageIdFor(photoA)
    assertTrue(first ~= nil, 'image id returned')
    assertMatch(first, '^img_', 'engine id shape')
    local second = Core.imageIdFor(photoA)
    assertEqual(second, first, 'cached id reused')
    assertTrue(Core.record(photoA) ~= nil, 'a record was stored')
end)

test('analysis is real and complete', function()
    local imageId = Core.imageIdFor(photoA)
    local analysis = Core.analyse(imageId, 'standard')
    assertTrue(analysis ~= nil, 'analysis returned')
    assertTrue(analysis.analysis.genre ~= nil, 'genre decided')
    assertTrue(tonumber(analysis.analysis.genre_confidence) > 0, 'confidence reported')
    assertTrue(tonumber(analysis.analysis.exposure.median_luma) > 0, 'exposure measured')
    local maskCount = 0
    for _ in pairs(analysis.masks or {}) do maskCount = maskCount + 1 end
    assertTrue(maskCount > 0, 'masks computed')
end)

test('inspect explains the edit without touching the catalogue', function()
    local before = calls.applies
    local result = Workflow.inspect(photoA)
    assertTrue(result.ok, 'inspect succeeded: ' .. tostring(result.error))
    assertEqual(calls.applies, before, 'nothing applied')
    assertTrue(#result.explanation > 0, 'explanation items')
    assertTrue(result.explanation[1].reason ~= nil, 'each item has a reason')
    assertTrue(tonumber(result.metrics.score) > 0, 'quality score')
    assertTrue(result.packet ~= nil, 'delivery packet prepared')
    assertTrue(result.packet.settings.Exposure2012 ~= nil, 'develop settings present')
end)

test('filter drops unknown keys and clamps out-of-range values', function()
    local filtered, report = Apply.filter({
        Exposure2012 = 9.5,
        Contrast2012 = 12,
        WhiteBalance = 'Nonsense',
        EnableLensCorrections = true,
        PresetType = 'Kestrel',
        BogusKey = 3,
    })
    assertEqual(filtered.Exposure2012, 5, 'exposure clamped')
    assertEqual(filtered.Contrast2012, 12, 'contrast kept')
    assertEqual(filtered.WhiteBalance, nil, 'bad enum dropped')
    assertEqual(filtered.EnableLensCorrections, nil, 'unknown key dropped')
    assertEqual(filtered.PresetType, nil, 'preset marker dropped')
    assertEqual(#report.dropped, 4, 'four keys reported')
    assertEqual(#report.adjusted, 1, 'one value adjusted')
end)

test('editing writes develop settings and keeps a snapshot', function()
    local baseline = photoA:getDevelopSettings()
    local result = Workflow.editPhoto(photoA, { quality = 'standard', mode = 'native' })
    assertTrue(result.ok, 'edit succeeded: ' .. tostring(result.error))
    assertTrue(result.globals > 5, 'globals written: ' .. tostring(result.globals))
    assertTrue(calls.applies > 0, 'applyDevelopSettings called')
    local after = photoA:getDevelopSettings()
    assertTrue(after.Exposure2012 ~= baseline.Exposure2012 or after.Contrast2012 ~= 0,
        'settings changed')
    local stored = Core.record(photoA)
    assertTrue(stored.baseline ~= nil, 'baseline captured')
    assertEqual(stored.baseline.Exposure2012, 0, 'baseline is the original value')
    assertEqual(stored.version_id, result.version_id, 'version remembered')
    assertEqual(stored.showing, 'delivered', 'state recorded')
end)

test('revert restores the exact pre-edit settings', function()
    local baseline = Core.record(photoA).baseline
    local ok, err = Apply.revert(photoA)
    assertTrue(ok, 'revert succeeded: ' .. tostring(err))
    local restored = photoA:getDevelopSettings()
    assertTrue(deepEqual(restored, baseline), 'settings byte-identical to baseline')
    assertEqual(Core.record(photoA).showing, 'original', 'state updated')
end)

test('compare toggles between original and result', function()
    local state = Apply.toggle(photoA)
    assertEqual(state, 'delivered', 'restored the result')
    assertTrue(photoA:getDevelopSettings().Exposure2012 ~= 0, 'result applied')
    state = Apply.toggle(photoA)
    assertEqual(state, 'original', 'back to the original')
    assertEqual(photoA:getDevelopSettings().Exposure2012, 0, 'original exposure')
end)

test('revert without a snapshot fails cleanly', function()
    local stranger = makePhoto(TEST_SAMPLE, 'uuid-stranger')
    local ok, err = Apply.revert(stranger)
    assertTrue(not ok, 'revert refused')
    assertMatch(err, 'no saved original', 'explains why')
end)

test('sync copies the look to other photographs', function()
    local applied, failed = Workflow.syncGroup({ photoA, photoB })
    assertEqual(applied, 1, 'one photo synced')
    assertEqual(failed, 0, 'no failures')
    local source = Core.record(photoA).delivered
    local target = photoB:getDevelopSettings()
    assertEqual(target.Exposure2012, source.Exposure2012, 'exposure copied')
    assertEqual(target.Contrast2012, source.Contrast2012, 'contrast copied')
    assertTrue(Core.record(photoB).baseline ~= nil, 'target snapshot kept')
end)

test('a queue job is processed by the engine and applied in Lightroom', function()
    local photos = { photoA, photoB }
    local outcomes = {}
    local result = Batch.run(photos, { quality = 'standard', mode = 'native', intent = 'natural' },
        setmetatable({
            setCaption = function(self, text) calls.caption = text end,
            setPortionComplete = function(self, done, total) calls.portion = { done, total } end,
            isCanceled = function() return false end,
        }, { __index = Scope }))
    assertTrue(result ~= nil, 'batch ran: ' .. tostring(result))
    assertEqual(result.status, 'done', 'job finished')
    assertEqual(result.queued, 2, 'two photographs queued')
    assertEqual(result.applied, 2, 'both applied')
    assertEqual(result.failed, 0, 'no failures')
    assertTrue(calls.portion ~= nil, 'progress reported')
end)

test('render mode adds a 16-bit TIFF and leaves the original alone', function()
    local before = #calls.added
    local baseline = photoB:getDevelopSettings()
    local result = Workflow.editPhoto(photoB, { quality = 'standard', mode = 'render' })
    assertTrue(result.ok, 'render succeeded: ' .. tostring(result.error))
    assertMatch(result.render_path, '%.tif$', 'TIFF path')
    assertEqual(#calls.added, before + 1, 'added to the catalogue')
    assertEqual(calls.added[#calls.added].path, result.render_path, 'correct file added')
    assertTrue(deepEqual(photoB:getDevelopSettings(), baseline), 'original develop settings untouched')
    local handle = io.open(result.render_path, 'rb')
    assertTrue(handle ~= nil, 'file exists on disk')
    local header = handle:read(4)
    handle:close()
    assertTrue(header == 'II*\0' or header == 'MM\0*', 'valid TIFF header')
end)

test('Lightroom rejecting the settings is reported, not fatal', function()
    local result = Workflow.editPhoto(photoA, { quality = 'standard', mode = 'native' })
    assertTrue(result.ok, 'edit succeeded first')
    failApplies = 4
    local outcome = Apply.applyPacket(photoA, result.packet, 'native')
    failApplies = 0
    assertTrue(not outcome.applied, 'apply reported failure')
    assertTrue(outcome.error ~= nil, 'with a reason')
end)

test('one unreadable photo does not stop the batch', function()
    local broken = makePhoto(TEST_WORKDIR .. '/does-not-exist.jpg', 'uuid-broken')
    local good = makePhoto(TEST_SAMPLE, 'uuid-good')
    Core.forgetRecord(good)
    local resultsList, failures = Workflow.editPhotos({ broken, good },
        { quality = 'standard', mode = 'native' })
    assertEqual(#resultsList, 1, 'the good photo was edited')
    assertEqual(#failures, 1, 'the broken one was recorded')
    assertMatch(failures[1].error, 'register', 'failure explains itself')
end)

test('menu command edits the selection end to end', function()
    selection = { photoA }
    collectionPhotos = { photoA }
    modalAnswer = 'ok'
    Apply.revert(photoA)
    local before = photoA:getDevelopSettings()
    local dialogsBefore = #calls.dialogs
    dofile(TEST_PLUGIN_DIR .. '/MenuItemEditSelected.lua')
    assertTrue(#calls.dialogs > dialogsBefore, 'a completion dialog appeared')
    local report = lastDialog()
    assertMatch(report.title, 'finished', 'completion dialog')
    assertTrue(calls.writeAccess > 0, 'write access requested')
    assertTrue(not deepEqual(photoA:getDevelopSettings(), before), 'settings changed')
    assertTrue(calls.scopes >= 1, 'progress scope used')
end)

test('cancelling the options dialog changes nothing', function()
    selection = { photoA }
    modalAnswer = 'cancel'
    local before = photoA:getDevelopSettings()
    dofile(TEST_PLUGIN_DIR .. '/MenuItemEditSelected.lua')
    modalAnswer = 'ok'
    assertTrue(deepEqual(photoA:getDevelopSettings(), before), 'no change after cancel')
end)

test('compare command reports the switch', function()
    selection = { photoA }
    modalAnswer = 'ok'
    Menus.compareSelection()
    local dialog = lastDialog()
    assertMatch(dialog.title, 'compare', 'compare dialog')
    assertMatch(dialog.text, 'original', 'mentions the original')
end)

test('apply-latest re-applies a stored edit', function()
    selection = { photoA }
    Core.updateRecord(photoA, { showing = 'original' })
    Apply.revert(photoA)
    Menus.applyLatest()
    local dialog = lastDialog()
    assertMatch(dialog.text, 'updated from their latest', 'summary text')
    assertTrue(photoA:getDevelopSettings().Exposure2012 ~= 0, 'develop settings restored')
end)

test('revert command restores and reports', function()
    selection = { photoA, photoB }
    modalAnswer = 'ok'
    Menus.revertSelection()
    local dialog = lastDialog()
    assertMatch(dialog.text, 'reverted', 'summary text')
    assertEqual(photoA:getDevelopSettings().Exposure2012, 0, 'photo A original')
end)

test('sync command copies the first look', function()
    selection = { photoA, photoB }
    modalAnswer = 'ok'
    Menus.syncSelection()
    local dialog = lastDialog()
    assertMatch(dialog.text, 'photograph%(s%) updated', 'summary text')
end)

test('queue command runs a real engine job', function()
    selection = { photoA }
    modalAnswer = 'ok'
    Menus.queueSelection()
    local dialog = lastDialog()
    assertMatch(dialog.title, 'background queue', 'queue dialog')
    assertMatch(dialog.text, 'Queue done', 'job completed')
end)

test('status dialog reports the live engine', function()
    Menus.status()
    local dialog = lastDialog()
    assertMatch(dialog.title, 'engine status', 'title')
    assertMatch(dialog.text, 'engine schema 3', 'schema')
    assertMatch(dialog.text, 'Delivery capability', 'capabilities')
end)

test('diagnostics pass against a healthy engine', function()
    local text, failures = Menus.runDiagnostics()
    assertEqual(failures, 0, 'no failing checks: ' .. tostring(text))
    assertMatch(text, 'engine reachable', 'engine check')
    assertMatch(text, 'POST round%-trip', 'post check')
    assertMatch(text, 'preferences readable', 'prefs check')
    assertTrue(not text:find('✗'), 'no cross marks')
end)

test('settings dialog builds its views and keeps the address', function()
    modalAnswer = 'ok'
    local saved = Dialogs.settings()
    assertTrue(saved ~= nil, 'settings saved')
    assertEqual(saved.base_url, TEST_BASE_URL, 'address preserved')
    assertEqual(saved.default_quality, 'standard', 'quality preserved')
end)

test('edit options dialog returns usable options', function()
    modalAnswer = 'ok'
    local options = Dialogs.editOptions('Test', 'subtitle', nil, 3)
    assertTrue(options ~= nil, 'options returned')
    assertEqual(options.quality, 'standard', 'quality default')
    assertEqual(options.intent, 'automatic', 'intent default')
    assertTrue(options.strength > 0, 'strength present')
    modalAnswer = 'cancel'
    assertEqual(Dialogs.editOptions('Test', 'subtitle', nil, 3), nil, 'cancel returns nil')
    modalAnswer = 'ok'
end)

test('preview command shows analysis and explanation', function()
    selection = { photoA }
    modalAnswer = 'cancel'
    Menus.previewSelection()
    modalAnswer = 'ok'
    local confirm = nil
    for index = #calls.dialogs, 1, -1 do
        if calls.dialogs[index].kind == 'confirm' then confirm = calls.dialogs[index]; break end
    end
    assertTrue(confirm ~= nil, 'an explanation dialog appeared')
    assertMatch(confirm.title, 'preview', 'preview title')
    assertMatch(confirm.text, 'What Kestrel changed and why', 'explanation section')
    assertMatch(confirm.text, 'Genre:', 'analysis section')
end)

test('send-to-workspace opens a deep link', function()
    selection = { photoB }
    local before = #calls.opened
    Menus.sendToWorkspace()
    assertTrue(#calls.opened > before, 'browser asked to open')
    assertMatch(calls.opened[#calls.opened], '#/editor/', 'deep link to the photo')
end)

test('collection command uses the active collection', function()
    collectionPhotos = { photoA }
    selection = {}
    modalAnswer = 'cancel'
    local ok, err = pcall(Menus.editCollection)
    assertTrue(ok, 'no error: ' .. tostring(err))
    modalAnswer = 'ok'
end)

test('empty selection is explained, never an exception', function()
    selection = {}
    local before = #calls.dialogs
    Menus.editSelection()
    assertTrue(#calls.dialogs > before, 'a dialog appeared')
    assertMatch(lastDialog().text, 'Select at least one', 'clear instruction')
end)

test('every menu file loads without a Lua error', function()
    selection = { photoA }
    collectionPhotos = { photoA }
    local files = {
        'MenuItemEditSelected.lua', 'MenuItemApplyLatest.lua', 'MenuItemPreview.lua',
        'MenuItemCompare.lua', 'MenuItemRevert.lua', 'MenuItemSync.lua',
        'MenuItemQueue.lua', 'MenuItemEditCollection.lua', 'MenuItemSendToWorkspace.lua',
        'MenuItemStatus.lua', 'MenuItemSettings.lua', 'MenuItemDiagnostics.lua',
        'MenuItemAbout.lua',
    }
    for _, file in ipairs(files) do
        modalAnswer = 'cancel'
        local ok, err = pcall(dofile, TEST_PLUGIN_DIR .. '/' .. file)
        modalAnswer = 'ok'
        assertTrue(ok, file .. ': ' .. tostring(err))
    end
end)

test('plugin info provider builds both panels', function()
    local provider = dofile(TEST_PLUGIN_DIR .. '/PluginInfoProvider.lua')
    local top = provider.sectionsForTopOfDialog(viewFactory, {})
    assertEqual(#top, 1, 'one top section')
    assertEqual(top[1].title, 'Kestrel', 'section title')
    local bottom = provider.sectionsForBottomOfDialog(viewFactory, {})
    assertTrue(#bottom >= 1, 'bottom section built')
end)

return results
