--[[
    KestrelApply — writing edits into Lightroom, safely and reversibly.

    Two rules govern this module:

      1. Only develop settings that Lightroom documents are written. Anything the
         engine sends that the SDK does not understand is dropped and reported,
         never silently forced into a catalogue.
      2. Every value is clamped to Lightroom's own range before it is applied, so
         a bug in the engine can never produce an unopenable photo.

    The previous develop settings of each photo are captured the first time we
    touch it, which is what makes Revert and the compare toggle exact.
]]

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'

local Core = KestrelRequire('KestrelCore')

local Apply = {}

-- Ranges used by Lightroom's develop panel. Anything outside is clamped.
local RANGES = {
    Exposure2012 = { -5, 5 },
    Contrast2012 = { -100, 100 },
    Highlights2012 = { -100, 100 }, Shadows2012 = { -100, 100 },
    Whites2012 = { -100, 100 }, Blacks2012 = { -100, 100 },
    Texture = { -100, 100 }, Clarity2012 = { -100, 100 }, Dehaze = { -100, 100 },
    Vibrance = { -100, 100 }, Saturation = { -100, 100 },
    Sharpness = { 0, 150 }, SharpenRadius = { 0.5, 3.0 },
    SharpenDetail = { 0, 100 }, SharpenEdgeMasking = { 0, 100 },
    LuminanceSmoothing = { 0, 100 }, LuminanceDetail = { 0, 100 },
    ColorNoiseReduction = { 0, 100 }, ColorNoiseReductionDetail = { 0, 100 },
    ColorNoiseReductionSmoothness = { 0, 100 },
    PostCropVignetteAmount = { -100, 100 }, PostCropVignetteMidpoint = { 0, 100 },
    PostCropVignetteFeather = { 0, 100 }, PostCropVignetteRoundness = { -100, 100 },
    PostCropVignetteStyle = { 1, 3 }, PostCropVignetteHighlightContrast = { 0, 100 },
    GrainAmount = { 0, 100 }, GrainSize = { 0, 100 }, GrainFrequency = { 0, 100 },
    LensProfileEnable = { 0, 1 }, LensManualDistortionAmount = { -100, 100 },
    AutoLateralCA = { 0, 1 },
    DefringePurpleAmount = { 0, 20 }, DefringeGreenAmount = { 0, 20 },
    DefringePurpleHueLo = { 0, 100 }, DefringePurpleHueHi = { 0, 100 },
    DefringeGreenHueLo = { 0, 100 }, DefringeGreenHueHi = { 0, 100 },
    ParametricShadows = { -100, 100 }, ParametricDarks = { -100, 100 },
    ParametricLights = { -100, 100 }, ParametricHighlights = { -100, 100 },
    ParametricShadowSplit = { 0, 100 }, ParametricMidtoneSplit = { 0, 100 },
    ParametricHighlightSplit = { 0, 100 },
    SplitToningShadowHue = { 0, 360 }, SplitToningShadowSaturation = { 0, 100 },
    SplitToningHighlightHue = { 0, 360 }, SplitToningHighlightSaturation = { 0, 100 },
    SplitToningBalance = { -100, 100 },
    HueAdjustmentRed = { -100, 100 }, HueAdjustmentOrange = { -100, 100 },
    HueAdjustmentYellow = { -100, 100 }, HueAdjustmentGreen = { -100, 100 },
    HueAdjustmentAqua = { -100, 100 }, HueAdjustmentBlue = { -100, 100 },
    HueAdjustmentPurple = { -100, 100 }, HueAdjustmentMagenta = { -100, 100 },
    SaturationAdjustmentRed = { -100, 100 }, SaturationAdjustmentOrange = { -100, 100 },
    SaturationAdjustmentYellow = { -100, 100 }, SaturationAdjustmentGreen = { -100, 100 },
    SaturationAdjustmentAqua = { -100, 100 }, SaturationAdjustmentBlue = { -100, 100 },
    SaturationAdjustmentPurple = { -100, 100 }, SaturationAdjustmentMagenta = { -100, 100 },
    LuminanceAdjustmentRed = { -100, 100 }, LuminanceAdjustmentOrange = { -100, 100 },
    LuminanceAdjustmentYellow = { -100, 100 }, LuminanceAdjustmentGreen = { -100, 100 },
    LuminanceAdjustmentAqua = { -100, 100 }, LuminanceAdjustmentBlue = { -100, 100 },
    LuminanceAdjustmentPurple = { -100, 100 }, LuminanceAdjustmentMagenta = { -100, 100 },
    RedEye = { 0, 1 },
    RetouchArea = { 0, 1 },
}

