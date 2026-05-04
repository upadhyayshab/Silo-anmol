@echo off
REM Activate the virtual environment
call .venv\scripts\activate.bat

REM Set the PYTHONPATH environment variable
set PYTHONPATH=%PYTHONPATH%;%CD%\SharedBackend\src;%CD%\app

REM Run the main Python application
python app/app.py

REM --- END ---