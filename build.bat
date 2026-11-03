@echo off
cd /d "%~dp0"
echo === Movie List Generator - Build ===

python -m pip install --upgrade pyinstaller || goto :error

echo.
echo Building movie_list_generator.exe (single exe: server + VLC handler, no console) ...
python -m PyInstaller --onefile --noconsole --name movie_list_generator movie_list_generator.py || goto :error

echo.
echo Creating release folder ...
if not exist release mkdir release
copy /Y dist\movie_list_generator.exe release\ >nul
if exist library.json copy /Y library.json release\ >nul
if exist posters xcopy /E /I /Y posters release\posters >nul
rem config.json (API keys) is NOT copied. See config.example.json
if exist config.example.json copy /Y config.example.json release\config.example.json >nul

echo.
echo DONE. Files are in the release folder.
pause
exit /b 0

:error
echo.
echo BUILD ERROR.
pause
exit /b 1
