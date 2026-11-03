--[[ Kestrel — Edit the selected photographs. Menu handlers run on the main thread; the slow work
     starts a background task with its own progress bar. ]]

KestrelRequire('KestrelMenus').editSelection()
