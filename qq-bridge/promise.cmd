@echo off
rem Promise ledger CLI wrapper. Use this from the chat agent: it waits for the exit.
rem ASCII only: cmd parses this file as GBK.
setlocal
set "ELECTRON_RUN_AS_NODE=1"
set "EXE=C:\Users\USER\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe"
"%EXE%" "%~dp0promise.mjs" %*
