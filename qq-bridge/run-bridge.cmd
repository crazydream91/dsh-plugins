@echo off
rem Start the QQ <-> DSH bridge using the Node runtime bundled with DSH.
setlocal
set "ELECTRON_RUN_AS_NODE=1"
set "EXE=C:\Users\USER\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe"
"%EXE%" "%~dp0bridge.mjs" %*
