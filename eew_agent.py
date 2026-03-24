#!/usr/bin/env python3
"""
eew_agent.py — GitHub Copilot CLI 驅動的 EEW 系統持續改善代理人

架構：
    1. 監看 run_eew/params/ 等待新事件完成（.rep 序列停止增長）
    2. 呼叫 analyze_rep 技能取得結構化診斷
    3. 規則引擎自動調整 tcpd.d 參數
    4. 將無法自動處理的問題記錄為「待 Copilot 審查」項目
    5. 記錄每次改動的前後比較，建立改善歷史

用法：
    python3 eew_agent.py                    # 啟動代理人，持續監看
    python3 eew_agent.py --dry-run          # 模擬執行，不實際修改任何檔案
    python3 eew_agent.py --review           # 顯示待 Copilot 審查的項目
    python3 eew_agent.py --history          # 顯示改善歷史
    python3 eew_agent.py --analyze-only     # 只分析現有事件，不等待新事件
"""

import argparse
import copy
import json
import os
import re
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from analyze_rep import group_events, analyze_event, load_station_file, DEFAULT_STA_FILE
import glob

# ---------------------------------------------------------------------------
# 路徑設定
# ---------------------------------------------------------------------------

BASE        = os.path.dirname(os.path.abspath(__file__))
PARAMS_DIR  = os.path.join(BASE, "run_eew", "params")
TCPD_D      = os.path.join(PARAMS_DIR, "tcpd.d")
AGENT_DIR   = os.path.join(BASE, ".agent")
HISTORY_FILE= os.path.join(AGENT_DIR, "history.json")
REVIEW_FILE = os.path.join(AGENT_DIR, "pending_review.json")

os.makedirs(AGENT_DIR, exist_ok=True)

BOLD  = "\033[1m"; CYAN = "\033[36m"; GRN = "\033[32m"
YEL   = "\033[33m"; RED  = "\033[91m"; DIM = "\033[2m"; RESET = "\033[0m"

# ---------------------------------------------------------------------------
# tcpd.d 參數讀寫
# ---------------------------------------------------------------------------

# 可自動調整的參數及其安全範圍 [min, max, step]
TUNABLE = {
    "Trig_tm_win":    {"min": 20.0,  "max": 60.0,  "step": 5.0,  "unit": "s"},
    "Trig_dis_win":   {"min": 80.0,  "max": 250.0, "step": 20.0, "unit": "km"},
    "Active_parr_win":{"min": 30.0,  "max": 70.0,  "step": 5.0,  "unit": "s"},
    "SwP_V":          {"min": 4.5,   "max": 6.5,   "step": 0.05, "unit": "km/s"},
    "SwP_VG":         {"min": 0.04,  "max": 0.10,  "step": 0.005,"unit": "km/s/km"},
    "DpP_V":          {"min": 7.0,   "max": 8.5,   "step": 0.05, "unit": "km/s"},
    "DpP_VG":         {"min": 0.002, "max": 0.010, "step": 0.001,"unit": "km/s/km"},
}

def read_params(filepath):
    """從 .d 檔讀取所有可調參數的當前值。"""
    params = {}
    try:
        with open(filepath) as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith("#") or not stripped:
                    continue
                for key in TUNABLE:
                    m = re.match(rf"^\s*{key}\s+([\d.]+)", line)
                    if m:
                        params[key] = float(m.group(1))
    except FileNotFoundError:
        pass
    return params


def write_param(filepath, key, new_val, dry_run=False):
    """安全地更新 .d 檔中單一參數值，保留其餘內容與註解。"""
    with open(filepath) as f:
        content = f.read()

    pattern = rf"(^\s*{re.escape(key)}\s+)([\d.]+)"
    new_content, count = re.subn(
        pattern,
        lambda m: m.group(1) + str(new_val),
        content,
        flags=re.MULTILINE
    )
    if count == 0:
        return False

    if not dry_run:
        # 先備份
        backup = filepath + ".bak"
        with open(backup, "w") as f:
            f.write(content)
        with open(filepath, "w") as f:
            f.write(new_content)
    return True


