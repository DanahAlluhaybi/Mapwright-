@echo off
rem Starts Mapwright and opens its page in the browser. Close this window to stop it.
cd /d "%~dp0"
call .venv\Scripts\activate.bat
python -m mapwright serve --open --reference customer_id=benchmark/reference_customers.csv
pause