local ENUMS = {
    WhiteBalance = { ['As Shot'] = true, Auto = true, Daylight = true, Cloudy = true,
                     Shade = true, Tungsten = true, Fluorescent = true, Flash = true,
                     Custom = true },
    ToneCurveName2012 = { Linear = true, ['Medium Contrast'] = true,
                          ['Strong Contrast'] = true, Custom = true },
    ProcessVersion = { ['5.4'] = true, ['6.7'] = true, ['11.0'] = true, ['15.4'] = true },
}

local STRINGS = {
    WhiteBalance = true, ToneCurveName2012 = true, ProcessVersion = true,
    CameraProfile = true, Look = true, PresetType = false,   -- PresetType is not a develop key
}

local CURVES = {
    ToneCurvePV2012 = true, ToneCurvePV2012Red = true,
    ToneCurvePV2012Green = true, ToneCurvePV2012Blue = true,
}

Apply.RANGES = RANGES

--- Keep only settings Lightroom understands, clamped into range.
--  Returns settings, report where report = { dropped = {...}, adjusted = {...}, kept = n }
function Apply.filter(settings)
    local out, dropped, adjusted = {}, {}, {}
    local kept = 0
    for key, value in pairs(settings or {}) do
        local kind = type(value)
        if CURVES[key] then
            if kind == 'table' and #value > 1 and #value <= 256 then
                out[key] = value
                kept = kept + 1
            else
                dropped[#dropped + 1] = key
            end
        elseif ENUMS[key] then
            if kind == 'string' and ENUMS[key][value] then
                out[key] = value
                kept = kept + 1
            else
                dropped[#dropped + 1] = key
            end
        elseif RANGES[key] then
            local number = tonumber(value)
            if number == nil or number ~= number then
                dropped[#dropped + 1] = key
            else
                local low, high = RANGES[key][1], RANGES[key][2]
                local clamped = number
                if clamped < low then clamped = low end
                if clamped > high then clamped = high end
                if clamped ~= number then
                    adjusted[#adjusted + 1] = { key = key, from = number, to = clamped }
                end
                out[key] = clamped
                kept = kept + 1
            end
        elseif kind == 'string' and STRINGS[key] then
            out[key] = value
            kept = kept + 1
        else
            dropped[#dropped + 1] = key
        end
    end
    return out, { dropped = dropped, adjusted = adjusted, kept = kept }
end

-- --------------------------------------------------------------------------- apply
local function writeAccess(name, fn)
    local catalog = LrApplication.activeCatalog()
    local ok, err = pcall(function()
        catalog:withWriteAccessDo(name, fn, { timeout = 60 })
    end)
    return ok, err
end

--- Apply a settings table. Options are passed when Lightroom supports them; a
--- failure falls back to the documented single-argument form.
function Apply.apply(photo, settings, name)
    if not photo or not settings then return false, 'missing photo or settings' end
    local ok, err = writeAccess(name or 'Kestrel edit', function()
        local applied, firstError = pcall(photo.applyDevelopSettings, photo, settings)
        if applied then return end
        -- Older Lightroom builds reject the whole table when one key is unknown,
        -- so retry with the strict whitelist before giving up.
        local filtered = Apply.filter(settings)
        local retried, retryError = pcall(photo.applyDevelopSettings, photo, filtered)
        if not retried then error(retryError or firstError, 0) end
    end)
    if not ok then
        Core.logError('apply failed', err)
        return false, tostring(err)
    end
    return true
end

--- Apply exactly what Lightroom reported before we touched the photo.
function Apply.applyBaseline(photo, baseline)
    if not baseline then return false, 'no baseline' end
    local ok, err = writeAccess('Kestrel revert', function()
        local applied, firstError = pcall(photo.applyDevelopSettings, photo, baseline)
        if applied then return end
        local filtered = Apply.filter(baseline)
        local retried, retryError = pcall(photo.applyDevelopSettings, photo, filtered)
        if not retried then error(retryError or firstError, 0) end
    end)
    if not ok then return false, tostring(err) end
    return true
end

function Apply.currentSettings(photo)
    local ok, settings = pcall(function() return photo:getDevelopSettings() end)
    if ok then return settings end
    return nil
end

-- --------------------------------------------------------------------------- packet
function Apply.maskSummary(packet)
    local masks = packet and packet.masks
    if type(masks) ~= 'table' then return { count = 0, list = {} } end
    local list = masks.masks or {}
    local byCapability = {}
    for _, mask in ipairs(list) do
        local capability = mask.capability or 'plugin'
        byCapability[capability] = (byCapability[capability] or 0) + 1
    end
    return { count = #list, list = list, byCapability = byCapability }
end

--- Apply a delivery packet to a photo and remember what changed.
--  Returns a result table: { applied = bool, globals = n, masks = n, error = ... }
function Apply.applyPacket(photo, packet, mode)
    if not packet then return { applied = false, error = 'empty packet' } end
    local settings, report = Apply.filter(packet.settings)
    local record = Core.record(photo) or {}
    local baseline = record.baseline
    local keepSnapshot = Core.settings().keep_snapshot

    if baseline == nil and keepSnapshot then
        local current = Apply.currentSettings(photo)
        if current then
            baseline = current
        end
    end

    local ok, err = Apply.apply(photo, settings, 'Kestrel: apply edit')
    if not ok then
        return { applied = false, error = err, dropped = report.dropped }
    end

    local masks = Apply.maskSummary(packet)
    local changed = {
        version_id = packet.version_id,
        image_id = packet.image_id,
        baseline = baseline,
        delivered = settings,
        applied = settings,
        showing = 'delivered',
        mode = mode or packet.mode,
        genre = packet.genre,
        intent = packet.intent,
        packet_path = packet.packet_path,
        score = packet.metrics and packet.metrics.score,
        masks = masks.count,
        delivered_at = LrDate.currentTime(),
    }
    Core.updateRecord(photo, changed)
    return {
        applied = true,
        globals = report.kept,
        masks = masks.count,
        dropped = report.dropped,
        adjusted = report.adjusted,
        score = changed.score,
    }
end

-- --------------------------------------------------------------------------- revert
function Apply.revert(photo)
    local record = Core.record(photo)
    if not record or not record.baseline then
        return false, 'Kestrel has no saved original settings for this photo.'
    end
    local ok, err = Apply.applyBaseline(photo, record.baseline)
    if not ok then
        return false, 'Lightroom refused to restore the original settings: ' .. tostring(err)
    end
    Core.updateRecord(photo, { showing = 'original', applied = nil })
    return true
end

--- Toggle between the Kestrel result and the exact pre-edit settings.
function Apply.toggle(photo)
    local record = Core.record(photo)
    if not record then
        return nil, 'This photo has not been edited by Kestrel yet.'
    end
    if record.showing == 'delivered' then
        local ok, err = Apply.revert(photo)
        if not ok then return nil, err end
        return 'original'
    end
    if not record.delivered then
        return nil, 'No Kestrel result is stored for this photo.'
    end
    local ok, err = Apply.apply(photo, record.delivered, 'Kestrel: restore result')
    if not ok then return nil, err end
    Core.updateRecord(photo, { showing = 'delivered', applied = record.delivered })
    return 'delivered'
end

--- Copy one photo's delivered develop settings onto others (global values only).
function Apply.syncSettings(photo, others)
    local record = Core.record(photo)
    if not record or not record.delivered then
        return 0, 0, 'The source photo has no Kestrel result yet.'
    end
    local applied, errors = 0, {}
    for _, target in ipairs(others) do
        if target ~= photo then
            local previous = Core.record(target)
            local baseline = (previous and previous.baseline) or Apply.currentSettings(target)
            local ok, err = Apply.apply(target, record.delivered, 'Kestrel: sync look')
            if ok then
                Core.updateRecord(target, {
                    baseline = baseline,
                    delivered = record.delivered,
                    applied = record.delivered,
                    showing = 'delivered',
                    version_id = record.version_id,
                    synced_from = record.path,
                })
                applied = applied + 1
            else
                errors[#errors + 1] = err
            end
        end
    end
    return applied, #errors, errors[1]
end

-- --------------------------------------------------------------------------- render
--- Add an engine-rendered TIFF to the catalogue, next to its original.
function Apply.addRenderedCopy(originalPath, renderedPath)
    local catalog = LrApplication.activeCatalog()
    local photo
    local ok, result = pcall(function()
        return catalog:addPhoto(renderedPath, { addToSelection = false })
    end)
    if ok and result then
        photo = result
    else
        local ok2, fallback = pcall(function() return catalog:addPhoto(renderedPath) end)
        if ok2 then photo = fallback end
    end
    if not photo then
        return nil, 'Lightroom could not import the rendered file: ' .. tostring(renderedPath)
    end
    return photo
end

return Apply
