@echo off
REM Quick deploy: copy hair integration to HA and restart.
REM Usage: deploy.bat 192.168.1.100
if "%1"=="" echo Usage: deploy.bat ^<ha_ip^> && exit /b 1
set HA_HOST=%1
echo Deploying to %HA_HOST%...
scp -i e:\work\HA\ssh\ha -r custom_components\hair root@%HA_HOST%:/config/custom_components/
echo Restarting HA...
ssh -i e:\work\HA\ssh\ha root@%HA_HOST% "ha core restart"
echo Done.
