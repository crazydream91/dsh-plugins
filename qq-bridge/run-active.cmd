@echo off
rem Proactive speaker + reminder daemon. ASCII only: cmd reads this as GBK.
setlocal
set "ELECTRON_RUN_AS_NODE=1"
set "EXE=C:\Users\USER\AppData\Local\Programs\DeepSeek Harness\DeepSeek Harness.exe"
"%EXE%" "%~dp0active.mjs" %*
