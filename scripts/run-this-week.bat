@echo off

:: Navigate to the src directory
cd /d "%~dp0..\src"

echo Generating Interactive SPP-Ready Export and Opening It in the Browser
echo.

:: Run the script
python spp_export.py this-week --open

:: "Pause" keeps the window open so you can see if it succeeded
:: pause