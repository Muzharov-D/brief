@echo off
rem Install Ogonyok on Windows: double-click this file.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 pause
