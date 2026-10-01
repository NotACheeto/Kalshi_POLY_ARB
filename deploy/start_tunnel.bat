@echo off
REM ====================================================================
REM  Windows SSH Tunnel Helper for Kalshi-Poly Arbitrage Dashboard
REM ====================================================================

set /p KEY_PATH="Enter path to your AWS .pem key (e.g. C:\keys\my-ec2-key.pem): "
set /p EC2_HOST="Enter your EC2 Public IP or DNS (e.g. 54.123.45.67): "

echo.
echo Opening secure encrypted tunnel to AWS EC2...
echo Forwarding remote port 8000 -> http://localhost:8000
echo.
echo Leave this command window open while viewing the dashboard.
echo Press Ctrl+C to close the tunnel.
echo.

start "" "http://localhost:8000"
ssh -i "%KEY_PATH%" -N -L 8000:localhost:8000 ubuntu@%EC2_HOST%
