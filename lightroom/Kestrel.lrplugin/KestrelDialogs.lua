--[[
    KestrelDialogs — every window the photographer sees from Lightroom.

    Text is generated from real engine responses: if the engine is not running,
    the dialog says so and gives the command to start it. Nothing here invents
    progress or pretends a feature exists.
]]

local LrApplication = import 'LrApplication'
local LrBinding = import 'LrBinding'
local LrDialogs = import 'LrDialogs'
local LrFunctionContext = import 'LrFunctionContext'
local LrHttp = import 'LrHttp'
local LrTasks = import 'LrTasks'
local LrView = import 'LrView'

local Apply = KestrelRequire('KestrelApply')
local Core = KestrelRequire('KestrelCore')
local Workflow = KestrelRequire('KestrelWorkflow')

local Dialogs = {}

local QUALITY = {
    { title = 'Standard — fast check (about 4 s)', value = 'standard' },
    { title = 'High — default, refined (about 12 s)', value = 'high' },
    { title = 'Maximum — deconvolution (about 30 s)', value = 'maximum' },
}

local INTENT = {
    { title = 'Auto — decide from the scene', value = 'automatic' },
    { title = 'Natural — quiet, documentary', value = 'natural' },
    { title = 'Vibrant — brighter colour', value = 'vibrant' },
    { title = 'Dramatic — deep contrast', value = 'dramatic' },
    { title = 'Editorial — magazine finish', value = 'editorial' },
}

local GENRE = {
    { title = 'Detect automatically', value = '' },
    { title = 'Sports', value = 'sports' },
    { title = 'Wedding', value = 'wedding' },
    { title = 'Portrait', value = 'portrait' },
    { title = 'Event', value = 'event' },
    { title = 'Landscape', value = 'landscape' },
    { title = 'Wildlife', value = 'wildlife' },
    { title = 'Commercial', value = 'commercial' },
    { title = 'General', value = 'general' },
}

local MODE = {
    { title = 'Native develop settings — editable in Lightroom', value = 'adaptive' },
    { title = 'Native settings only — ignore local masks', value = 'native' },
    { title = 'Rendered 16-bit TIFF — all local work baked in', value = 'render' },
}

