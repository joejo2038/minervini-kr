#!/bin/bash
cd "$(dirname "$0")" || exit 1

echo
echo " ==========================================="
echo "  미너비니 KR 스크리너"
echo " ==========================================="
echo

pause_return() { echo; read -r -p " 엔터를 누르면 메뉴로 돌아갑니다." _; }

# --- 1. 시스템 파이썬 찾기 ---
if command -v python3 >/dev/null 2>&1; then
  SYSPY=python3
elif command -v python >/dev/null 2>&1; then
  SYSPY=python
else
  echo " [x] 파이썬이 설치되어 있지 않습니다."
  echo
  echo "     맥이면 터미널에 아래를 붙여넣으세요."
  echo "       brew install python3"
  echo
  echo "     홈브류가 없으면 https://www.python.org/downloads/ 에서 받으세요."
  echo
  read -r -p " 엔터를 누르면 닫힙니다." _
  exit 1
fi

echo " [v] $($SYSPY --version)"

# --- 2. 가상환경 준비 ---
# 시스템 파이썬에 직접 설치하면 최근 버전에서 externally-managed-environment
# 오류가 납니다. 프로젝트 전용 환경을 만들어 그 안에만 설치합니다.
VENV=".venv"
PIPFLAGS=""

if [ ! -x "$VENV/bin/python" ]; then
  echo " 프로젝트 전용 파이썬 환경을 만듭니다."
  if ! $SYSPY -m venv "$VENV" 2>/dev/null; then
    echo " [!] 가상환경 생성 실패. 시스템에 직접 설치를 시도합니다."
    PIPFLAGS="--break-system-packages"
  fi
fi

if [ -x "$VENV/bin/python" ]; then
  PY="$VENV/bin/python"
else
  PY="$SYSPY"
fi

# --- 3. 최초 1회 패키지 설치 ---
if [ ! -f ".installed" ]; then
  echo " 필요한 패키지를 설치합니다. 2~5분 걸립니다."
  echo
  $PY -m pip install --upgrade pip $PIPFLAGS >/dev/null 2>&1
  if ! $PY -m pip install -r requirements.txt $PIPFLAGS; then
    echo
    echo " [x] 패키지 설치에 실패했습니다. 위 메시지를 확인하세요."
    echo "     인터넷 연결이나 사내 방화벽 문제일 수 있습니다."
    echo
    read -r -p " 엔터를 누르면 닫힙니다." _
    exit 1
  fi
  touch .installed
  echo
  echo " [v] 준비 완료"
fi

# --- 4. 메뉴 ---
while true; do
  echo
  echo " ------------------------------------------"
  echo "  무엇을 하시겠습니까?"
  echo " ------------------------------------------"
  echo "  1  데모 실행        가짜 데이터로 동작 확인 (30초)"
  echo "  2  최초 데이터 적재  2년치. 20~40분. 처음 딱 한 번"
  echo "  3  오늘 스크리닝    매일 이걸 누르세요 (2~5분)"
  echo "  4  결과 보기        웹 화면 띄우기"
  echo "  5  상태 확인        지금 뭐가 적재됐는지 점검"
  echo "  6  왜 셋업이 없나   1단계 종목별 탈락 사유 (1분)"
  echo "  7  백테스트        과거 데이터로 전략 검증 (1~3분)"
  echo "  8  파라미터 탐색    최적 설정 찾기 (10~30분)"
  echo "  9  종료"
  echo
  read -r -p "  번호 입력: " choice
  echo

  case "$choice" in
    1) $PY run_screen.py --demo; pause_return ;;
    2)
      echo " 전 종목 3년치를 받습니다. 창을 닫지 마세요."
      echo
      $PY run_screen.py --backfill
      pause_return
      ;;
    3) $PY run_screen.py; pause_return ;;
    4)
      # 8000번이 이미 쓰이고 있으면 다음 빈 포트를 찾습니다.
      PORT=8000
      while lsof -i ":$PORT" >/dev/null 2>&1 && [ $PORT -lt 8010 ]; do
        PORT=$((PORT+1))
      done
      echo " 브라우저에서 http://localhost:$PORT 을 엽니다."
      echo " 끝내려면 Ctrl+C 를 누르세요."
      echo
      ( sleep 1; open "http://localhost:$PORT" 2>/dev/null || xdg-open "http://localhost:$PORT" 2>/dev/null ) &
      PYABS="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")"
      ( cd docs && "$PYABS" -m http.server "$PORT" )
      ;;
    5) $PY run_screen.py --status; pause_return ;;
    6) $PY run_screen.py --why; pause_return ;;
    7) $PY run_screen.py --backtest; pause_return ;;
    8)
      echo " 조합마다 전체 기간을 다시 돌립니다. 5~15분 걸립니다."
      echo " 결과를 config에 바로 반영하려면 나중에 다음을 실행하세요:"
      echo "   .venv/bin/python run_screen.py --grid --apply"
      echo
      $PY run_screen.py --grid
      pause_return
      ;;
    9) exit 0 ;;
    *) echo " 1~9 중에서 골라주세요." ;;
  esac
done
