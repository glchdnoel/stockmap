#!/bin/zsh
# 더블클릭하면 토스증권 보유 종목을 받아 다운로드 폴더에 저장해요 (조회 전용).
cd "$(dirname "$0")" || exit 1
/usr/bin/python3 stockmap_sync.py "$@"
echo ""
read -k 1 "?아무 키나 누르면 창이 닫혀요..."
