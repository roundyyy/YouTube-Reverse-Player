@echo off
echo This script will install required Python libraries and run the tool.

REM --- Check if Python is installed ---
python --version 1>nul 2>nul
IF ERRORLEVEL 1 (
    echo Python not found. Please install Python 3.9 or higher from https://www.python.org/downloads/
    pause
    goto end
)

REM --- Check if pip is available ---
pip --version 1>nul 2>nul
IF ERRORLEVEL 1 (
    echo pip not found. Please make sure pip is in your PATH or installed with your Python.
    pause
    goto end
)

REM --- Install Python requirements ---
echo Installing Python requirements from requirements.txt...
pip install -r requirements.txt
IF ERRORLEVEL 1 (
    echo Failed to install Python requirements. 
    pause
    goto end
)

REM --- (Optional) Check if ffmpeg is installed ---
ffmpeg -version 1>nul 2>nul
IF ERRORLEVEL 1 (
    echo WARNING: ffmpeg is not found in PATH.
    echo This tool uses ffmpeg for reversing/encoding. 
    echo Please install ffmpeg or place ffmpeg.exe in this folder. 
    pause
)


echo.
echo Launching the YouTube Reverse Player...
echo ---------------------------------------
python youtube_reverse_player.py

:end
pause