-- --------------------------------------------------------------------------- options
--- Ask how to edit. Returns a table of options, or nil when cancelled.
function Dialogs.editOptions(title, subtitle, initial, count)
    local chosen = nil
    LrFunctionContext.callWithContext('kestrel.editOptions', function(context)
        local props = LrBinding.makePropertyTable(context)
        local defaults = Workflow.defaultOptions(initial)
        props.quality = defaults.quality
        props.intent = defaults.intent
        props.genre = defaults.genre or ''
        props.mode = defaults.mode
        props.strength = tonumber(defaults.strength) or 1.0
        props.reuse = defaults.analyse ~= false
        props.subject = defaults.subject or ''

        local f = LrView.osFactory()
        local contents = f:column {
            bind_to_object = props,
            spacing = f:control_spacing(),

            f:static_text {
                title = subtitle or 'Every photograph is analysed and edited individually.',
                width_in_chars = 64,
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Quality', width = 90 },
                f:popup_menu { items = QUALITY, value = f:bind('quality'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Style', width = 90 },
                f:popup_menu { items = INTENT, value = f:bind('intent'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Genre', width = 90 },
                f:popup_menu { items = GENRE, value = f:bind('genre'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Deliver as', width = 90 },
                f:popup_menu { items = MODE, value = f:bind('mode'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Strength', width = 90 },
                f:slider { value = f:bind('strength'), min = 0.2, max = 1.5, width = 240 },
                f:static_text { title = f:bind('strength') },
            },
            f:checkbox {
                title = 'Reuse cached analysis when the file has not changed',
                value = f:bind('reuse'),
            },
            f:static_text {
                title = string.format('%d photograph(s). Originals are never modified; '
                    .. 'the current develop settings are saved so Revert is exact.', count or 0),
                width_in_chars = 64,
            },
        }

        local answer = LrDialogs.presentModalDialog {
            title = title or 'Kestrel — edit',
            contents = contents,
            actionVerb = 'Edit',
            cancelVerb = 'Cancel',
        }
        if answer == 'ok' then
            chosen = {
                quality = props.quality,
                intent = props.intent,
                genre = (props.genre ~= '' and props.genre) or nil,
                mode = props.mode,
                strength = tonumber(props.strength) or 1.0,
                analyse = props.reuse and true or false,
            }
        end
    end)
    return chosen
end

-- --------------------------------------------------------------------------- settings
function Dialogs.settings()
    local saved = nil
    LrFunctionContext.callWithContext('kestrel.settings', function(context)
        local props = LrBinding.makePropertyTable(context)
        local current = Core.settings()
        props.base_url = current.base_url
        props.default_quality = current.default_quality
        props.default_intent = current.default_intent
        props.apply_mode = current.apply_mode
        props.collection_name = current.collection_name
        props.keep_snapshot = current.keep_snapshot
        props.max_records = current.max_records
        props.open_workspace_after = current.open_workspace_after

        local f = LrView.osFactory()
        local contents = f:column {
            bind_to_object = props,
            spacing = f:control_spacing(),
            f:static_text {
                title = 'Kestrel runs a local engine. Start it with “kestrel serve” and '
                    .. 'leave it running while you edit.',
                width_in_chars = 62,
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Engine address', width = 130 },
                f:edit_field { value = f:bind('base_url'), width_in_chars = 34 },
                f:push_button {
                    title = 'Test',
                    action = function()
                        local url = props.base_url
                        Core.saveSettings({ base_url = url })
                        LrTasks.startAsyncTask(function()
                            local health, err = Core.health(4)
                            if health then
                                LrDialogs.message('Kestrel engine reachable',
                                    string.format('%s %s\nSchema %s\nLibrary: %s',
                                        health.app, health.version, tostring(health.schema),
                                        Core.baseUrl()), 'info')
                            else
                                Core.error('Kestrel engine not reachable',
                                    'No answer from ' .. tostring(url) .. '\n\n' .. tostring(err)
                                    .. '\n\nStart the engine with:  kestrel serve')
                            end
                        end)
                    end,
                },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Default quality', width = 130 },
                f:popup_menu { items = QUALITY, value = f:bind('default_quality'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Default style', width = 130 },
                f:popup_menu { items = INTENT, value = f:bind('default_intent'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Delivery', width = 130 },
                f:popup_menu { items = MODE, value = f:bind('apply_mode'), width = 300 },
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Collection name', width = 130 },
                f:edit_field { value = f:bind('collection_name'), width_in_chars = 24 },
                f:static_text { title = 'engine-side grouping' },
            },
            f:checkbox {
                title = 'Keep a snapshot of the original develop settings (recommended)',
                value = f:bind('keep_snapshot'),
            },
            f:checkbox {
                title = 'Open the Kestrel workspace after a batch',
                value = f:bind('open_workspace_after'),
            },
            f:row {
                spacing = f:control_spacing(),
                f:static_text { title = 'Remembered photos', width = 130 },
                f:edit_field { value = f:bind('max_records'), width_in_chars = 6, precision = 0 },
                f:static_text { title = string.format('(%d stored now)', Core.recordCount()) },
            },
        }

        local answer = LrDialogs.presentModalDialog {
            title = 'Kestrel — settings',
            contents = contents,
            actionVerb = 'Save',
            cancelVerb = 'Cancel',
            otherVerb = 'Reset',
        }
        if answer == 'ok' then
            saved = Core.saveSettings({
                base_url = props.base_url,
                default_quality = props.default_quality,
                default_intent = props.default_intent,
                apply_mode = props.apply_mode,
                collection_name = props.collection_name,
                keep_snapshot = props.keep_snapshot ~= false,
                max_records = tonumber(props.max_records) or 400,
                open_workspace_after = props.open_workspace_after and true or false,
            })
        elseif answer == 'other' then
            saved = Core.resetSettings()
        end
    end)
    return saved
end

-- --------------------------------------------------------------------------- status
function Dialogs.status()
    local health, err = Core.requireEngine()
    if not health then
        LrDialogs.message('Kestrel — engine not running', err, 'warning')
        return
    end
    local stats, statsErr = Core.call('/api/stats')
    local lines = {
        string.format('%s %s  (engine schema %s)', health.app, health.version, tostring(health.schema)),
        'Address: ' .. Core.baseUrl(),
        '',
        'Hardware',
        string.format('  CPU %s × %d cores, %.1f GB RAM',
            tostring((health.hardware or {}).cpu_name), tonumber((health.hardware or {}).cpu_count) or 0,
            tonumber((health.hardware or {}).total_memory_gb) or 0),
        string.format('  ONNX providers: %s',
            table.concat((health.hardware or {}).onnx_providers or { 'CPU' }, ', ')),
        '',
    }
    if stats then
        lines[#lines + 1] = string.format('Library: %d photographs, %d collections, %d versions',
            tonumber(stats.images) or 0, tonumber(stats.collections) or 0, tonumber(stats.versions) or 0)
    else
        lines[#lines + 1] = 'Library statistics unavailable: ' .. tostring(statsErr)
    end
    local queue = health.engine or {}
    lines[#lines + 1] = ''
    lines[#lines + 1] = string.format('Settings: quality %s, %d worker(s), memory budget %.1f GB',
        tostring(queue.analysis_edge_default), tonumber(queue.max_workers) or 1,
        tonumber(queue.memory_budget_gb) or 0)
    local caps = health.capabilities or {}
    local names = {}
    for name in pairs(caps) do names[#names + 1] = name end
    table.sort(names)
    lines[#lines + 1] = ''
    lines[#lines + 1] = 'Delivery capability'
    for _, name in ipairs(names) do
        lines[#lines + 1] = string.format('  %-26s %s', name, tostring(caps[name]))
    end
    LrDialogs.message('Kestrel — engine status', table.concat(lines, '\n'), 'info')
end

-- --------------------------------------------------------------------------- explain
--- Show what the engine saw and why it changed things, then optionally apply.
function Dialogs.explain(result)
    if not result or not result.ok then
        LrDialogs.message('Kestrel — preview',
            'This photograph could not be analysed.\n\n' .. tostring(result and result.error), 'warning')
        return 'failed'
    end
    local metrics = result.metrics or {}
    local text = table.concat({
        Workflow.analysisLines(result),
        '',
        string.format('Planned edit: %s, %s, strength %.2f',
            tostring(result.genre), tostring(result.plan and result.plan.intent or ''),
            tonumber(result.plan and result.plan.strength or 1)),
        result.profile and ('Profile: ' .. tostring(result.profile)) or '',
        string.format('Quality score %.3f · naturalness %.3f · skin ΔE %.1f',
            tonumber(metrics.score or 0), tonumber(metrics.naturalness or 0),
            tonumber(metrics.skin_delta_e or 0)),
        '',
        'What Kestrel changed and why',
        Workflow.explanationLines(result, 8),
        '',
        'Rendered in ' .. Workflow.secondsText(result) .. ' · engine ' .. Core.baseUrl(),
    }, '\n')

    local confirm = LrDialogs.confirm {
        title = 'Kestrel — preview and explanation',
        message = text,
        actionVerb = 'Apply this edit',
        cancelVerb = 'Not now',
    }
    if confirm == 'ok' then
        local mode = Core.settings().apply_mode
        if mode == 'render' then
            return 'apply-render'
        end
        local applied = Apply.applyPacket(result.photo, result.packet, mode)
        if not applied.applied then
            Core.error('Kestrel could not apply the edit', tostring(applied.error))
            return 'failed'
        end
        return 'applied'
    end
    return 'skipped'
end

-- --------------------------------------------------------------------------- summary
function Dialogs.summary(results, failures, seconds)
    local text = Workflow.summary(results, failures, seconds)
    local answer = LrDialogs.presentModalDialog {
        title = 'Kestrel — finished',
        contents = function(f)
            return f:column {
                spacing = f:control_spacing(),
                f:static_text { title = text, width_in_chars = 70 },
            }
        end,
        actionVerb = 'Close',
        cancelVerb = 'Close',
        otherVerb = 'Open workspace',
    }
    if answer == 'other' then
        Dialogs.openWorkspace(results[1] and results[1].image_id or nil)
    end
    return answer
end

function Dialogs.openWorkspace(imageId)
    local url = Core.baseUrl() .. '/'
    if imageId then
        url = url .. '#/editor/' .. imageId
    end
    local ok = pcall(function() LrHttp.openUrlInBrowser(url) end)
    if not ok then
        LrDialogs.message('Open the Kestrel workspace',
            'Open this address in your browser:\n\n' .. url, 'info')
    end
end

function Dialogs.about()
    local info = KestrelPluginInfo and KestrelPluginInfo() or { version = '1.0.0' }
    LrDialogs.message('Kestrel — AI photo finishing for Lightroom',
        table.concat({
            'Version ' .. tostring(info.version) .. '  ·  plug-in schema 3',
            '',
            'Kestrel analyses every photograph before editing it: scene, light,',
            'faces, subject, noise and colour. It explains each change, keeps the',
            'original untouched, and renders locally.',
            '',
            'Sports is the flagship genre, but the engine also understands weddings,',
            'portraits, events, landscapes, wildlife, commercial work and general',
            'photography. Profiles are preferences the engine interprets, not fixed',
            'slider bundles.',
            '',
            'Privacy: photographs are read from this machine, processed on this',
            'machine and never uploaded. Personalised learning is opt-in and never',
            'retrains a shared model.',
            '',
            'Engine address: ' .. Core.baseUrl(),
            'Documentation: ' .. tostring(info.support),
        }, '\n'), 'info')
end

return Dialogs
