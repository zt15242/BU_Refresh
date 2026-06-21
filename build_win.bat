@echo off
echo ===================================================
echo   BU省刷新工具 - Windows 打包脚本
echo ===================================================
echo.

:: 检查 Python 是否安装
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Python，请先安装 Python 并添加到系统环境变量 PATH 中。
    pause
    exit /b 1
)

echo [1/3] 正在安装/更新必要依赖...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pywebview pyinstaller

echo [2/3] 正在使用 PyInstaller 进行打包...
pyinstaller BU省刷新工具.spec --clean

if %errorlevel% neq 0 (
    echo [错误] 打包失败，请检查上方报错信息。
    pause
    exit /b 1
)

echo.
echo ===================================================
echo [成功] 打包完成！
echo 生成的执行文件位于: dist\BU省刷新工具.exe
echo ===================================================
pause
