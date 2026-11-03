--[[ Kestrel — Open the selection in the web workspace. Menu handlers run on the main thread; the slow work
     starts a background task with its own progress bar. ]]

KestrelRequire('KestrelMenus').sendToWorkspace()
