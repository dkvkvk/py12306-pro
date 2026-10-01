@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   py12306 抢票助手 - 安装运行环境
echo ============================================
echo.
where python >nul 2>nul
if errorlevel 1 (
  echo [错误] 没有找到 python，请先安装 Python 3.11 或更高版本，
  echo        安装时记得勾选 "Add Python to PATH"。
  echo        下载地址: https://www.python.org/downloads/
  pause
  exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
  echo [错误] Python 版本过低，需要 3.11 及以上。
  python -V
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo [1/2] 创建虚拟环境 .venv ...
  python -m venv .venv || (echo [错误] 创建虚拟环境失败 & pause & exit /b 1)
)
echo [2/2] 安装依赖（首次可能需要几分钟）...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo [错误] 依赖安装失败。如果是网络问题，可以改用国内源重试：
  echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
  pause
  exit /b 1
)
echo.
echo 环境安装完成！接下来：
echo   1. 复制 env.example 为 .env 并填写账号与任务（文件位置见下）
echo   2. 双击「启动py12306.bat」开始
echo.
".venv\Scripts\python.exe" -c "import sys; sys.path.insert(0,'.'); from core import paths; print('数据目录:', paths.data_root()); print('请把 .env 放在数据目录下（或项目根目录）')"
pause
