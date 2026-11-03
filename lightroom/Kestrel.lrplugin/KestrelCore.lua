--[[
    KestrelCore — settings, engine transport, library records.

    Everything the plug-in knows about a photograph lives in Lightroom's own
    preferences (LrPrefs), keyed by the photo's UUID with the path as a fallback.
    No database of its own: if Lightroom's preferences survive, so do our
    snapshots and the mapping to the engine's edit versions.
]]

local LrApplication = import 'LrApplication'
local LrDate = import 'LrDate'
local LrDialogs = import 'LrDialogs'
local LrHttp = import 'LrHttp'
local LrLogger = import 'LrLogger'
local LrPathUtils = import 'LrPathUtils'
local LrPrefs = import 'LrPrefs'
local LrTasks = import 'LrTasks'

local Json = KestrelRequire('KestrelJson')

local Core = {}

local log = LrLogger('Kestrel')
log:enable('logfile')

local prefs = LrPrefs.prefsForPlugin()

-- --------------------------------------------------------------------------- settings
local DEFAULTS = {
    base_url = 'http://127.0.0.1:8765',
    default_quality = 'high',       -- standard | high | maximum
    default_intent = 'automatic',   -- automatic | natural | vibrant | dramatic | editorial
    strength = 1.0,
    apply_mode = 'adaptive',        -- adaptive | native | render
    collection_name = 'Kestrel',
    auto_analyse = true,
    keep_snapshot = true,
    open_workspace_after = false,
    max_records = 400,
    request_timeout = 30,
}

function Core.settings()
    local stored = prefs.settings
    if type(stored) ~= 'table' then stored = {} end
    local out = {}
    for key, default in pairs(DEFAULTS) do
        local value = stored[key]
        if value == nil then
            value = default
        elseif type(default) == 'number' then
            value = tonumber(value) or default
        elseif type(default) == 'boolean' then
            value = (value == true)
        elseif type(default) == 'string' then
            value = tostring(value)
        end
        out[key] = value
    end
    return out
end

function Core.saveSettings(changes)
    local current = Core.settings()
    for key, value in pairs(changes or {}) do
        if DEFAULTS[key] ~= nil then
            current[key] = value
        end
    end
    prefs.settings = current
    return current
end

function Core.resetSettings()
    prefs.settings = nil
    return Core.settings()
end

-- --------------------------------------------------------------------------- logging
function Core.log(message, detail)
    local text = '[Kestrel] ' .. tostring(message)
    if detail ~= nil then
        if type(detail) == 'table' then
            text = text .. ' ' .. tostring(Json.encode(detail))
        else
            text = text .. ' ' .. tostring(detail)
        end
    end
    log:info(text)
end

function Core.logError(message, detail)
    local text = '[Kestrel] ' .. tostring(message)
    if detail ~= nil then
        text = text .. ' ' .. tostring(type(detail) == 'table' and Json.encode(detail) or detail)
    end
    log:error(text)
end

-- --------------------------------------------------------------------------- transport
local function trim(value)
    return (tostring(value or ''):gsub('^%s+', ''):gsub('%s+$', ''))
end

function Core.baseUrl()
    local url = trim(Core.settings().base_url)
    if url == '' then url = DEFAULTS.base_url end
    return (url:gsub('/+$', ''))
end

