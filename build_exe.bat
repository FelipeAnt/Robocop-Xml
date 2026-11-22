@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: build_exe.bat
:: Empacota robocop_clone.py e configurador.py em executaveis standalone
:: usando PyInstaller, e monta uma pasta "pacote_cliente" pronta pra
:: copiar direto pra maquina de um cliente novo.
::
:: Rode isso na SUA maquina de desenvolvimento (precisa ter Python +
:: as dependencias instaladas) - numa pasta "limpa", so com os arquivos
:: fonte. NAO rode dentro da pasta de producao de um cliente que ja
:: esta rodando (tem config.json, banco de dados e logs proprios dele,
:: que nao devem ir pro pacote de outro cliente).
:: ==================================================================

echo ===============================================
echo  Build - Robocop Clone (.exe)
echo ===============================================
echo.

where python >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Python nao encontrado. Rode este build numa maquina com
    echo Python instalado ^(so aqui no build, nao precisa no cliente^).
    pause
    exit /b 1
)

:: --- Confere se os modulos locais estao presentes antes de comecar ---
:: robocop_clone.py importa db_manager, fiscal_parser, stability e segredo. O
:: PyInstaller detecta e empacota esses modulos sozinho (sao so biblioteca
:: padrao por dentro), mas so se os .py estiverem fisicamente aqui do lado.
set "FALTANDO="
for %%F in (robocop_clone.py configurador.py db_manager.py fiscal_parser.py stability.py segredo.py r2_teste.py requirements.txt) do (
    if not exist "%%F" set "FALTANDO=!FALTANDO! %%F"
)
if defined FALTANDO (
    echo [ERRO] Arquivo^(s^) faltando nesta pasta: %FALTANDO%
    echo Todos os modulos ^(robocop_clone.py, configurador.py, db_manager.py,
    echo fiscal_parser.py, stability.py, segredo.py, r2_teste.py^) e o requirements.txt precisam estar
    echo juntos, na mesma pasta, antes de rodar o build.
    pause
    exit /b 1
)

echo Instalando dependencias de build...
pip install --upgrade pyinstaller >nul
pip install --upgrade -r requirements.txt >nul

echo.
echo Empacotando robocop_clone.py...
:: --collect-all boto3/botocore: o boto3 carrega arquivos de dados
:: (definicoes de API em JSON) em tempo de execucao, que o PyInstaller
:: nao detecta sozinho por analise estatica do codigo. Sem isso, o
:: .exe roda mas quebra na hora de falar com o R2.
:: --contents-directory: da um nome unico pra pasta interna de cada
:: exe (em vez do padrao "_internal"), pra poder juntar os dois exes
:: na mesma pasta final sem um sobrescrever os arquivos do outro.
python -m PyInstaller --onedir --console --noconfirm --name robocop_clone ^
    --contents-directory robocop_clone_libs ^
    --collect-all boto3 --collect-all botocore --collect-all watchdog ^
    robocop_clone.py

echo.
echo Empacotando configurador.py...
python -m PyInstaller --onedir --windowed --noconfirm --name configurador ^
    --contents-directory configurador_libs ^
    configurador.py

echo.
echo Montando pacote pronto para o cliente...
if exist pacote_cliente rmdir /s /q pacote_cliente
mkdir pacote_cliente

xcopy /E /I /Y dist\robocop_clone\* pacote_cliente\ >nul
xcopy /E /I /Y dist\configurador\* pacote_cliente\ >nul

for %%F in (instalar_servico.bat desinstalar_servico.bat) do (
    if exist "%%F" (
        copy /Y "%%F" pacote_cliente\ >nul
    ) else (
        echo [AVISO] %%F nao encontrado nesta pasta - nao foi incluido no pacote.
    )
)

echo.
echo ===============================================
echo  Build concluido.
echo.
echo  Pacote pronto para o cliente em: pacote_cliente\
echo    - robocop_clone.exe + configurador.exe
echo    - instalar_servico.bat / desinstalar_servico.bat
echo.
echo  Copie a pasta "pacote_cliente" inteira para a maquina do cliente,
echo  rode configurador.exe la para gerar o config.json daquele cliente,
echo  teste robocop_clone.exe manualmente, e so depois instalar_servico.bat.
echo.
echo  Nao inclua config.json, controle_upload.sqlite3 nem os .log de um
echo  cliente no pacote de outro - cada instalacao gera os seus proprios.
echo ===============================================
pause
