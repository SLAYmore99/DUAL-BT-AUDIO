@echo off
cd /d "%~dp0"
echo Checking packages...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
echo Starting Dual Speaker Mixer...
python dual_speaker.py
if errorlevel 1 pause
