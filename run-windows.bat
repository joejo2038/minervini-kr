@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 미너비니 KR 스크리너

echo.
echo  ===========================================
echo   미너비니 KR 스크리너
echo  ===========================================
echo.

REM --- 1. 파이썬 확인 ---
where python >nul 2>&1
if errorlevel 1 (
  echo  [x] 파이썬이 설치되어 있지 않습니다.
  echo.
  echo      https://www.python.org/downloads/ 에서 설치하세요.
  echo      설치 첫 화면 맨 아래 "Add python.exe to PATH" 를 반드시 체크해야 합니다.
  echo      설치 후 이 창을 닫고 다시 실행하세요.
  echo.
  pause
  exit /b 1
)

for /f "tokens=*" %%v in ('python --version 2^>^&1') do echo  [v] %%v

REM --- 2. 가상환경 준비 ---
REM 시스템 파이썬을 건드리지 않도록 프로젝트 전용 환경을 만듭니다.
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo  프로젝트 전용 파이썬 환경을 만듭니다.
  python -m venv .venv
  if errorlevel 1 (
    echo  [!] 가상환경 생성 실패. 시스템 파이썬을 그대로 씁니다.
    set "PY=python"
  )
)
if not exist "%PY%" set "PY=python"

REM --- 3. 최초 1회 패키지 설치 ---
if not exist ".installed" (
  echo  필요한 패키지를 설치합니다. 2~5분 걸립니다.
  echo.
  "%PY%" -m pip install --upgrade pip >nul 2>&1
  "%PY%" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  [x] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요.
    echo      인터넷 연결이나 사내 방화벽 문제일 수 있습니다.
    echo.
    pause
    exit /b 1
  )
  echo. > .installed
  echo.
  echo  [v] 준비 완료
)

:menu
echo.
echo  ------------------------------------------
echo   무엇을 하시겠습니까?
echo  ------------------------------------------
echo    1  데모 실행        가짜 데이터로 동작 확인 (30초)
echo    2  최초 데이터 적재  2년치. 20~40분. 처음 딱 한 번
echo    3  오늘 스크리닝    매일 이걸 누르세요 (2~5분)
echo    4  결과 보기        웹 화면 띄우기
echo    5  상태 확인        지금 뭐가 적재됐는지 점검
echo    6  백테스트        과거 데이터로 전략 검증 (1~3분)
echo    7  파라미터 탐색    최적 설정 찾기 (10~30분)
echo    8  종료
echo.
set /p choice="  번호 입력: "
echo.

if "%choice%"=="1" goto demo
if "%choice%"=="2" goto backfill
if "%choice%"=="3" goto daily
if "%choice%"=="4" goto serve
if "%choice%"=="5" goto status
if "%choice%"=="6" goto backtest
if "%choice%"=="7" goto grid
if "%choice%"=="8" exit /b 0
echo  1~8 중에서 골라주세요.
goto menu

:demo
"%PY%" run_screen.py --demo
echo.
pause
goto menu

:backfill
echo  전 종목 3년치를 받습니다. 창을 닫지 마세요.
echo.
"%PY%" run_screen.py --backfill
echo.
pause
goto menu

:daily
"%PY%" run_screen.py
echo.
pause
goto menu

:status
"%PY%" run_screen.py --status
pause
goto menu

:backtest
"%PY%" run_screen.py --backtest
pause
goto menu

:grid
echo  조합마다 전체 기간을 다시 돌립니다. 시간이 걸립니다.
echo.
"%PY%" run_screen.py --grid
pause
goto menu

:serve
echo  브라우저에서 http://localhost:8000 을 엽니다.
echo  끝내려면 이 창에서 Ctrl+C 를 누르세요.
echo.
set "PYABS=%CD%\%PY%"
if "%PY%"=="python" set "PYABS=python"
start "" http://localhost:8000
cd docs
"%PYABS%" -m http.server 8000
cd ..
goto menu
