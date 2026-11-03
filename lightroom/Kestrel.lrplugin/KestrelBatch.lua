--[[
    KestrelBatch — the engine-side queue, driven from Lightroom.

    The single-photo path (KestrelWorkflow) is the best choice for a handful of
    frames. For a whole match — or a whole season — the engine's own queue is
    better: it survives Lightroom being closed, it groups photographs by lighting
    condition, and it can be paused and resumed from either interface.

    This module submits a job, watches it, and applies each finished version as
    it arrives, so a five-thousand frame shoot still ends with every edit in the
    catalogue without keeping Lightroom awake for hours.
]]

local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'

local Apply = KestrelRequire('KestrelApply')
local Core = KestrelRequire('KestrelCore')

local Batch = {}

local POLL_SECONDS = 2

--- Register photos with the engine and submit one queue job.
--  Returns jobId, count, error.
function Batch.submit(photos, options)
    local ids, byId = {}, {}
    local failed = 0
    for _, photo in ipairs(photos) do
        local imageId = Core.imageIdFor(photo, options and options.collection)
        if imageId then
            ids[#ids + 1] = imageId
            byId[imageId] = photo
        else
            failed = failed + 1
        end
    end
    if #ids == 0 then
        return nil, 0, 'None of the selected photographs could be registered with the engine.'
    end
    local jobOptions = {
        intent = options and options.intent or 'automatic',
        quality = options and options.quality or 'high',
        strength = options and options.strength or 1.0,
    }
    if options and options.genre then jobOptions.genre = options.genre end

    local created, err = Core.call('/api/jobs', {
        method = 'POST',
        payload = { image_ids = ids, kind = 'edit', priority = 6, options = jobOptions },
    })
    if not created then return nil, #ids, err end
    return created.job.id, #ids, nil, byId, failed
end

--- Poll a job until it finishes. Returns { status, items, job } or nil, error.
function Batch.watch(jobId, callbacks)
    callbacks = callbacks or {}
    local lastCompleted = -1
    while true do
        local data, err = Core.call('/api/jobs/' .. jobId, { timeout = 10 })
        if not data then return nil, err end
        local job = data.job or {}
        local items = data.items or {}
        if callbacks.progress then
            pcall(callbacks.progress, job, items)
        end
        if job.completed ~= lastCompleted then
            lastCompleted = job.completed
            Core.log('job progress', string.format('%s/%s', tostring(job.completed), tostring(job.total)))
        end
        local status = tostring(job.status)
        if status == 'done' or status == 'failed' or status == 'cancelled' then
            return { status = status, items = items, job = job }
        end
        if callbacks.cancelled and callbacks.cancelled() then
            Core.call('/api/jobs/' .. jobId .. '/cancel', { method = 'POST' })
            return { status = 'cancelled', items = items, job = job }
        end
        LrTasks.sleep(POLL_SECONDS)
    end
end

--- Apply every finished version in a job to its Lightroom photo.
--  Returns applied, failed, errors.
function Batch.deliver(items, byId, mode)
    local applied, failed = 0, 0
    local errors = {}
    for _, item in ipairs(items or {}) do
        local photo = byId[item.image_id]
        if photo and item.status == 'done' and item.version_id then
            local packet, err = Core.delivery(item.image_id, item.version_id, mode)
            if not packet then
                failed = failed + 1
                errors[#errors + 1] = tostring(err)
            else
                local outcome = Apply.applyPacket(photo, packet, mode)
                if outcome.applied then
                    applied = applied + 1
                else
                    failed = failed + 1
                    errors[#errors + 1] = tostring(outcome.error)
                end
            end
        elseif photo and item.status == 'failed' then
            failed = failed + 1
            errors[#errors + 1] = tostring(item.error)
        end
    end
    return applied, failed, errors
end

--- Queue a selection in the background and apply the results as they finish.
function Batch.run(photos, options, progress)
    if #photos == 0 then return nil, 'No photographs selected.' end
    local jobId, count, err, byId, unregistered = Batch.submit(photos, options)
    if not jobId then
        return nil, string.format('The engine did not accept the job: %s', tostring(err))
    end
    progress:setCaption(string.format('Queued %d photograph(s). Waiting for the engine…', count))

    local outcome, watchErr = Batch.watch(jobId, {
        cancelled = function() return progress:isCanceled() end,
        progress = function(job)
            local total = math.max(tonumber(job.total) or count, 1)
            local completed = tonumber(job.completed) or 0
            progress:setPortionComplete(completed, total)
            progress:setCaption(string.format('%d of %d — %s', completed, total,
                tostring(job.message or job.status)))
        end,
    })
    if not outcome then
        return nil, string.format('Lost contact with the engine while waiting: %s', tostring(watchErr))
    end

    local applied, failed, errors = Batch.deliver(outcome.items, byId, options and options.mode)
    return {
        job_id = jobId,
        queued = count,
        unregistered = unregistered or 0,
        status = outcome.status,
        applied = applied,
        failed = failed,
        errors = errors,
    }
end

function Batch.summarise(outcome)
    if not outcome then return 'The batch did not run.' end
    local lines = {
        string.format('Queue %s: %d queued, %d applied to Lightroom, %d failed.',
            tostring(outcome.status), outcome.queued, outcome.applied, outcome.failed),
    }
    if (outcome.unregistered or 0) > 0 then
        lines[#lines + 1] = string.format('%d photograph(s) could not be read by the engine.',
            outcome.unregistered)
    end
    for index = 1, math.min(#(outcome.errors or {}), 6) do
        lines[#lines + 1] = '  • ' .. tostring(outcome.errors[index])
    end
    lines[#lines + 1] = ''
    lines[#lines + 1] = 'The job stays in the Kestrel workspace, where it can be paused, '
        .. 'resumed or retried without losing finished frames.'
    return table.concat(lines, '\n')
end

--- Menu command: queue the selection on the engine.
function Batch.enqueueSelection(photos, options, progress)
    local outcome, err = Batch.run(photos, options, progress)
    if not outcome then
        LrDialogs.message('Kestrel — background queue', tostring(err), 'warning')
        return
    end
    LrDialogs.message('Kestrel — background queue', Batch.summarise(outcome),
        outcome.failed == 0 and 'info' or 'warning')
end

return Batch
