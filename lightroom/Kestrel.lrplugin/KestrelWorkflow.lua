--[[
    KestrelWorkflow — the per-photo pipeline.

    One photograph at a time: register it with the engine, analyse it, ask for a
    plan, ask for the delivery packet, then write that packet into Lightroom.
    Batch work is only a loop over this function, which is why a failure in the
    middle of a shoot never costs the frames that already succeeded.

    Nothing here is destructive: the original file is never touched, and the
    pre-edit develop settings are captured before the first write.
]]

local LrPathUtils = import 'LrPathUtils'

local Apply = KestrelRequire('KestrelApply')
local Core = KestrelRequire('KestrelCore')

local Workflow = {}

-- --------------------------------------------------------------------------- options
function Workflow.defaultOptions(overrides)
    local settings = Core.settings()
    local options = {
        intent = settings.default_intent,
        quality = settings.default_quality,
        strength = settings.strength,
        mode = settings.apply_mode,
        collection = settings.collection_name,
        analyse = settings.auto_analyse,
        genre = nil,
        profile_id = nil,
    }
    for key, value in pairs(overrides or {}) do
        if value ~= nil then options[key] = value end
    end
    return options
end

local function elapsedSeconds(started)
    local now = os.time()
    return math.max(0, now - (started or now))
end

-- --------------------------------------------------------------------------- preview
--- Analyse and plan without writing anything to the catalogue.
function Workflow.inspect(photo, options)
    local opts = Workflow.defaultOptions(options)
    local started = os.time()

    local imageId, err = Core.imageIdFor(photo, opts.collection)
    if not imageId then
        return { ok = false, photo = photo, error = err }
    end

    local analysis, analysisErr = Core.analyse(imageId, opts.quality)
    if not analysis and opts.analyse ~= false then
        return { ok = false, photo = photo, error = 'Analysis failed: ' .. tostring(analysisErr) }
    end

    local preview, previewErr = Core.preview(imageId, {
        intent = opts.intent,
        strength = opts.strength,
        quality = opts.quality,
        profile_id = opts.profile_id,
        genre = opts.genre,
    })
    if not preview then
        return { ok = false, photo = photo, error = 'Rendering failed: ' .. tostring(previewErr) }
    end

    local packet, deliveryErr
    if opts.mode ~= 'render' then
        packet, deliveryErr = Core.delivery(imageId, preview.version.id, opts.mode)
        if not packet then
            return { ok = false, photo = photo, error = 'Delivery packet failed: ' .. tostring(deliveryErr) }
        end
    end

    return {
        ok = true,
        photo = photo,
        image_id = imageId,
        version = preview.version,
        version_id = preview.version.id,
        analysis = preview.analysis or (analysis and analysis.analysis),
        plan = preview.plan,
        explanation = preview.explanation or {},
        masks = preview.masks or {},
        metrics = preview.metrics or (preview.version and preview.version.metrics) or {},
        warnings = preview.warnings or {},
        profile = preview.profile,
        genre = preview.plan and preview.plan.genre,
        packet = packet,
        seconds = elapsedSeconds(started),
        filename = Core.photoPath(photo) and LrPathUtils.leafName(Core.photoPath(photo)),
    }
end

-- --------------------------------------------------------------------------- edit one
--- Full path: inspect, then deliver (native settings or a rendered copy).
function Workflow.editPhoto(photo, options)
    local opts = Workflow.defaultOptions(options)
    local started = os.time()
    local result = Workflow.inspect(photo, opts)
    if not result.ok then return result end

    if opts.mode == 'render' then
        local exported, exportErr = Core.exportRender(result.image_id, result.version_id, opts.quality, 'tiff')
        if not exported then
            result.ok = false
            result.error = 'Render export failed: ' .. tostring(exportErr)
            return result
        end
        local added, addErr = Apply.addRenderedCopy(Core.photoPath(photo), exported.path)
        if not added then
            result.ok = false
            result.error = addErr
            return result
        end
        Core.updateRecord(photo, {
            version_id = result.version_id,
            image_id = result.image_id,
            render_path = exported.path,
            mode = 'render',
            showing = 'rendered',
            score = result.metrics and result.metrics.score,
            genre = result.genre,
        })
        result.rendered = true
        result.render_path = exported.path
        result.globals = 0
        result.masks = #(result.masks or {})
        result.seconds = elapsedSeconds(started)
        return result
    end

    local applied = Apply.applyPacket(photo, result.packet, opts.mode)
    result.globals = applied.globals or 0
    result.applied_masks = applied.masks or 0
    result.dropped = applied.dropped or {}
    if not applied.applied then
        result.ok = false
        result.error = applied.error or 'Lightroom rejected the develop settings.'
        return result
    end
    result.applied = true
    result.seconds = elapsedSeconds(started)
    return result
end

