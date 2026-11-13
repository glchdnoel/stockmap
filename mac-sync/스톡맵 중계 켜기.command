#!/bin/zsh
# 더블클릭하면 맥 중계를 켜고 크롬으로 스톡맵을 앱 창으로 열어요 (국내 종목 실시간, 조회 전용. 사파리는 중계를 못 써요).
# 이 창이 열려 있는 동안만 중계가 돌아요. 다 봤으면 창을 닫거나 Ctrl + C를 누르세요.
cd "$(dirname "$0")" || exit 1
/usr/bin/python3 stockmap_relay.py --open "$@"
echo ""
read -k 1 "?아무 키나 누르면 창이 닫혀요..."
