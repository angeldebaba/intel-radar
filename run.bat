@echo off
chcp 65001 >nul
cd /d %~dp0
echo ========================================
echo   行业情报雷达 - 本地启动
echo   访问地址: http://127.0.0.1:5000
echo   后台入口: http://127.0.0.1:5000/#admin
echo ========================================
"C:\Users\李悦锋\.workbuddy\binaries\python\envs\default\Scripts\python.exe" app.py
pause
