#!/usr/bin/env bash
# monitor.sh — 一鍵啟動 EEW 即時監控
# 用法：bash monitor.sh [--map] [--open]
#
#   --map   同時產生互動式地圖 HTML
#   --open  自動在瀏覽器開啟地圖
#
# 會開兩個 Terminal 視窗：
#   視窗 1 (watch_rep)  → 每份 .rep 即時警報 + 防災建議
#   視窗 2 (eew_agent)  → 事件分析 + 自動參數調整

set -e
EW_HOME="$(cd "$(dirname "$0")" && pwd)"
WATCH_ARGS=""

for arg in "$@"; do
  case $arg in
    --map)  WATCH_ARGS="$WATCH_ARGS --map"  ;;
    --open) WATCH_ARGS="$WATCH_ARGS --open" ;;
  esac
done

echo "======================================"
echo "  EEW 監控系統啟動"
echo "  目錄：$EW_HOME"
echo "======================================"

# macOS：用 osascript 開新 Terminal 視窗
open_terminal() {
  local title="$1"
  local cmd="$2"
  osascript <<EOF
tell application "Terminal"
  activate
  set w to do script "echo '=== $title ==='; cd '$EW_HOME'; $cmd"
  set custom title of front window to "$title"
end tell
EOF
}

echo ""
echo "開啟視窗 1：即時警報 (watch_rep)"
open_terminal "EEW 即時警報" "python3 watch_rep.py $WATCH_ARGS"
sleep 1

echo "開啟視窗 2：事件分析代理人 (eew_agent)"
open_terminal "EEW 代理人" "python3 eew_agent.py"
sleep 1

echo ""
echo "✅ 兩個監控視窗已啟動"
echo ""
echo "視窗 1 (watch_rep)  → 每份 .rep 即時印出警報與防災建議"
echo "視窗 2 (eew_agent)  → 事件穩定後分析並自動調整參數"
echo ""
echo "停止監控：在各視窗按 Ctrl-C"
