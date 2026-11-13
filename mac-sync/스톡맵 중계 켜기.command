#!/bin/zsh
# 더블클릭하면 맥 중계를 켜고 스톡맵을 열어요 (크롬에 앱으로 설치했으면 그 앱, 아니면 크롬 탭) (국내 종목 실시간, 조회 전용).
# 이 창이 열려 있는 동안만 중계가 돌아요. 다 봤으면 창을 닫거나 Ctrl + C를 누르세요.
cd "$(dirname "$0")" || exit 1
/usr/bin/python3 stockmap_relay.py --open "$@"
echo ""
read -k 1 "?아무 키나 누르면 창이 닫혀요..."