# ---------------------------------------------------------------------------
# 歷史記錄
# ---------------------------------------------------------------------------

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def load_review():
    if os.path.exists(REVIEW_FILE):
        with open(REVIEW_FILE) as f:
            return json.load(f)
    return []


def save_review(items):
    with open(REVIEW_FILE, "w") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 決策引擎 — 根據分析結果決定調整哪些參數
# ---------------------------------------------------------------------------

def decide_adjustments(analysis, current_params, history):
    """
    輸入：事件分析結果 + 當前參數值 + 歷史記錄
    輸出：
        adjustments  : list of {key, old_val, new_val, reason}
        review_items : list of {issue, suggestion, context}  ← 需要 Copilot 判斷
    """
    adjustments  = []
    review_items = []

    sq  = analysis["solution_quality"]
    mg  = analysis["magnitude"]
    lc  = analysis["location"]

    gap      = sq["final_Gap_deg"]
    perr     = sq["avg_P_residual_s"]
    mall_std = mg["Mall_std"]
    spread   = lc["spread_km"]
    mall     = mg["Mall_final"]
    mpd      = mg["Mpd_final"]
    mtc      = mg["Mtc_final"]
    stable_s = mg["stable_proc_s"]

    # ---- 規則 1：P 波殘差偏大 → 嘗試微調速度模型 -------------------------
    if perr > 1.5:
        # 殘差大 → 速度可能偏低，小幅提高 SwP_V
        key = "SwP_V"
        if key in current_params:
            old = current_params[key]
            new = round(min(old + TUNABLE[key]["step"], TUNABLE[key]["max"]), 5)
            if new != old:
                adjustments.append({
                    "key": key, "old": old, "new": new,
                    "reason": f"P殘差={perr:.2f}s 偏大，上調淺層P波速度 {old}→{new} km/s"
                })
    elif perr > 1.0:
        # 中等偏差 → 加入 Copilot 審查
        review_items.append({
            "issue": f"P波殘差 {perr:.2f}s 偏高（閾值1.0s）",
            "suggestion": "考慮重新校正速度模型 (SwP_V/SwP_VG/DpP_V/DpP_VG)，"
                          "或檢查是否有測站系統性延遲",
            "context": {"avg_P_residual": perr, "current_SwP_V": current_params.get("SwP_V")}
        })

    # ---- 規則 2：方位角空隙大 → 放寬距離窗口讓遠站參與 -------------------
    if gap > 200:
        key = "Trig_dis_win"
        if key in current_params:
            old = current_params[key]
            new = round(min(old + TUNABLE[key]["step"], TUNABLE[key]["max"]), 1)
            if new != old:
                adjustments.append({
                    "key": key, "old": old, "new": new,
                    "reason": f"方位角空隙={gap}° 過大，放寬距離窗口 {old}→{new} km 讓遠站參與"
                })
    elif gap > 170:
        review_items.append({
            "issue": f"方位角空隙 {gap}° 偏大",
            "suggestion": "測站佈建存在空缺方位，短期建議適當放寬 Trig_dis_win；"
                          "長期應在空缺方向增設測站",
            "context": {"gap": gap, "Trig_dis_win": current_params.get("Trig_dis_win")}
        })

    # ---- 規則 3：規模波動大 → 延長存活時窗讓更多測站穩定參與 --------------
    if mall_std > 0.4:
        key = "Active_parr_win"
        if key in current_params:
            old = current_params[key]
            new = round(min(old + TUNABLE[key]["step"], TUNABLE[key]["max"]), 1)
            if new != old:
                adjustments.append({
                    "key": key, "old": old, "new": new,
                    "reason": f"Mall標準差={mall_std:.2f} 過大，延長 Active_parr_win {old}→{new}s"
                })

    # ---- 規則 4：Mall 與 Mpd 差異大 → 需 Copilot 判斷 --------------------
    if abs(mall - mpd) > 1.0:
        review_items.append({
            "issue": f"Mall({mall:.2f}) 與 Mpd({mpd:.2f}) 差異 {abs(mall-mpd):.2f}",
            "suggestion": "Pd 振幅計算可能與規模公式不匹配。"
                          "建議檢查 pick_eew.c 中 Pd 積分時窗，"
                          "或重新推導本地 Mpd 回歸係數",
            "context": {"Mall": mall, "Mpd": mpd, "Mtc": mtc}
        })

    # ---- 規則 5：Mall 與 Mtc 差異大 → 需 Copilot 判斷 --------------------
    if abs(mall - mtc) > 1.0:
        review_items.append({
            "issue": f"Mall({mall:.2f}) 與 Mtc({mtc:.2f}) 差異 {abs(mall-mtc):.2f}",
            "suggestion": "Tc 主頻估算可能在此深度/震距條件下失效。"
                          "建議檢查 pick_ew.c 中 Tc 計算邏輯，"
                          "或限制 Mtc 的適用震距範圍",
            "context": {"Mall": mall, "Mpd": mpd, "Mtc": mtc}
        })

    # ---- 規則 6：收斂太慢 → 收緊時間窗口 -----------------------------------
    if stable_s and stable_s > 25.0:
        key = "Trig_tm_win"
        if key in current_params:
            old = current_params[key]
            new = round(max(old - TUNABLE[key]["step"], TUNABLE[key]["min"]), 1)
            if new != old:
                adjustments.append({
                    "key": key, "old": old, "new": new,
                    "reason": f"規模收斂時間={stable_s:.1f}s 偏慢，縮短 Trig_tm_win {old}→{new}s"
                })

    # ---- 防止反覆在同一參數上來回震盪 ------------------------------------
    adjustments = _filter_oscillation(adjustments, history)

    return adjustments, review_items


