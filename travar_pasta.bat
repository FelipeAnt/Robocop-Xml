@echo off
setlocal enabledelayedexpansion

:: ==================================================================
:: travar_pasta.bat
:: Restringe o acesso desta pasta a SYSTEM e Administradores.
:: OPCIONAL - so rode depois de confirmar que o robocop_clone.py e o
:: servico ja estao funcionando normalmente. Se qualquer etapa falhar,
:: este script desfaz a alteracao automaticamente em vez de deixar a
:: pasta travada pra todo mundo.
:: ==================================================================

set "SCRIPT_DIR=%~dp0"
set "SCRIPT_DIR_NOSLASH=%SCRIPT_DIR:~0,-1%"

echo ===============================================
echo  Travar pasta - Robocop Clone
echo ===============================================
echo.
echo Isso restringe o acesso a esta pasta somente a SYSTEM e Administradores.
echo Recomendado SOMENTE depois de confirmar que tudo ja esta funcionando.
echo.
set /p CONFIRMA=Tem certeza que quer continuar? (S/N): 
if /I not "%CONFIRMA%"=="S" (
    echo Cancelado, nada foi alterado.
    pause
    exit /b 0
)

net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [ERRO] Execute este script como Administrador.
    pause
    exit /b 1
)

echo.
echo Removendo heranca de permissoes...
icacls "%SCRIPT_DIR_NOSLASH%" /inheritance:r >nul
if %errorLevel% neq 0 (
    echo [ERRO] Falha ao remover heranca. Nada foi alterado, pasta continua normal.
    pause
    exit /b 1
)

echo Concedendo acesso total a SYSTEM...
icacls "%SCRIPT_DIR_NOSLASH%" /grant:r "SYSTEM:(OI)(CI)F" >nul
if %errorLevel% neq 0 (
    echo [ERRO] Falha ao conceder acesso a SYSTEM.
    echo Revertendo para heranca padrao, pra nao deixar a pasta trancada...
    icacls "%SCRIPT_DIR_NOSLASH%" /inheritance:e >nul
    echo Revertido com sucesso. A pasta NAO foi travada.
    pause
    exit /b 1
)

echo Concedendo acesso total ao grupo Administradores...
icacls "%SCRIPT_DIR_NOSLASH%" /grant:r "*S-1-5-32-544:(OI)(CI)F" >nul
if %errorLevel% neq 0 (
    echo [ERRO] Falha ao conceder acesso a Administradores.
    echo Revertendo para heranca padrao, pra nao deixar a pasta trancada...
    icacls "%SCRIPT_DIR_NOSLASH%" /inheritance:e >nul
    echo Revertido com sucesso. A pasta NAO foi travada.
    pause
    exit /b 1
)

echo.
echo ===============================================
echo  Pasta travada com sucesso.
echo  Somente SYSTEM e o grupo Administradores tem acesso agora.
echo.
echo  Se precisar desfazer no futuro, rode como Administrador:
echo    icacls "%SCRIPT_DIR_NOSLASH%" /reset /t
echo ===============================================
pause
