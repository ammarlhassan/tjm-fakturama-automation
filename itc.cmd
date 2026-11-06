@echo off
setlocal
set "PYTHONUTF8=1"
set "ITC_PYTHON=%LOCALAPPDATA%\TJM-ImageToCash\environment\Scripts\python.exe"
if not exist "%ITC_PYTHON%" (
  echo Local environment not found. Follow the setup instructions in README.md.
  exit /b 1
)
"%ITC_PYTHON%" -m image_to_cash.cli %*

