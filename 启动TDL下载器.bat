@echo off
chcp 65001 >nul
cd /d "%~dp0"
REM 查找 pythonw.exe（无控制台窗口），找不到则用 python.exe
set "PYEXE="
for %%P in (pythonw.exe) do if exist "%%~$PATH:P" set "PYEXE=%%~$PATH:P"
if not defined PYEXE (
  for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do set "PYEXE=%%I"
)
if not defined PYEXE (
  if exist "%LOCALAPPDATA%\Programs\Python\Python315\pythonw.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python315\pythonw.exe"
)
if not defined PYEXE (
  echo 未找到 pythonw.exe，请确认 Python 已安装
  pause
  exit /b 1
)
start "" "%PYEXE%" "%~dp0tdl_gui.pyw"
exit
