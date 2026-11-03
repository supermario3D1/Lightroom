--[[
    Kestrel — AI photo finishing for Adobe Lightroom Classic.

    Info.lua is the plug-in entry point: it declares identity, menu items and
    version metadata, and installs the small module loader that the other files
    use.  Lightroom loads this file first, once, when the plug-in is registered.

    There is no `require` inside the Lightroom sandbox, so plugin modules are
    loaded through KestrelRequire(), defined below and cached in package.loaded.
]]

local LrTasks = import 'LrTasks'

local PLUGIN = {
    name        = 'Kestrel',
    version     = '1.0.0',
    engine_schema = 3,
    vendor      = 'Kestrel Imaging',
    support     = 'https://github.com/supermario3D1/Lightroom',
}

-- --------------------------------------------------------------------------- loader
-- Global on purpose: every menu script runs in its own chunk and needs access.
function KestrelRequire(module)
    local key = 'kestrel.' .. module
    local cached = package.loaded[key]
    if cached then
        return cached
    end
    local path = _PLUGIN.path .. '/' .. module .. '.lua'
    local chunk, err = loadfile(path)
    if not chunk then
        error('Kestrel: cannot load module ' .. module .. ' (' .. tostring(err) .. ')', 0)
    end
    local loaded = chunk()
    package.loaded[key] = loaded or true
    return loaded or true
end

function KestrelPluginInfo()
    return PLUGIN
end

-- --------------------------------------------------------------------------- init
function LrInitPlugin()
    -- The plug-in talks to a local engine. Nothing is sent off the machine.
    LrTasks.startAsyncTask(function()
        local Core = KestrelRequire('KestrelCore')
        Core.log('plugin loaded', PLUGIN.version)
    end)
end

return {
    LrSdkVersion = 10.0,
    LrSdkMinimumVersion = 6.0,
    LrToolkitIdentifier = 'com.kestrelimaging.lightroom',
    LrPluginName = 'Kestrel — AI Photo Finishing',
    LrPluginInfoUrl = PLUGIN.support,
    LrPluginInfoProvider = 'PluginInfoProvider.lua',

    LrLibraryMenuItems = {
        {
            title = 'Kestrel — Edit selected photos…',
            file = 'MenuItemEditSelected.lua',
        },
        {
            title = 'Kestrel — Apply latest edit',
            file = 'MenuItemApplyLatest.lua',
        },
        {
            title = 'Kestrel — Preview and explain',
            file = 'MenuItemPreview.lua',
        },
        {
            title = 'Kestrel — Compare original / result',
            file = 'MenuItemCompare.lua',
        },
        {
            title = 'Kestrel — Revert to original',
            file = 'MenuItemRevert.lua',
        },
        {
            title = 'Kestrel — Sync look across selection',
            file = 'MenuItemSync.lua',
        },
        {
            title = 'Kestrel — Process in background queue…',
            file = 'MenuItemQueue.lua',
        },
        {
            title = 'Kestrel — Edit whole collection…',
            file = 'MenuItemEditCollection.lua',
        },
        {
            title = 'Kestrel — Send selection to workspace',
            file = 'MenuItemSendToWorkspace.lua',
        },
        {
            title = 'Kestrel — Engine status',
            file = 'MenuItemStatus.lua',
        },
        {
            title = 'Kestrel — Settings…',
            file = 'MenuItemSettings.lua',
        },
        {
            title = 'Kestrel — Diagnostics',
            file = 'MenuItemDiagnostics.lua',
        },
        {
            title = 'Kestrel — About',
            file = 'MenuItemAbout.lua',
        },
    },

    LrDevelopPresetFolders = {},
}
