@echo off
REM RingCentral Archiver - run manually or via Windows Task Scheduler
cd /d "%~dp0"
python archiver.py %*
