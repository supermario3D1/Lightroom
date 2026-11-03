--[[
    KestrelMenus — the commands behind the Plug-in Extras menu.

    Each command is a thin, honest wrapper: ask for options on the main thread,
    then hand the slow work to a background task with a cancellable progress
    bar. Long operations never freeze Lightroom's interface.
]]

local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrFileUtils = import 'LrFileUtils'
local LrFunctionContext = import 'LrFunctionContext'
local LrPathUtils = import 'LrPathUtils'
local LrProgressScope = import 'LrProgressScope'

local Apply = KestrelRequire('KestrelApply')
local Core = KestrelRequire('KestrelCore')
local Dialogs = KestrelRequire('KestrelDialogs')
local Workflow = KestrelRequire('KestrelWorkflow')

local Menus = {}

-- --------------------------------------------------------------------------- helpers
local function selectedPhotos()
    local ok, photos = pcall(function()
        return LrApplication.activeCatalog():getTargetPhotos()
    end)
    if ok and type(photos) == 'table' then return photos end
    return {}
end

local function requirePhotos(photos)
    if #photos == 0 then
        LrDialogs.message('Kestrel',
            'Select at least one photograph in the Library or Develop module first.', 'info')
        return false
    end
    return true
end

local function photoName(photo)
    local path = Core.photoPath(photo)
    if path then return LrPathUtils.leafName(path) end
    return 'photograph'
end

function Menus.async(name, title, fn)
    LrFunctionContext.postAsyncTaskWithContext(name, function(context)
        local progress = LrProgressScope { title = title, functionContext = context }
        local ok, err = pcall(fn, progress)
        pcall(function() progress:done() end)
        if not ok then
            Core.error(title .. ' failed', tostring(err))
        end
    end)
end

local function runEdit(photos, options)
    Menus.async('kestrel.edit', 'Kestrel — analysing and editing', function(progress)
        local health, engineError = Core.requireEngine()
        if not health then error(engineError, 0) end
        local total = #photos

        local results, failures, seconds = Workflow.editPhotos(photos, options, {
            progress = function(index, count, photo)
                progress:setPortionComplete((index - 1) / count)
                progress:setCaption(string.format('%d of %d — %s', index, count, photoName(photo)))
            end,
            cancelled = function() return progress:isCanceled() end,
        })

        progress:setPortionComplete(1, 1)
        Dialogs.summary(results, failures, seconds)
        if Core.settings().open_workspace_after and results[1] then
            Dialogs.openWorkspace(results[1].image_id)
        end
    end)
end

Menus.runEdit = runEdit

