--[[
    KestrelJson — a small, strict JSON codec in plain Lua.

    Written for the Lightroom sandbox (Lua 5.1, no external libraries) and kept
    deliberately simple: the payloads Kestrel exchanges are small JSON documents.
    Numbers are encoded with %.15g so develop settings round-trip exactly.
]]

local Json = {}

-- JSON requires escaping only the C0 controls, the quote and the backslash.
-- This is done byte by byte on purpose: Lua's '%c' class is locale-dependent
-- and on some builds (macOS included) matches the C1 range 0x80-0x9F. Those
-- are UTF-8 continuation bytes, so a pattern-based escape splits multi-byte
-- characters and corrupts every non-ASCII string it touches.
local escapeBytes = {
    [8] = '\\b', [9] = '\\t', [10] = '\\n', [12] = '\\f', [13] = '\\r',
    [34] = '\\"', [92] = '\\\\',
}

local function escapeByte(byte)
    local mapped = escapeBytes[byte]
    if mapped then return mapped end
    return string.format('\\u%04x', byte)
end

local function encodeText(text)
    text = tostring(text)
    local out = {}
    for index = 1, #text do
        local byte = string.byte(text, index)
        if byte < 32 or byte == 34 or byte == 92 then
            out[#out + 1] = escapeByte(byte)
        else
            out[#out + 1] = string.sub(text, index, index)
        end
    end
    return table.concat(out)
end

local function isArray(tbl)
    local count = 0
    for key in pairs(tbl) do
        if type(key) ~= 'number' then return false end
        count = count + 1
    end
    if count == 0 then return true end    -- empty table encodes as [], the common case
    local max = 0
    for key in pairs(tbl) do
        if key > max then max = key end
    end
    return max == count
end

local encodeValue

local function encodeTable(tbl, out, depth)
    if depth > 40 then
        error('Kestrel JSON: nesting too deep')
    end
    if isArray(tbl) then
        out[#out + 1] = '['
        for index = 1, #tbl do
            if index > 1 then out[#out + 1] = ',' end
            encodeValue(tbl[index], out, depth + 1)
        end
        out[#out + 1] = ']'
    else
        -- collect and sort keys so requests are stable and diffs are readable
        local keys = {}
        for key in pairs(tbl) do keys[#keys + 1] = tostring(key) end
        table.sort(keys)
        out[#out + 1] = '{'
        for index, key in ipairs(keys) do
            if index > 1 then out[#out + 1] = ',' end
            out[#out + 1] = '"'
            out[#out + 1] = encodeText(key)
            out[#out + 1] = '":'
            encodeValue(tbl[key], out, depth + 1)
        end
        out[#out + 1] = '}'
    end
end

encodeValue = function(value, out, depth)
    local kind = type(value)
    if value == nil then
        out[#out + 1] = 'null'
    elseif kind == 'boolean' then
        out[#out + 1] = value and 'true' or 'false'
    elseif kind == 'number' then
        if value ~= value or value == math.huge or value == -math.huge then
            out[#out + 1] = 'null'
        elseif value == math.floor(value) and math.abs(value) < 1e15 then
            out[#out + 1] = string.format('%d', value)
        else
            out[#out + 1] = string.format('%.15g', value)
        end
    elseif kind == 'string' then
        out[#out + 1] = '"'
        out[#out + 1] = encodeText(value)
        out[#out + 1] = '"'
    elseif kind == 'table' then
        encodeTable(value, out, depth)
    else
        error('Kestrel JSON: cannot encode a ' .. kind)
    end
end

function Json.encode(value)
    local out = {}
    encodeValue(value, out, 0)
    return table.concat(out)
end

-- --------------------------------------------------------------------------- decode
local function decodeError(text, position, message)
    error(string.format('%s at character %d near %q', message, position,
        text:sub(position, position + 12)), 0)
end

local function skipWhitespace(text, position)
    local _, stop = text:find('^[ \t\r\n]*', position)
    return (stop or position - 1) + 1
end

local function utf8Encode(code)
    if code < 0x80 then
        return string.char(code)
    elseif code < 0x800 then
        return string.char(0xC0 + math.floor(code / 0x40), 0x80 + (code % 0x40))
    elseif code < 0x10000 then
        return string.char(0xE0 + math.floor(code / 0x1000),
                           0x80 + (math.floor(code / 0x40) % 0x40),
                           0x80 + (code % 0x40))
    end
    return string.char(0xF0 + math.floor(code / 0x40000),
                       0x80 + (math.floor(code / 0x1000) % 0x40),
                       0x80 + (math.floor(code / 0x40) % 0x40),
                       0x80 + (code % 0x40))
end

local decodeValue

local function decodeString(text, position)
    -- position points at the opening quote
    local out = {}
    local index = position + 1
    while true do
        local char = text:sub(index, index)
        if char == '' then decodeError(text, index, 'unterminated string') end
        if char == '"' then
            return table.concat(out), index + 1
        elseif char == '\\' then
            local code = text:sub(index + 1, index + 1)
            if code == 'u' then
                local hex = text:sub(index + 2, index + 5)
                local value = tonumber(hex, 16)
                if not value then decodeError(text, index, 'bad unicode escape') end
                out[#out + 1] = utf8Encode(value)
                index = index + 6
            else
                local map = {
                    ['"'] = '"', ['\\'] = '\\', ['/'] = '/',
                    b = '\b', f = '\f', n = '\n', r = '\r', t = '\t',
                }
                local mapped = map[code]
                if not mapped then decodeError(text, index, 'bad escape') end
                out[#out + 1] = mapped
                index = index + 2
            end
        else
            out[#out + 1] = char
            index = index + 1
        end
    end
end

local function decodeNumber(text, position)
    local value = text:match('^-?%d+%.?%d*[eE]?[+-]?%d*', position)
    if not value or value == '' then decodeError(text, position, 'bad number') end
    local number = tonumber(value)
    if not number then decodeError(text, position, 'bad number') end
    return number, position + #value
end

decodeValue = function(text, position)
    position = skipWhitespace(text, position)
    local char = text:sub(position, position)
    if char == '' then decodeError(text, position, 'unexpected end of input') end
    if char == '{' then
        local object = {}
        position = skipWhitespace(text, position + 1)
        if text:sub(position, position) == '}' then return object, position + 1 end
        while true do
            position = skipWhitespace(text, position)
            if text:sub(position, position) ~= '"' then
                decodeError(text, position, 'expected a key')
            end
            local key
            key, position = decodeString(text, position)
            position = skipWhitespace(text, position)
            if text:sub(position, position) ~= ':' then
                decodeError(text, position, 'expected a colon')
            end
            object[key], position = decodeValue(text, position + 1)
            position = skipWhitespace(text, position)
            local nextChar = text:sub(position, position)
            if nextChar == '}' then return object, position + 1 end
            if nextChar ~= ',' then decodeError(text, position, 'expected a comma') end
            position = position + 1
        end
    elseif char == '[' then
        local array = {}
        local index = 1
        position = skipWhitespace(text, position + 1)
        if text:sub(position, position) == ']' then return array, position + 1 end
        while true do
            array[index], position = decodeValue(text, position)
            index = index + 1
            position = skipWhitespace(text, position)
            local nextChar = text:sub(position, position)
            if nextChar == ']' then return array, position + 1 end
            if nextChar ~= ',' then decodeError(text, position, 'expected a comma') end
            position = position + 1
        end
    elseif char == '"' then
        return decodeString(text, position)
    elseif char == 't' then
        if text:sub(position, position + 3) == 'true' then return true, position + 4 end
        decodeError(text, position, 'bad literal')
    elseif char == 'f' then
        if text:sub(position, position + 4) == 'false' then return false, position + 5 end
        decodeError(text, position, 'bad literal')
    elseif char == 'n' then
        if text:sub(position, position + 3) == 'null' then return nil, position + 4 end
        decodeError(text, position, 'bad literal')
    else
        return decodeNumber(text, position)
    end
end

function Json.decode(text)
    if type(text) ~= 'string' then
        error('Kestrel JSON: decode expects a string', 0)
    end
    local value, position = decodeValue(text, 1)
    position = skipWhitespace(text, position)
    if position <= #text then
        decodeError(text, position, 'trailing content')
    end
    return value
end

return Json
