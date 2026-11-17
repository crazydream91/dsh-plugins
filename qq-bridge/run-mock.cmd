@echo off
rem Start the fake NapCat (no QQ account needed) to test the bridge locally.
setlocal
set "ELECTRON_RUN_AS_NODE=1"
set "EXE=C:\Users\USER\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe"
"%EXE%" "%~dp0mock-napcat.mjs" %*
