@echo off
cd /d "%~dp0"
set ELECTRON_RUN_AS_NODE=1
start "" /b "C:\Users\USER\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe" "%~dp0kick-watch.mjs"