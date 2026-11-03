--[[ Kestrel — Copy the first photo's look to the selection. Menu handlers run on the main thread; the slow work
     starts a background task with its own progress bar. ]]

KestrelRequire('KestrelMenus').syncSelection()