def _filter_oscillation(adjustments, history, window=4):
    """若最近 N 次歷史中同一參數已反覆調整，略過本次避免震盪。"""
    recent_keys = {}
    for entry in history[-window:]:
        for adj in entry.get("adjustments", []):
            k = adj["key"]
            recent_keys[k] = recent_keys.get(k, 0) + 1

    filtered = []
    for adj in adjustments:
        if recent_keys.get(adj["key"], 0) >= 3:
            print(f"  {YEL}⚡ 略過 {adj['key']}：近期已調整 {recent_keys[adj['key']]} 次，"
                  f"可能震盪，移至人工審查{RESET}")
        else:
            filtered.append(adj)
    return filtered


# ---------------------------------------------------------------------------
# 應用調整
# ---------------------------------------------------------------------------

def apply_adjustments(adjustments, dry_run=False):
    applied = []
    for adj in adjustments:
        ok = write_param(TCPD_D, adj["key"], adj["new"], dry_run=dry_run)
        status = "✓" if ok else "✗"
        tag = " [DRY-RUN]" if dry_run else ""
        print(f"  {GRN}{status}{RESET} {adj['key']:20s}  "
              f"{adj['old']} → {GRN}{adj['new']}{RESET}  |  {adj['reason']}{tag}")
        if ok:
            applied.append(adj)
    return applied


# ---------------------------------------------------------------------------
# 主迴圈
# ---------------------------------------------------------------------------

def run_agent(dry_run=False, analyze_only=False):
    print(f"\n{BOLD}{CYAN}{'='*65}{RESET}")
    print(f"{BOLD}{CYAN}  EEW 系統改善代理人  (Copilot CLI 驅動){RESET}")
    if dry_run:
        print(f"  {YEL}模式：DRY-RUN（不實際修改任何檔案）{RESET}")
    print(f"{CYAN}{'='*65}{RESET}\n")

    history     = load_history()
    review_items= load_review()
    seen_events = set(e["event_id"] for e in history)
    all_stations = load_station_file(DEFAULT_STA_FILE)

    if analyze_only:
        _process_existing(history, review_items, seen_events, dry_run, all_stations)
        return

    print(f"監看目錄：{PARAMS_DIR}")
    print(f"已處理事件：{len(seen_events)} 個  |  Ctrl-C 停止\n")

    prev_rep_count = 0
    idle_ticks     = 0
    pending        = False   # 有新增檔案尚未分析
    IDLE_THRESHOLD = 5       # 連續 5 秒無新 .rep → 事件結束

    try:
        while True:
            time.sleep(1)
            rep_files = glob.glob(os.path.join(PARAMS_DIR, "*.rep"))
            cur_count = len(rep_files)

            if cur_count != prev_rep_count:
                if cur_count < prev_rep_count:
                    # 目錄被清空或重建，重置所有狀態
                    print(f"\n  {YEL}偵測到 .rep 檔案數量減少 ({prev_rep_count}→{cur_count})，重置計數器{RESET}")
                    pending = False
                idle_ticks = 0
                prev_rep_count = cur_count
                if cur_count > 0:
                    pending = True
                    sys.stdout.write(f"\r  偵測到 {cur_count} 份 .rep  (等待事件完成...)")
                    sys.stdout.flush()
            else:
                if pending:
                    idle_ticks += 1
                    if idle_ticks >= IDLE_THRESHOLD and cur_count > 0:
                        print(f"\n\n{BOLD}▶ 事件序列已穩定，開始分析...{RESET}")
                        _process_existing(history, review_items, seen_events, dry_run, all_stations)
                        idle_ticks = 0
                        pending = False  # 分析完畢，等下次新增才再觸發

    except KeyboardInterrupt:
        print(f"\n\n{DIM}代理人停止。{RESET}")


