@echo off
rem Elevated launcher: the game runs as admin, so the script must too (UIPI).
rem Usage:  run_admin.bat            (整套每日;今天已完成的自動跳過)
rem         run_admin.bat 遠航       (只跑單一任務)
rem         run_admin.bat 兌換校準   (互動式記錄兌換座標,不會點兌換)
powershell -NoProfile -Command "Start-Process -Verb RunAs cmd -ArgumentList '/k chcp 65001 >nul && cd /d %~dp0 && .venv\Scripts\python.exe -m agent.pilot %*'"
