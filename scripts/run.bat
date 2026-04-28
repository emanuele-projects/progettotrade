@echo off
cd /d "%~dp0\.."
if not exist .venv (
    echo Creating Python virtual environment...
    python -m venv .venv
    call .venv\Scripts\activate
    echo Installing dependencies...
    pip install -r requirements.txt
) else (
    call .venv\Scripts\activate
)
python main.py %*