def _process_existing(history, review_items, seen_events, dry_run, all_stations=None):
    """分析所有尚未處理的事件並應用改善。"""
    events  = group_events(PARAMS_DIR)
    current_params = read_params(TCPD_D)

    new_count = 0
    for ev_files in events:
        event_id = os.path.basename(ev_files[0])[:14]
        if event_id in seen_events:
            continue

        new_count += 1
        print(f"\n{BOLD}{CYAN}── 事件 {event_id} ──{RESET}")
        analysis = analyze_event(ev_files, all_stations=all_stations)
        if not analysis:
            continue

        h = analysis
        print(f"  震央  {h['epicenter']['lat']:.4f}°N  {h['epicenter']['lon']:.4f}°E  "
              f"深度={h['epicenter']['dep']:.1f}km")
        print(f"  規模  Mall={h['magnitude']['Mall_final']:.2f}  "
              f"Mpd={h['magnitude']['Mpd_final']:.2f}  "
              f"Mtc={h['magnitude']['Mtc_final']:.2f}  "
              f"σ=±{h['magnitude']['Mall_std']:.2f}")
        print(f"  品質  Q={h['solution_quality']['final_Q']}  "
              f"Gap={h['solution_quality']['final_Gap_deg']}°  "
              f"P殘差={h['solution_quality']['avg_P_residual_s']:.3f}s")
        tr = h.get("trigger_rate")
        if tr and tr["rate"] is not None:
            rate_pct = f"{tr['rate']*100:.0f}%"
            color = GRN if tr["rate"] >= 0.7 else (YEL if tr["rate"] >= 0.5 else RED)
            print(f"  觸發率  {color}{tr['triggered']}/{tr['available']} 站 ({rate_pct}){RESET}"
                  f"  [{tr['radius_km']:.0f} km 內]")

        # 決策
        adjustments, new_reviews = decide_adjustments(analysis, current_params, history)

        # 顯示問題
        if analysis["issues"]:
            print(f"\n  {RED}問題：{RESET}")
            for iss in analysis["issues"]:
                print(f"    ✗ {iss}")
        if analysis["warnings"]:
            print(f"  {YEL}警告：{RESET}")
            for w in analysis["warnings"]:
                print(f"    ⚠ {w}")

        # 應用參數調整
        if adjustments:
            print(f"\n  {BOLD}自動參數調整：{RESET}")
            applied = apply_adjustments(adjustments, dry_run=dry_run)
            # 更新 current_params
            for adj in applied:
                current_params[adj["key"]] = adj["new"]
        else:
            applied = []
            print(f"\n  {GRN}✓ 無需自動調整參數{RESET}")

        # 累積人工審查項目
        for item in new_reviews:
            item["event_id"]  = event_id
            item["timestamp"] = datetime.now().isoformat()
            review_items.append(item)

        if new_reviews:
            print(f"\n  {YEL}已加入 {len(new_reviews)} 項待 Copilot 審查：{RESET}")
            for item in new_reviews:
                print(f"    → {item['issue']}")

        # 記錄歷史
        entry = {
            "event_id":   event_id,
            "timestamp":  datetime.now().isoformat(),
            "Mall_final": analysis["magnitude"]["Mall_final"],
            "Mall_std":   analysis["magnitude"]["Mall_std"],
            "Gap":        analysis["solution_quality"]["final_Gap_deg"],
            "P_residual": analysis["solution_quality"]["avg_P_residual_s"],
            "adjustments": applied,
            "review_count": len(new_reviews),
        }
        history.append(entry)
        seen_events.add(event_id)
        save_history(history)
        save_review(review_items)

    if new_count == 0:
        print(f"  {DIM}所有事件已處理完畢（{len(seen_events)} 個）{RESET}")
    else:
        _print_trend(history)


