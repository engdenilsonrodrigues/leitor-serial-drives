@echo off
chcp 65001 >nul
cd /d "%~dp0"
where git >nul 2>nul || (echo Git nao encontrado. Instale em https://git-scm.com e rode de novo. & pause & exit /b 1)
if not exist .git git init
git config user.name >nul 2>nul || git config user.name "engdenilsonrodrigues"
git config user.email >nul 2>nul || git config user.email "eng.denilsonrodrigues@gmail.com"
git add .
git commit -m "Leitor serial de drives: app web + Python + perfis" 
git branch -M main
git remote get-url origin >nul 2>nul || git remote add origin https://github.com/engdenilsonrodrigues/leitor-serial-drives.git
git push -u origin main
echo.
if %errorlevel%==0 (echo PRONTO: projeto enviado para github.com/engdenilsonrodrigues/leitor-serial-drives) else (echo Falhou o envio - veja a mensagem acima.)
pause