-- --------------------------------------------------------------------------- many
--- Edit a list of photos. A failure is recorded and the loop continues.
--  hooks: { progress = fn(index, total, photo), onResult = fn(result),
--           cancelled = fn() -> bool }
function Workflow.editPhotos(photos, options, hooks)
    hooks = hooks or {}
    local results, failures = {}, {}
    local started = os.time()
    local total = #photos
    local stopped = false
    for index, photo in ipairs(photos) do
        if stopped then break end
        if hooks.cancelled and hooks.cancelled() then
            stopped = true
            failures[#failures + 1] = {
                photo = photo,
                error = string.format('Cancelled after %d of %d photographs.', index - 1, total),
            }
        else
            if hooks.progress then pcall(hooks.progress, index, total, photo) end
            local ok, result = pcall(Workflow.editPhoto, photo, options)
            if not ok then
                failures[#failures + 1] = { photo = photo, error = tostring(result) }
                Core.logError('photo failed', result)
            elseif result.ok then
                results[#results + 1] = result
            else
                failures[#failures + 1] = { photo = photo, error = result.error }
                Core.logError('photo failed', result.error)
            end
            if hooks.onResult then pcall(hooks.onResult, result) end
        end
    end
    return results, failures, elapsedSeconds(started)
end

-- --------------------------------------------------------------------------- sync
function Workflow.syncGroup(photos)
    if #photos < 2 then
        return 0, 0, 'Select at least two photographs: the first one supplies the look.'
    end
    local source = photos[1]
    local others = {}
    for index = 2, #photos do others[#others + 1] = photos[index] end
    return Apply.syncSettings(source, others)
end

-- --------------------------------------------------------------------------- text
local function scoreText(metrics)
    if not metrics or not metrics.score then return '' end
    return string.format(' quality %.3f', tonumber(metrics.score) or 0)
end

function Workflow.summary(results, failures, seconds)
    local lines = {}
    lines[#lines + 1] = string.format('%d photograph(s) edited%s in %s.',
        #results, scoreText(results[1] and results[1].metrics), tostring(seconds or 0) .. ' s')
    if #failures > 0 then
        lines[#lines + 1] = ''
        lines[#lines + 1] = string.format('%d photograph(s) need attention:', #failures)
        for index, failure in ipairs(failures) do
            if index > 8 then
                lines[#lines + 1] = string.format('… and %d more (see the Kestrel log).', #failures - 8)
                break
            end
            lines[#lines + 1] = '  • ' .. tostring(failure.error)
        end
    end
    local first = results[1]
    if first and first.explanation and #first.explanation > 0 then
        lines[#lines + 1] = ''
        lines[#lines + 1] = 'Why the first frame looks the way it does:'
        for index = 1, math.min(4, #first.explanation) do
            local item = first.explanation[index]
            lines[#lines + 1] = string.format('  • %s — %s', tostring(item.action), tostring(item.reason))
        end
    end
    return table.concat(lines, '\n')
end

function Workflow.explanationLines(result, limit)
    local lines = {}
    local items = (result and result.explanation) or {}
    for index = 1, math.min(limit or #items, #items) do
        local item = items[index]
        local amount = (item.amount and item.amount ~= '') and (' [' .. tostring(item.amount) .. ']') or ''
        lines[#lines + 1] = string.format('%d. %s%s\n   %s', index,
            tostring(item.action), amount, tostring(item.reason))
    end
    if #items == 0 then
        lines[#lines + 1] = 'No changes were needed beyond the base profile.'
    end
    return table.concat(lines, '\n')
end

function Workflow.analysisLines(result)
    local analysis = result and result.analysis
    if not analysis then return 'No analysis available.' end
    local scene = analysis.scene or {}
    local exposure = analysis.exposure or {}
    local colour = analysis.colour or {}
    local quality = analysis.quality or {}
    local tags = scene.event_tags or {}
    local lines = {
        string.format('Genre: %s (%.0f%% confidence)', tostring(analysis.genre),
            100 * tonumber(analysis.genre_confidence or 0)),
        string.format('Light: %s%s%s', scene.indoor and scene.indoor > 0.5 and 'indoor' or 'outdoor',
            (scene.night or 0) > 0.4 and ', night' or '',
            (scene.stadium or 0) > 0.4 and ', stadium lighting' or ''),
        string.format('Exposure: median %.2f, highlight clipping %.2f%%, shadow clipping %.2f%%',
            tonumber(exposure.median_luma or 0), 100 * tonumber(exposure.clipping_high or 0),
            100 * tonumber(exposure.clipping_low or 0)),
        string.format('White balance: %.0f K, tint %.0f, cast confidence %.0f%%',
            tonumber(colour.temperature or 0), tonumber(colour.tint or 0),
            100 * tonumber(colour.wb_confidence or 0)),
        string.format('Noise %.3f (ISO ~%s), sharpness %.2f',
            tonumber(quality.noise_sigma or 0), tostring(quality.iso_estimate or '?'),
            tonumber(quality.sharpness or 0)),
    }
    if #tags > 0 then
        lines[#lines + 1] = 'Signals: ' .. table.concat(tags, ', ')
    end
    return table.concat(lines, '\n')
end

function Workflow.secondsText(result)
    if not result then return '' end
    return string.format('%.1f s', tonumber(result.seconds or 0))
end

return Workflow
