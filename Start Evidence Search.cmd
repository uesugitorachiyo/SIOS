@echo off
setlocal
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo Setup is required. See docs\EVIDENCE_SEARCH.md in this folder.
  pause
  exit /b 1
)
set "PYTHONPATH=%~dp0src"
"%~dp0.venv\Scripts\python.exe" -m aios.evidence_search_web --registered sios-nucx --open
if errorlevel 1 (
  echo Evidence Search could not start. See docs\EVIDENCE_SEARCH.md.
  pause
  exit /b 1
)