--- Low-level call. Returns a decoded Lua table, or nil plus a readable error.
--  opts: { method = 'GET'|'POST', payload = table, timeout = seconds, retries = n }
function Core.call(path, opts)
    opts = opts or {}
    local method = (opts.method or 'GET'):upper()
    local timeout = opts.timeout or Core.settings().request_timeout
    local attempts = opts.retries or (method == 'GET' and 2 or 1)
    local url = Core.baseUrl() .. path
    local headers = {
        { field = 'Accept', value = 'application/json' },
        { field = 'Content-Type', value = 'application/json' },
    }
    local body = nil
    if opts.payload ~= nil then
        local ok, encoded = pcall(Json.encode, opts.payload)
        if not ok then
            return nil, 'Could not encode the request: ' .. tostring(encoded)
        end
        body = encoded
    end

    local lastError = 'no response'
    for attempt = 1, attempts do
        local raw, response
        if method == 'GET' then
            raw, response = LrHttp.get(url, headers, timeout)
        else
            raw, response = LrHttp.post(url, body or '', headers, method, timeout)
        end

        if raw ~= nil then
            local status = response and tonumber(response.status) or 200
            if status >= 200 and status < 300 then
                if raw == '' then return true end
                local ok, decoded = pcall(Json.decode, raw)
                if ok and type(decoded) == 'table' then
                    return decoded
                end
                return nil, 'The engine returned a response Kestrel could not read.'
            end
            local detail = nil
            local ok, decoded = pcall(Json.decode, raw)
            if ok and type(decoded) == 'table' then
                detail = decoded.message or decoded.detail or decoded.error
            end
            lastError = string.format('HTTP %d %s', status, tostring(detail or raw))
            if status >= 400 and status < 500 then
                return nil, lastError     -- do not retry client errors
            end
        else
            local reason = 'connection failed'
            if type(response) == 'table' and response.error then
                reason = tostring(response.error.errorCode or response.error)
            end
            lastError = string.format('%s (%s)', reason, url)
        end

        if attempt < attempts then
            LrTasks.sleep(0.4 * attempt)
        end
    end
    return nil, lastError
end

function Core.encodeComponent(value)
    return (tostring(value or ''):gsub('[^%w%-%._~]', function(char)
        return string.format('%%%02X', string.byte(char))
    end))
end

function Core.health(timeout)
    return Core.call('/api/health', { timeout = timeout or 4, retries = 1 })
end

function Core.requireEngine()
    local health, err = Core.health()
    if not health then
        return nil, table.concat({
            'Kestrel could not reach its local engine.',
            '',
            'Start it from a terminal with:',
            '    kestrel serve',
            '',
            'Engine address: ' .. Core.baseUrl(),
            'Details: ' .. tostring(err),
        }, '\n')
    end
    return health
end

-- --------------------------------------------------------------------------- records
local function recordKey(photo)
    local ok, uuid = pcall(function() return photo:getRawMetadata('uuid') end)
    if ok and uuid and uuid ~= '' then
        return uuid
    end
    local okPath, path = pcall(function() return photo:getRawMetadata('path') end)
    if okPath and path then return path end
    return nil
end

function Core.photoKey(photo)
    return recordKey(photo)
end

function Core.photoPath(photo)
    local ok, path = pcall(function() return photo:getRawMetadata('path') end)
    if ok and path and path ~= '' then return path end
    return nil
end

local function records()
    local all = prefs.records
    if type(all) ~= 'table' then all = {} end
    return all
end

