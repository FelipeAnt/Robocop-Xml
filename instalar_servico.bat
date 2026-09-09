@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: instalar_servico.bat
:: Instala o robocop_clone.py como servico do Windows, usando o NSSM.
:: Baixa o NSSM automaticamente se ele ainda nao estiver presente.
:: ==================================================================

set "SERVICE_NAME=RobocopClone"
set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR_NOSLASH=%SCRIPT_DIR:~0,-1%"
set "NSSM_DIR=%SCRIPT_DIR%nssm"
set "NSSM_ZIP_URL=https://nssm.cc/release/nssm-2.24.zip"
set "NSSM_ZIP=%SCRIPT_DIR%nssm.zip"

if /I "%PROCESSOR_ARCHITECTURE%"=="AMD64" (
    set "ARCH=win64"
) else if defined PROCESSOR_ARCHITEW6432 (
    set "ARCH=win64"
) else (
    set "ARCH=win32"
)
set "NSSM_EXE=%NSSM_DIR%\%ARCH%\nssm.exe"

echo ===============================================
echo  Instalador de servico - Robocop Clone
echo ===============================================
echo.

:: --- Nao permite instalar dentro de Program Files (protegido p/ escrita pelo Windows) ---
echo %SCRIPT_DIR% | findstr /I "Program Files" >nul
if %errorLevel% equ 0 (
    echo [ERRO] Esta pasta esta dentro de "Program Files" ou "Program Files ^(x86^)".
    echo O Windows bloqueia escrita nesses locais para usuarios comuns, e este projeto
    echo precisa escrever config.json, logs e banco de dados em tempo real.
    echo.
    echo Mova a pasta inteira do projeto para um local como C:\RobocopXML\ e rode
    echo este instalador novamente a partir de la.
    pause
    exit /b 1
)

:: --- Precisa ser Administrador ---
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Este script precisa ser executado como Administrador.
    echo Clique com o botao direito neste arquivo e escolha "Executar como administrador".
    pause
    exit /b 1
)

:: --- Se existir um .exe empacotado (PyInstaller), usa ele direto -----
:: Isso elimina a necessidade de Python instalado na maquina do cliente.
if exist "%SCRIPT_DIR%robocop_clone.exe" (
    set "USANDO_EXE=1"
    echo Encontrado robocop_clone.exe empacotado - nao precisa de Python instalado.
    goto :python_ok
)

:: --- Sem .exe: localiza o python.exe (fluxo de desenvolvimento/teste) ---
where python >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Python nao encontrado no PATH, e nao ha robocop_clone.exe empacotado.
    echo Instale o Python ^(python.org^) marcando "Add to PATH" durante a instalacao,
    echo ou empacote o projeto com PyInstaller antes de instalar nesta maquina.
    pause
    exit /b 1
)
for /f "delims=" %%P in ('where python') do (
    echo %%P | findstr /I "WindowsApps" >nul
    if errorlevel 1 (
        if not defined PYTHON_EXE set "PYTHON_EXE=%%P"
    )
)
if not defined PYTHON_EXE (
    echo [ERRO] So encontrei o "python.exe" de atalho da Microsoft Store ^(App Execution Alias^),
    echo que nao executa o Python de verdade quando chamado por um servico.
    echo.
    echo Instale o Python de verdade em https://www.python.org/downloads/ marcando
    echo "Add to PATH" durante a instalacao, ou empacote o projeto com PyInstaller.
    pause
    exit /b 1
)
echo Python encontrado em: %PYTHON_EXE%

:python_ok
echo Pasta do projeto: %SCRIPT_DIR%
echo.

:: --- Pre-requisito: config.json precisa existir ---
if not exist "%SCRIPT_DIR%config.json" (
    echo [ERRO] config.json nao encontrado nesta pasta.
    echo Rode "python configurador.py" primeiro para criar a configuracao
    echo ^(pastas monitoradas e credenciais do Cloudflare R2^).
    pause
    exit /b 1
)