-- --------------------------------------------------------------------------- commands
function Menus.editSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end
    local options = Dialogs.editOptions(
        'Kestrel — edit photographs',
        'Kestrel analyses each frame individually: scene, light, faces, subject, noise. '
            .. 'It then writes develop settings you can keep editing in Lightroom.',
        nil, #photos)
    if not options then return end
    runEdit(photos, options)
end

function Menus.applyLatest()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end

    Menus.async('kestrel.apply-latest', 'Kestrel — applying the latest edit', function(progress)
        local health, engineError = Core.requireEngine()
        if not health then error(engineError, 0) end
        local applied, skipped, failed = 0, 0, 0
        local messages = {}
        for index, photo in ipairs(photos) do
            progress:setPortionComplete((index - 1) / #photos)
            progress:setCaption(string.format('%d of %d — %s', index, #photos, photoName(photo)))
            if progress:isCanceled() then break end

            local record = Core.record(photo)
            if not record or not record.version_id then
                skipped = skipped + 1
            else
                local packet, err = Core.delivery(record.image_id, record.version_id,
                    Core.settings().apply_mode)
                if not packet then
                    failed = failed + 1
                    messages[#messages + 1] = photoName(photo) .. ': ' .. tostring(err)
                else
                    local outcome = Apply.applyPacket(photo, packet, Core.settings().apply_mode)
                    if outcome.applied then
                        applied = applied + 1
                    else
                        failed = failed + 1
                        messages[#messages + 1] = photoName(photo) .. ': ' .. tostring(outcome.error)
                    end
                end
            end
        end
        progress:setPortionComplete(1, 1)

        local lines = {
            string.format('%d photograph(s) updated from their latest Kestrel edit.', applied),
        }
        if skipped > 0 then
            lines[#lines + 1] = string.format('%d photograph(s) had no stored edit — use “Edit selected photos”.', skipped)
        end
        if failed > 0 then
            lines[#lines + 1] = string.format('%d failed:', failed)
            for index = 1, math.min(#messages, 6) do
                lines[#lines + 1] = '  • ' .. messages[index]
            end
        end
        LrDialogs.message('Kestrel — latest edit applied', table.concat(lines, '\n'), 'info')
    end)
end

function Menus.previewSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end
    local photo = photos[1]

    Menus.async('kestrel.preview', 'Kestrel — previewing', function(progress)
        local health, engineError = Core.requireEngine()
        if not health then error(engineError, 0) end
        progress:setCaption('Analysing and rendering ' .. photoName(photo) .. '…')
        local options = Workflow.defaultOptions()
        local result = Workflow.inspect(photo, options)
        progress:setPortionComplete(1, 1)
        if not result.ok then
            Core.error('Kestrel could not preview this photograph', tostring(result.error))
            return
        end
        local verdict = Dialogs.explain(result)
        if verdict == 'apply-render' then
            local renderOptions = Workflow.defaultOptions({ mode = 'render' })
            local rendered = Workflow.editPhoto(photo, renderOptions)
            if rendered.ok then
                LrDialogs.message('Kestrel — rendered copy added',
                    'A 16-bit TIFF was rendered and imported next to the original. '
                        .. 'The RAW file is untouched.', 'info')
            else
                Core.error('Kestrel could not render this photograph', tostring(rendered.error))
            end
        elseif verdict == 'applied' and #photos > 1 then
            local answer = LrDialogs.confirm {
                title = 'Apply to the whole selection?',
                message = string.format('Apply the same decisions to the other %d photograph(s)? '
                    .. 'Each one is still analysed and graded on its own measurements.',
                    #photos - 1),
                actionVerb = 'Apply to all',
                cancelVerb = 'Only this one',
            }
            if answer == 'ok' then
                runEdit(photos, options)
            end
        end
    end)
end

function Menus.compareSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end

    local withRecord, showingDelivered = 0, 0
    for _, photo in ipairs(photos) do
        local record = Core.record(photo)
        if record then
            withRecord = withRecord + 1
            if record.showing ~= 'original' then
                showingDelivered = showingDelivered + 1
            end
        end
    end
    if withRecord == 0 then
        LrDialogs.message('Kestrel — compare',
            'None of the selected photographs has been edited by Kestrel yet.', 'info')
        return
    end

    local toOriginal = showingDelivered >= withRecord
    local answer = LrDialogs.confirm {
        title = toOriginal and 'Show the original?' or 'Show the Kestrel result?',
        message = toOriginal
            and 'The stored pre-edit develop settings will be restored. Your Kestrel result stays saved and can be shown again at any time.'
            or 'The stored Kestrel develop settings will be re-applied.',
        actionVerb = toOriginal and 'Show original' or 'Show result',
        cancelVerb = 'Cancel',
    }
    if answer ~= 'ok' then return end

    Menus.async('kestrel.compare', 'Kestrel — switching view', function(progress)
        local changed, skipped = 0, 0
        for index, photo in ipairs(photos) do
            progress:setPortionComplete((index - 1) / #photos)
            progress:setCaption(string.format('%d of %d — %s', index, #photos, photoName(photo)))
            local record = Core.record(photo)
            if not record then
                skipped = skipped + 1
            else
                local state, err = Apply.toggle(photo)
                if state then changed = changed + 1
                else Core.logError('compare failed', err) end
            end
        end
        progress:setPortionComplete(1, 1)
        LrDialogs.message('Kestrel — compare',
            string.format('Now showing the %s for %d photograph(s).%s',
                toOriginal and 'original' or 'Kestrel result', changed,
                skipped > 0 and string.format(' %d had no stored edit.', skipped) or ''),
            'info')
    end)
end

function Menus.revertSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end
    local answer = LrDialogs.confirm {
        title = 'Revert to the original develop settings?',
        message = 'Kestrel will restore the develop settings it found before its first edit. '
            .. 'The original files were never modified. Lightroom history also keeps every step.',
        actionVerb = 'Revert',
        cancelVerb = 'Keep the edit',
    }
    if answer ~= 'ok' then return end

    Menus.async('kestrel.revert', 'Kestrel — reverting', function(progress)
        local reverted, missing, failed = 0, 0, 0
        for index, photo in ipairs(photos) do
            progress:setPortionComplete((index - 1) / #photos)
            progress:setCaption(string.format('%d of %d — %s', index, #photos, photoName(photo)))
            local ok, err = Apply.revert(photo)
            if ok then
                reverted = reverted + 1
            elseif tostring(err):find('no saved original') then
                missing = missing + 1
            else
                failed = failed + 1
                Core.logError('revert failed', err)
            end
        end
        progress:setPortionComplete(1, 1)
        local lines = { string.format('%d photograph(s) reverted.', reverted) }
        if missing > 0 then
            lines[#lines + 1] = string.format('%d had no Kestrel snapshot to restore.', missing)
        end
        if failed > 0 then
            lines[#lines + 1] = string.format('%d failed — see the Kestrel log.', failed)
        end
        LrDialogs.message('Kestrel — reverted', table.concat(lines, '\n'), 'info')
    end)
end

function Menus.syncSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end
    if #photos < 2 then
        LrDialogs.message('Kestrel — sync look',
            'Select at least two photographs. The first one supplies the look.', 'info')
        return
    end
    local answer = LrDialogs.confirm {
        title = 'Sync the first photo’s look?',
        message = string.format(
            'The global develop settings of %s will be applied to the other %d photograph(s).\n\n'
                .. 'Local masks cannot be copied through the Lightroom SDK: run “Edit selected photos” '
                .. 'if you want masks re-derived for each frame.', photoName(photos[1]), #photos - 1),
        actionVerb = 'Sync',
        cancelVerb = 'Cancel',
    }
    if answer ~= 'ok' then return end

    Menus.async('kestrel.sync', 'Kestrel — syncing look', function(progress)
        progress:setCaption('Applying develop settings…')
        local applied, failed, firstError = Workflow.syncGroup(photos)
        progress:setPortionComplete(1, 1)
        local lines = { string.format('%d photograph(s) updated.', applied) }
        if failed > 0 then
            lines[#lines + 1] = string.format('%d failed: %s', failed, tostring(firstError))
        end
        lines[#lines + 1] = ''
        lines[#lines + 1] = 'Every photo keeps its own snapshot, so Revert still works per frame.'
        LrDialogs.message('Kestrel — look synced', table.concat(lines, '\n'), 'info')
    end)
end

function Menus.queueSelection()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end
    local options = Dialogs.editOptions(
        'Kestrel — background queue',
        'The engine processes the queue in the background: you can keep working, '
            .. 'pause it, or close Lightroom. Finished frames are applied as they arrive.',
        nil, #photos)
    if not options then return end

    Menus.async('kestrel.queue', 'Kestrel — queueing photographs', function(progress)
        local health, engineError = Core.requireEngine()
        if not health then error(engineError, 0) end
        local Batch = KestrelRequire('KestrelBatch')
        Batch.enqueueSelection(photos, options, progress)
    end)
end

function Menus.editCollection()
    local catalog = LrApplication.activeCatalog()
    local collection = catalog:getActiveCollection()
    if not collection then
        LrDialogs.message('Kestrel — edit collection',
            'Open a collection, folder or the filmstrip selection first.', 'info')
        return
    end
    local ok, photos = pcall(function() return collection:getPhotos(true) end)
    if not ok or not photos or #photos == 0 then
        LrDialogs.message('Kestrel — edit collection',
            'This collection has no photographs.', 'info')
        return
    end
    local options = Dialogs.editOptions(
        'Kestrel — edit collection',
        string.format('%s — %d photograph(s). They are processed one at a time, with '
            .. 'consistency grouped by lighting condition.', collection:getName(), #photos),
        nil, #photos)
    if not options then return end
    runEdit(photos, options)
end

function Menus.sendToWorkspace()
    local photos = selectedPhotos()
    if not requirePhotos(photos) then return end

    Menus.async('kestrel.send', 'Kestrel — sending to the workspace', function(progress)
        local health, engineError = Core.requireEngine()
        if not health then error(engineError, 0) end
        local collection = Core.ensureCollection(Core.settings().collection_name)
        local imported, skipped, failed = 0, 0, 0
        local firstImageId = nil
        for index, photo in ipairs(photos) do
            progress:setPortionComplete((index - 1) / #photos)
            progress:setCaption(string.format('%d of %d — %s', index, #photos, photoName(photo)))
            local path = Core.photoPath(photo)
            local record = Core.record(photo)
            if record and record.image_id then
                skipped = skipped + 1
                firstImageId = firstImageId or record.image_id
            elseif not path then
                failed = failed + 1
            else
                local imageId, err = Core.imageIdFor(photo, collection and collection.name)
                if imageId then
                    imported = imported + 1
                    firstImageId = firstImageId or imageId
                else
                    failed = failed + 1
                    Core.logError('send failed', err)
                end
            end
        end
        progress:setPortionComplete(1, 1)
        Dialogs.openWorkspace(firstImageId)
        LrDialogs.message('Kestrel — sent to workspace',
            string.format('%d new, %d already registered, %d failed.\n\n'
                .. 'The workspace opens in your browser with the full library, '
                .. 'before/after comparison and explanations.', imported, skipped, failed), 'info')
    end)
end

-- --------------------------------------------------------------------------- status
function Menus.status()
    Dialogs.status()
end

function Menus.settings()
    local saved = Dialogs.settings()
    if saved then
        Core.log('settings saved', saved.base_url)
    end
end

function Menus.about()
    Dialogs.about()
end

-- --------------------------------------------------------------------------- diagnostics
local function check(name, fn)
    local ok, detail = pcall(fn)
    if ok then
        return true, tostring(detail or 'ok')
    end
    return false, tostring(detail)
end

--- Self-test used by the Diagnostics menu and by the automated plug-in spec.
function Menus.runDiagnostics()
    local lines = {}
    local failures = 0
    local function add(passed, name, detail)
        if not passed then failures = failures + 1 end
        lines[#lines + 1] = string.format('%s %s — %s', passed and '✓' or '✗', name, detail)
    end

    for _, file in ipairs({ 'Info.lua', 'KestrelCore.lua', 'KestrelApply.lua',
                            'KestrelWorkflow.lua', 'KestrelDialogs.lua', 'KestrelJson.lua' }) do
        local path = _PLUGIN.path .. '/' .. file
        local ok, exists = pcall(function() return LrFileUtils.exists(path) end)
        add(ok and exists == true, 'plug-in file ' .. file, path)
    end

    local health, healthError = Core.health(4)
    add(health ~= nil, 'engine reachable',
        health and string.format('%s %s at %s', health.app, health.version, Core.baseUrl())
            or tostring(healthError))

    if health then
        add(tonumber(health.schema) == 3, 'engine schema',
            tostring(health.schema) .. ' (plug-in expects 3)')
        local stats, statsError = Core.call('/api/stats')
        add(stats ~= nil, 'library statistics',
            stats and string.format('%d photographs, %d collections', stats.images, stats.collections)
                or tostring(statsError))
        local profiles, profileError = Core.profiles()
        add(profiles ~= nil and #profiles > 0, 'profiles available',
            profiles and string.format('%d profiles', #profiles) or tostring(profileError))
        -- A real round-trip: create a throw-away collection, then delete it.
        local probe, postError = Core.call('/api/collections', {
            method = 'POST',
            payload = { name = 'Kestrel diagnostics probe', genre_hint = 'general' },
            timeout = 10,
        })
        if probe and probe.collection and probe.collection.id then
            Core.call('/api/collections/' .. probe.collection.id, { method = 'DELETE' })
            add(true, 'POST round-trip', 'created and deleted a probe collection')
        else
            add(false, 'POST round-trip', tostring(postError))
        end
        local caps = health.capabilities or {}
        local count = 0
        for _ in pairs(caps) do count = count + 1 end
        add(count > 0, 'capability report', string.format('%d capabilities', count))
    end

    add(type(Core.recordCount()) == 'number', 'preferences readable',
        string.format('%d photo record(s)', Core.recordCount()))
    add(type(Core.settings().base_url) == 'string', 'settings readable',
        Core.baseUrl())

    local text = table.concat(lines, '\n')
    return text, failures
end

function Menus.diagnostics()
    local text, failures = Menus.runDiagnostics()
    LrDialogs.message('Kestrel — diagnostics',
        text .. string.format('\n\n%d check(s) failed.', failures),
        failures == 0 and 'info' or 'warning')
end

return Menus