local function prune(all)
    local limit = Core.settings().max_records
    local keys, count = {}, 0
    for key in pairs(all) do
        keys[#keys + 1] = key
        count = count + 1
    end
    if count <= limit then return all end
    table.sort(keys, function(a, b)
        return tonumber(all[a].updated or 0) < tonumber(all[b].updated or 0)
    end)
    local drop = count - limit
    for index = 1, drop do
        all[keys[index]] = nil
    end
    return all
end

function Core.record(photo)
    local key = recordKey(photo)
    if not key then return nil end
    return records()[key]
end

function Core.updateRecord(photo, changes)
    local key = recordKey(photo)
    if not key then return nil end
    local all = records()
    local entry = all[key] or {}
    for field, value in pairs(changes or {}) do
        entry[field] = value
    end
    entry.updated = LrDate.currentTime()
    entry.path = Core.photoPath(photo) or entry.path
    all[key] = entry
    prefs.records = prune(all)
    return entry
end

function Core.forgetRecord(photo)
    local key = recordKey(photo)
    if not key then return end
    local all = records()
    all[key] = nil
    prefs.records = all
end

function Core.recordCount()
    local count = 0
    for _ in pairs(records()) do count = count + 1 end
    return count
end

-- --------------------------------------------------------------------------- library
function Core.listCollections()
    local data, err = Core.call('/api/collections')
    if not data then return nil, err end
    return data.collections or {}
end

function Core.findCollection(name)
    local collections, err = Core.listCollections()
    if not collections then return nil, err end
    for _, collection in ipairs(collections) do
        if collection.name == name then
            return collection
        end
    end
    return nil
end

function Core.ensureCollection(name, genreHint)
    if not name or name == '' then return nil end
    local existing, err = Core.findCollection(name)
    if err then return nil, err end
    if existing then return existing end
    local created, createErr = Core.call('/api/collections', {
        method = 'POST',
        payload = { name = name, genre_hint = genreHint or '' },
    })
    if not created then return nil, createErr end
    return created.collection
end

--- Import one photo path (idempotent: the engine reports existing files as skipped).
function Core.importPath(path, lrUuid, collectionId)
    local payload = { paths = { path } }
    if lrUuid then
        payload.lr_uuids = { [LrPathUtils.leafName(path)] = lrUuid }
    end
    if collectionId then payload.collection_id = collectionId end
    local data, err = Core.call('/api/import', { method = 'POST', payload = payload, timeout = 120 })
    if not data then return nil, err end
    local created = data.created or {}
    local skipped = data.skipped or {}
    local failed = data.failed or {}
    local entry = created[1] or skipped[1]
    if not entry then
        local reason = failed[1] and failed[1].error or 'the engine did not accept the file'
        return nil, reason
    end
    return entry.id, nil, data
end

--- Find the engine's image id for a photo, importing it if necessary.
function Core.imageIdFor(photo, collectionName)
    local path = Core.photoPath(photo)
    if not path then
        return nil, 'This photo has no file on this machine.'
    end
    local record = Core.record(photo)
    if record and record.image_id then
        return record.image_id, nil, record
    end

    local collection = nil
    if collectionName and collectionName ~= '' then
        collection = Core.ensureCollection(collectionName)
    end
    local lrUuid = recordKey(photo)
    local imageId, err = Core.importPath(path, lrUuid, collection and collection.id or nil)
    if not imageId then
        return nil, ('Could not register %s with the engine: %s'):format(
            LrPathUtils.leafName(path), tostring(err))
    end
    local entry = Core.updateRecord(photo, {
        image_id = imageId,
        filename = LrPathUtils.leafName(path),
    })
    return imageId, nil, entry
end

function Core.analyse(imageId, quality)
    local query = '/api/images/' .. imageId .. '/analyse'
    if quality then query = query .. '?quality=' .. Core.encodeComponent(quality) end
    local data, err = Core.call(query, { method = 'POST', timeout = 300 })
    if not data then return nil, err end
    return data
end

--- opts: intent, strength, quality, profile_id, genre
function Core.preview(imageId, opts)
    opts = opts or {}
    local payload = {
        intent = opts.intent,
        strength = tonumber(opts.strength) or 1.0,
        quality = opts.quality,
        profile_id = opts.profile_id,
        genre = opts.genre,
        save_version = true,
    }
    local data, err = Core.call('/api/images/' .. imageId .. '/preview', {
        method = 'POST',
        payload = payload,
        timeout = 900,      -- a Maximum render is allowed to take minutes
    })
    if not data then return nil, err end
    return data
end

function Core.delivery(imageId, versionId, mode)
    local data, err = Core.call('/api/images/' .. imageId .. '/apply', {
        method = 'POST',
        payload = { version_id = versionId, mode = mode or 'adaptive' },
        timeout = 120,
    })
    if not data then return nil, err end
    return data.payload
end

function Core.exportRender(imageId, versionId, quality, format)
    local data, err = Core.call('/api/images/' .. imageId .. '/export', {
        method = 'POST',
        payload = { version_id = versionId, quality = quality, format = format or 'tiff' },
        timeout = 900,
    })
    if not data then return nil, err end
    return data
end

function Core.feedback(imageId, kind, versionId, rating)
    local data, err = Core.call('/api/images/' .. imageId .. '/feedback', {
        method = 'POST',
        payload = { kind = kind, version_id = versionId, rating = rating or 0 },
    })
    if not data then return nil, err end
    return data
end

function Core.profiles(genre)
    local path = '/api/profiles'
    if genre and genre ~= '' then path = path .. '?genre=' .. Core.encodeComponent(genre) end
    local data, err = Core.call(path)
    if not data then return nil, err end
    return data.profiles or {}
end

-- --------------------------------------------------------------------------- dialogs
function Core.error(title, message)
    Core.logError(title, message)
    LrDialogs.message(title, message, 'critical')
end

--- Run a function, showing a clean error dialog instead of a Lightroom traceback.
function Core.guard(title, fn)
    return function(...)
        local ok, err = pcall(fn, ...)
        if not ok then
            Core.error(title, tostring(err))
        end
    end
end

return Core
