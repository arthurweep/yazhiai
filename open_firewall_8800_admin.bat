@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 请右键本文件，选择“以管理员身份运行”。
  pause
  exit /b 1
)
netsh advfirewall firewall delete rule name="PressFurnace TCP 8800" >nul 2>&1
netsh advfirewall firewall add rule name="PressFurnace TCP 8800" dir=in action=allow protocol=TCP localport=8800
echo.
echo 已开放 Windows 防火墙 TCP 8800 入站端口。
echo 其它电脑请访问 http://本机IPv4地址:8800/
pause