def _print_trend(history):
    """顯示參數調整趨勢與改善軌跡。"""
    if len(history) < 2:
        return
    recent = history[-min(10, len(history)):]
    print(f"\n{BOLD}── 改善趨勢（最近 {len(recent)} 個事件）──{RESET}")
    print(f"  {'事件ID':16s}  {'Mall':>5}  {'σ':>5}  {'Gap':>5}  {'P殘差':>6}  調整")
    print(f"  {'-'*60}")
    for e in recent:
        adjs = ", ".join(f"{a['key']}:{a['old']}→{a['new']}" for a in e["adjustments"])
        adjs = adjs[:30] + "…" if len(adjs) > 30 else adjs or "—"
        print(f"  {e['event_id']:16s}  "
              f"{e['Mall_final']:>5.2f}  "
              f"{e['Mall_std']:>5.2f}  "
              f"{e['Gap']:>5}°  "
              f"{e['P_residual']:>6.3f}s  "
              f"{DIM}{adjs}{RESET}")


# ---------------------------------------------------------------------------
# 顯示待審查項目
# ---------------------------------------------------------------------------

def show_review():
    items = load_review()
    if not items:
        print(f"\n{GRN}✓ 目前沒有待 Copilot 審查的項目。{RESET}")
        return

    print(f"\n{BOLD}{YEL}{'='*65}{RESET}")
    print(f"{BOLD}{YEL}  待 Copilot 審查項目  ({len(items)} 項){RESET}")
    print(f"{YEL}{'='*65}{RESET}")
    print(f"\n{DIM}以下問題超出自動調整能力，建議向 Copilot CLI 提問：{RESET}\n")

    for i, item in enumerate(items, 1):
        print(f"{BOLD}{i}. [{item.get('event_id','?')}]  {item['issue']}{RESET}")
        print(f"   建議：{item['suggestion']}")
        ctx = item.get("context", {})
        if ctx:
            print(f"   上下文：{json.dumps(ctx, ensure_ascii=False)}")
        print()

    print(f"{DIM}提示：將上方內容貼給 Copilot CLI，說明「請根據以下診斷修改相關原始碼或參數」{RESET}")


# ---------------------------------------------------------------------------
# 顯示歷史
# ---------------------------------------------------------------------------

def show_history():
    history = load_history()
    if not history:
        print(f"\n{DIM}尚無歷史記錄。{RESET}")
        return

    print(f"\n{BOLD}{'='*65}{RESET}")
    print(f"{BOLD}  改善歷史  ({len(history)} 個事件){RESET}")
    print(f"{'='*65}")
    _print_trend(history)

    total_adj = sum(len(e["adjustments"]) for e in history)
    print(f"\n  累計自動調整次數：{total_adj}")
    print(f"  累計 Copilot 審查項目：{sum(e['review_count'] for e in history)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="EEW 系統持續改善代理人（由 GitHub Copilot CLI 驅動）"
    )
    parser.add_argument("--dry-run",      action="store_true", help="模擬執行，不修改任何檔案")
    parser.add_argument("--review",       action="store_true", help="顯示待 Copilot 審查的項目")
    parser.add_argument("--history",      action="store_true", help="顯示改善歷史")
    parser.add_argument("--analyze-only", action="store_true", help="只分析現有事件，不持續監看")
    args = parser.parse_args()

    if args.review:
        show_review()
    elif args.history:
        show_history()
    else:
        run_agent(dry_run=args.dry_run, analyze_only=args.analyze_only)