:: --- Baixa e extrai o NSSM se necessario ---
if not exist "%NSSM_EXE%" (
    echo NSSM nao encontrado, baixando...
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri '%NSSM_ZIP_URL%' -OutFile '%NSSM_ZIP%' -UseBasicParsing } catch { exit 1 }"
    if not exist "%NSSM_ZIP%" (
        echo [ERRO] Falha ao baixar o NSSM automaticamente.
        echo Baixe manualmente em https://nssm.cc/download, extraia o conteudo,
        echo e copie as pastas win32/win64 para: %NSSM_DIR%
        pause
        exit /b 1
    )

    echo Extraindo NSSM...
    powershell -NoProfile -Command "Expand-Archive -Path '%NSSM_ZIP%' -DestinationPath '%SCRIPT_DIR%nssm_tmp' -Force"

    for /d %%D in ("%SCRIPT_DIR%nssm_tmp\nssm-*") do set "NSSM_SRC=%%D"
    if not defined NSSM_SRC (
        echo [ERRO] Nao encontrei a pasta extraida do NSSM. Verifique %SCRIPT_DIR%nssm_tmp
        pause
        exit /b 1
    )

    if not exist "%NSSM_DIR%" mkdir "%NSSM_DIR%"
    xcopy /E /I /Y "!NSSM_SRC!\win64" "%NSSM_DIR%\win64" >nul
    xcopy /E /I /Y "!NSSM_SRC!\win32" "%NSSM_DIR%\win32" >nul

    rmdir /S /Q "%SCRIPT_DIR%nssm_tmp"
    del /Q "%NSSM_ZIP%"
    echo NSSM pronto em %NSSM_DIR%
) else (
    echo NSSM ja presente, pulando download.
)
echo.

if not exist "%NSSM_EXE%" (
    echo [ERRO] %NSSM_EXE% nao existe apos a extracao. Instalacao abortada.
    pause
    exit /b 1
)

:: --- Se o servico ja existe, remove para reinstalar limpo ---
"%NSSM_EXE%" status %SERVICE_NAME% >nul 2>&1
if %errorLevel% equ 0 (
    echo Servico "%SERVICE_NAME%" ja existe. Parando e removendo para reinstalar...
    "%NSSM_EXE%" stop %SERVICE_NAME% >nul 2>&1
    "%NSSM_EXE%" remove %SERVICE_NAME% confirm >nul 2>&1
    timeout /t 2 >nul
)

:: --- Instala o servico ---
echo Instalando o servico "%SERVICE_NAME%"...
if defined USANDO_EXE (
    "%NSSM_EXE%" install %SERVICE_NAME% "%SCRIPT_DIR%robocop_clone.exe"
) else (
    "%NSSM_EXE%" install %SERVICE_NAME% "%PYTHON_EXE%" "\"%SCRIPT_DIR%robocop_clone.py\""
)
"%NSSM_EXE%" set %SERVICE_NAME% AppDirectory "%SCRIPT_DIR_NOSLASH%"
"%NSSM_EXE%" set %SERVICE_NAME% DisplayName "Robocop Clone - Upload automatico de XML"
"%NSSM_EXE%" set %SERVICE_NAME% Description "Monitora pastas de XML fiscal (NFC-e/NF-e) e envia para o Cloudflare R2 automaticamente."
"%NSSM_EXE%" set %SERVICE_NAME% Start SERVICE_AUTO_START
"%NSSM_EXE%" set %SERVICE_NAME% AppStdout "%SCRIPT_DIR%servico_stdout.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppStderr "%SCRIPT_DIR%servico_stderr.log"
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateFiles 1
"%NSSM_EXE%" set %SERVICE_NAME% AppRotateBytes 10485760
"%NSSM_EXE%" set %SERVICE_NAME% AppExit Default Restart
"%NSSM_EXE%" set %SERVICE_NAME% AppRestartDelay 5000

echo.
echo Iniciando o servico...
"%NSSM_EXE%" start %SERVICE_NAME%

echo.
echo ===============================================
echo  Servico "%SERVICE_NAME%" instalado e iniciado.
echo.
echo  Logs da aplicacao (uploads, erros):
echo    %SCRIPT_DIR%robocop_clone.log
echo  Logs do processo (stdout/stderr do servico):
echo    %SCRIPT_DIR%servico_stdout.log
echo    %SCRIPT_DIR%servico_stderr.log
echo.
echo  Gerenciar: abra "services.msc" e procure por "%SERVICE_NAME%",
echo  ou use os comandos do NSSM em: %NSSM_EXE%
echo.
echo  Dica de seguranca opcional: depois de confirmar que tudo esta
echo  funcionando, voce pode rodar travar_pasta.bat para restringir o
echo  acesso a esta pasta so a Administradores. Nao e obrigatorio.
echo ===============================================
pause
