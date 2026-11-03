--[[
    PluginInfoProvider — the panel shown in Lightroom's Plug-in Manager.

    It must be useful without blocking, so the address appears immediately and
    the live engine check runs in its own task. Every button does real work.
]]

local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'
local LrView = import 'LrView'

local Core = KestrelRequire('KestrelCore')
local Dialogs = KestrelRequire('KestrelDialogs')
local Menus = KestrelRequire('KestrelMenus')

local provider = {}

local function statusText()
    local health = Core.health(3)
    if not health then
        return 'Engine not reachable — start it with:  kestrel serve'
    end
    return string.format('Engine %s running at %s', health.version, Core.baseUrl())
end

function provider.sectionsForTopOfDialog(f, propertyTable)
    propertyTable.kestrel_engine_status = 'Checking the local engine…'

    LrTasks.startAsyncTask(function()
        local text = statusText()
        pcall(function() propertyTable.kestrel_engine_status = text end)
    end)

    return {
        {
            title = 'Kestrel',
            f:column {
                spacing = f:control_spacing(),
                f:static_text {
                    title = 'AI photo finishing for Lightroom Classic — analysis first, '
                        .. 'explanation always, originals never modified.',
                    width_in_chars = 60,
                },
                f:static_text {
                    title = LrView.bind('kestrel_engine_status'),
                    width_in_chars = 60,
                },
                f:row {
                    spacing = f:control_spacing(),
                    f:push_button {
                        title = 'Settings…',
                        action = function() Dialogs.settings() end,
                    },
                    f:push_button {
                        title = 'Engine status',
                        action = function() Dialogs.status() end,
                    },
                    f:push_button {
                        title = 'Diagnostics',
                        action = function() Menus.diagnostics() end,
                    },
                    f:push_button {
                        title = 'Open workspace',
                        action = function() Dialogs.openWorkspace(nil) end,
                    },
                },
            },
        },
    }
end

function provider.sectionsForBottomOfDialog(f, propertyTable)
    return {
        {
            f:row {
                spacing = f:control_spacing(),
                f:static_text {
                    title = 'Snapshots stored: ' .. tostring(Core.recordCount()),
                },
                f:static_text {
                    title = 'All processing happens on this machine.',
                },
            },
        },
    }
end

return provider
