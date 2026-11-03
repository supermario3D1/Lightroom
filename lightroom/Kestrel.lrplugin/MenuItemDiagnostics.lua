--[[ Kestrel — Self-test the plug-in and engine. Menu handlers run on the main thread; the slow work
     starts a background task with its own progress bar. ]]

KestrelRequire('KestrelMenus').diagnostics()
