@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: desinstalar_servico.bat
:: Para e remove o servico RobocopClone instalado via NSSM.
:: ==================================================================

set "SERVICE_NAME=RobocopClone"
set "SCRIPT_DIR=%~dp0"
set "NSSM_DIR=%SCRIPT_DIR%nssm"

if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    set "ARCH=win64"
) else if defined PROCESSOR_ARCHITEW6432 (
    set "ARCH=win64"
) else (
    set "ARCH=win32"
)
set "NSSM_EXE=%NSSM_DIR%\%ARCH%\nssm.exe"

echo ===============================================
echo  Desinstalador de servico - Robocop Clone
echo ===============================================
echo.

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Execute como Administrador.
    pause
    exit /b 1
)

if not exist "%NSSM_EXE%" (
    echo [ERRO] nssm.exe nao encontrado em %NSSM_EXE%
    echo Se o servico foi instalado de outra forma, remova manualmente pelo services.msc.
    pause
    exit /b 1
)

"%NSSM_EXE%" status %SERVICE_NAME% >nul 2>&1
if %errorLevel% neq 0 (
    echo O servico "%SERVICE_NAME%" nao esta instalado. Nada a fazer.
    pause
    exit /b 0
)

echo Parando o servico...
"%NSSM_EXE%" stop %SERVICE_NAME%

echo Removendo o servico...
"%NSSM_EXE%" remove %SERVICE_NAME% confirm

echo.
echo Servico removido. Os arquivos (config.json, token.json, logs, banco local)
echo continuam na pasta do projeto, nada foi apagado.
echo ===============================================
pause
