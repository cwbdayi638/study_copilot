#!/usr/bin/env python3
"""
analyze_rep.py — EEW .rep 檔案分析技能模組

可作為獨立工具或可匯入的技能模組，
供 AI 系統診斷 EEW 解算品質並建議改進方向。

用法：
    python3 analyze_rep.py run_eew/params/          # 分析資料夾內所有事件
    python3 analyze_rep.py run_eew/params/ --json   # 輸出 JSON（供 AI 讀取）
    python3 analyze_rep.py run_eew/params/ --event 2  # 只分析第 N 個事件
"""

import argparse
import glob
import json
import math
import os
import re
import sys

# 確保可以 import watch_rep
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from watch_rep import parse_rep_data, haversine, load_station_file

# 預設測站檔路徑
DEFAULT_STA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "run_eew", "params", "sta_hisn_Z"
)


def calc_trigger_rate(epi_lat, epi_lon, triggered_stas, all_stations, radius_km=50):
    """
    計算觸發率。

    Parameters
    ----------
    epi_lat, epi_lon : float  震央座標
    triggered_stas   : list of dict  .rep 末報中的測站清單（含 sta, lat, lon 欄位）
    all_stations     : dict  {sta_name: (lat, lon)} 來自 sta_hisn_Z
    radius_km        : float 半徑（預設 50 km）

    Returns
    -------
    dict with keys:
        radius_km, available, triggered, rate,
        available_list, triggered_list, untriggered_list
    """
    # 分母：sta_hisn_Z 中距震央 <= radius_km 的測站
    available = {
        sta: coords for sta, coords in all_stations.items()
        if haversine(epi_lat, epi_lon, coords[0], coords[1]) <= radius_km
    }
    available_names = set(available.keys())

    # 分子：.rep 末報中、站名也在 available 集合內的測站
    # （同時確保分子 ≤ 分母，避免計算異常）
    triggered_names = {s["sta"] for s in triggered_stas if s["sta"] in available_names}

    untriggered = sorted(available_names - triggered_names)
    n_avail     = len(available_names)
    n_trigger   = len(triggered_names)
    rate        = round(n_trigger / n_avail, 3) if n_avail > 0 else None

    return {
        "radius_km":        radius_km,
        "available":        n_avail,
        "triggered":        n_trigger,
        "rate":             rate,
        "available_list":   sorted(available_names),
        "triggered_list":   sorted(triggered_names),
        "untriggered_list": untriggered,
    }


# ---------------------------------------------------------------------------
# 事件分組
# ---------------------------------------------------------------------------

def group_events(directory):
    """
    將資料夾內的 .rep 檔依序列號分組，每當序列號重置即視為新事件。
    回傳 list of list[filepath]。
    """
    files = sorted(glob.glob(os.path.join(directory, "*.rep")))
    if not files:
        return []
    events, current, prev_seq = [], [], 0
    for f in files:
        m = re.search(r"_n(\d+)\.rep$", f)
        if not m:
            continue
        seq = int(m.group(1))
        if seq <= prev_seq and current:
            events.append(current)
            current = []
        current.append(f)
        prev_seq = seq
    if current:
        events.append(current)
    return events


# ---------------------------------------------------------------------------
# 單一事件分析
# ---------------------------------------------------------------------------

def analyze_event(files, all_stations=None, radius_km=50):
    """
    分析單一事件的所有 .rep 報告，回傳結構化分析結果字典。
    """
    reports = []
    for f in files:
        try:
            d = parse_rep_data(f)
            seq = int(re.search(r"_n(\d+)\.rep$", f).group(1))
            reports.append((seq, d))
        except Exception as e:
            pass

    if not reports:
        return None

    reports.sort(key=lambda x: x[0])
    first_seq, first = reports[0]
    last_seq,  last  = reports[-1]
    fh, lh = first["hypo"], last["hypo"]

    # ---- 規模序列 -----------------------------------------------------------
    mag_series = []
    for seq, d in reports:
        h = d["hypo"]
        mall = float(h["Mall"])
        if mall > 0:
            mag_series.append({
                "seq":  seq,
                "Mall": mall,
                "Mpd":  float(h["Mpd"]),
                "Mtc":  float(h["Mtc"]),
                "proc": float(h["process_time"]),
                "n":    int(d["header"].get("n", 0)),
                "n_c":  int(d["header"].get("n_c", 0)),
                "Q":    int(d["header"].get("Q", 0)),
                "Gap":  int(d["header"].get("Gap", 999)),
            })

    # ---- 規模收斂指標 -------------------------------------------------------
    malls = [r["Mall"] for r in mag_series]
    mall_mean  = sum(malls) / len(malls) if malls else 0
    mall_std   = math.sqrt(sum((m - mall_mean)**2 for m in malls) / len(malls)) if malls else 0

    # 穩定點：規模連續 5 份報告標準差 < 0.1
    stable_at_seq = None
    window = 5
    for i in range(len(mag_series) - window + 1):
        w = [r["Mall"] for r in mag_series[i:i+window]]
        if max(w) - min(w) < 0.15:
            stable_at_seq = mag_series[i]["seq"]
            stable_proc   = mag_series[i]["proc"]
            break

    # 初報 vs 末報偏差
    first_valid = next((r for r in mag_series), None)
    mag_drift   = round(float(lh["Mall"]) - first_valid["Mall"], 2) if first_valid else None

    # ---- 震源位置收斂 -------------------------------------------------------
    lats = [float(d["hypo"]["lat"]) for _, d in reports]
    lons = [float(d["hypo"]["lon"]) for _, d in reports]
    deps = [float(d["hypo"]["dep"]) for _, d in reports]

    loc_spread_lat = round(max(lats) - min(lats), 4)
    loc_spread_lon = round(max(lons) - min(lons), 4)
    # km per degree ≈ 111
    loc_spread_km  = round(math.sqrt((loc_spread_lat*111)**2 + (loc_spread_lon*111)**2), 1)

    # ---- 測站品質分析 -------------------------------------------------------
    last_stas = last["stations"]
    sta_weights   = [s["H_Wei"] for s in last_stas]
    sta_perrs     = [s["Perr"]  for s in last_stas]
    sta_pds       = [s["pd"]    for s in last_stas if s["pd"] > 0]
    high_wei_pct  = round(100 * sum(1 for w in sta_weights if w >= 0.5) / len(sta_weights), 1) if sta_weights else 0
    avg_perr      = round(sum(abs(p) for p in sta_perrs) / len(sta_perrs), 3) if sta_perrs else 0
    max_pd_sta    = max(last_stas, key=lambda s: s["pd"]) if last_stas else None

    # ---- 觸發率（需提供測站檔） ---------------------------------------------
    epi_lat = float(lh["lat"])
    epi_lon = float(lh["lon"])
    trigger_rate_info = None
    if all_stations:
        trigger_rate_info = calc_trigger_rate(epi_lat, epi_lon, last_stas, all_stations, radius_km)
        # 觸發率偏低時加入警告
        if trigger_rate_info["rate"] is not None and trigger_rate_info["rate"] < 0.5 and trigger_rate_info["available"] >= 3:
            warnings_tr = (f"觸發率偏低 {trigger_rate_info['triggered']}/{trigger_rate_info['available']} "
                           f"({trigger_rate_info['rate']*100:.0f}%) — "
                           f"{radius_km} km 內有 {trigger_rate_info['available']} 站，"
                           f"僅觸發 {trigger_rate_info['triggered']} 站")
        else:
            warnings_tr = None
    else:
        warnings_tr = None

    # ---- 品質判斷 -----------------------------------------------------------
    final_Q   = int(last["header"].get("Q", -99))
    final_Gap = int(last["header"].get("Gap", 999))

    issues   = []
    warnings = []

    if final_Gap > 180:
        issues.append(f"方位角空隙過大 ({final_Gap}°) — 震央周圍測站分布不均，定位可能偏移")
    if mall_std > 0.3:
        issues.append(f"規模波動過大 (σ={mall_std:.2f}) — 各報告 Mall 不穩定")
    if abs(float(lh["Mall"]) - float(lh["Mpd"])) > 0.8:
        warnings.append(f"Mall 與 Mpd 差異大 ({float(lh['Mall']):.2f} vs {float(lh['Mpd']):.2f}) — 規模估算方法不一致")
    if abs(float(lh["Mall"]) - float(lh["Mtc"])) > 0.8:
        warnings.append(f"Mall 與 Mtc 差異大 ({float(lh['Mall']):.2f} vs {float(lh['Mtc']):.2f}) — Tc 方法可能受干擾")
    if avg_perr > 1.0:
        issues.append(f"平均 P 波走時殘差偏大 ({avg_perr:.2f}s) — 速度模型可能需調整")
    if stable_at_seq is None:
        warnings.append("規模未在本事件序列內收斂 (連續5份差異≥0.15)")
    if loc_spread_km > 20:
        warnings.append(f"定位散布範圍 {loc_spread_km} km — 初報定位不穩定")
    if float(fh["Mall"]) == 0:
        warnings.append("初報 Mall=0 — 初期測站不足，無法立即估算規模")
    if warnings_tr:
        warnings.append(warnings_tr)

    # ---- 改進建議 -----------------------------------------------------------
    suggestions = _generate_suggestions(
        final_Gap, mall_std, avg_perr, loc_spread_km,
        float(lh["Mall"]), float(lh["Mpd"]), float(lh["Mtc"]),
        stable_at_seq, mag_series
    )

    return {
        "event_id":        os.path.basename(files[0])[:14],
        "origin_time":     f"{lh['year']}/{int(lh['month']):02d}/{int(lh['day']):02d} "
                           f"{int(lh['hour']):02d}:{int(lh['min']):02d}:{float(lh['sec']):05.2f}",
        "epicenter":       {"lat": float(lh["lat"]), "lon": float(lh["lon"]), "dep": float(lh["dep"])},
        "report_count":    len(reports),
        "magnitude": {
            "Mall_final":  float(lh["Mall"]),
            "Mpd_final":   float(lh["Mpd"]),
            "Mtc_final":   float(lh["Mtc"]),
            "Mall_mean":   round(mall_mean, 2),
            "Mall_std":    round(mall_std,  2),
            "mag_drift":   mag_drift,
            "stable_at_seq":  stable_at_seq,
            "stable_proc_s":  stable_proc if stable_at_seq else None,
        },
        "location": {
            "spread_lat_deg": loc_spread_lat,
            "spread_lon_deg": loc_spread_lon,
            "spread_km":      loc_spread_km,
            "dep_range":      [min(deps), max(deps)],
        },
        "solution_quality": {
            "final_Q":         final_Q,
            "final_Gap_deg":   final_Gap,
            "avg_P_residual_s": avg_perr,
            "high_weight_sta_pct": high_wei_pct,
            "stations_final":  len(last_stas),
            "max_pd_station":  max_pd_sta["sta"] if max_pd_sta else None,
            "max_pd_value":    round(max_pd_sta["pd"], 5) if max_pd_sta else None,
        },
        "first_warning_proc_s": float(first["hypo"]["process_time"]),
        "last_proc_s":          float(lh["process_time"]),
        "trigger_rate":         trigger_rate_info,
        "mag_series":           mag_series,
        "issues":               issues,
        "warnings":             warnings,
        "suggestions":          suggestions,
    }


# ---------------------------------------------------------------------------
# 改進建議生成
# ---------------------------------------------------------------------------

def _generate_suggestions(gap, mall_std, avg_perr, loc_spread_km,
                           mall, mpd, mtc, stable_seq, mag_series):
    s = []

    if gap > 180:
        s.append({
            "target": "測站佈建",
            "param":  None,
            "action": f"方位角空隙 {gap}° 過大，建議在空缺方位增設測站或降低 Trig_dis_win 讓遠站提早參與",
        })
    if avg_perr > 1.0:
        s.append({
            "target": "速度模型",
            "param":  "tcpd.d: SwP_V / SwP_VG / DpP_V / DpP_VG",
            "action": f"P 波殘差平均 {avg_perr:.2f}s，建議重新校正速度模型參數以符合本地地殼結構",
        })
    if mall_std > 0.3:
        s.append({
            "target": "規模穩定性",
            "param":  "tcpd.d: Trig_tm_win / Active_parr_win",
            "action": f"Mall 標準差 {mall_std:.2f}，建議延長 Active_parr_win 讓更多穩定測站參與，減少規模跳動",
        })
    if abs(mall - mpd) > 0.8:
        s.append({
            "target": "Mpd 校正",
            "param":  "pick_eew.d: Pd 振幅計算時窗",
            "action": f"Mall={mall:.2f} 與 Mpd={mpd:.2f} 差異 {abs(mall-mpd):.2f}，"
                      f"建議檢查 pick_eew 中 Pd 積分時窗是否適合本地衰減特性",
        })
    if abs(mall - mtc) > 0.8:
        s.append({
            "target": "Mtc 校正",
            "param":  "pick_eew.d: Tc 計算參數",
            "action": f"Mall={mall:.2f} 與 Mtc={mtc:.2f} 差異 {abs(mall-mtc):.2f}，"
                      f"建議重新評估 Tc 主頻期估算方式或適用震距範圍",
        })
    if loc_spread_km > 20:
        s.append({
            "target": "定位收斂速度",
            "param":  "tcpd.d: Trig_tm_win / Trig_dis_win",
            "action": f"早期定位散布 {loc_spread_km} km，建議適當縮小 Trig_dis_win 或提高最小觸發站數以增加初報穩定性",
        })
    if stable_seq and stable_seq > 20:
        s.append({
            "target": "規模收斂速度",
            "param":  "tcpd.d: Term_num / Active_parr_win",
            "action": f"規模到第 {stable_seq} 報才收斂，建議調整權重機制讓近站 Pd 更早主導規模估算",
        })
    return s


# ---------------------------------------------------------------------------
# 多事件比較
# ---------------------------------------------------------------------------

def compare_events(results):
    """跨事件統計摘要，回傳字典。"""
    valid = [r for r in results if r]
    if not valid:
        return {}
    malls    = [r["magnitude"]["Mall_final"] for r in valid]
    stds     = [r["magnitude"]["Mall_std"]   for r in valid]
    gaps     = [r["solution_quality"]["final_Gap_deg"] for r in valid]
    perrs    = [r["solution_quality"]["avg_P_residual_s"] for r in valid]
    stable_t = [r["magnitude"]["stable_proc_s"] for r in valid if r["magnitude"]["stable_proc_s"]]
    tr_rates = [r["trigger_rate"]["rate"] for r in valid
                if r.get("trigger_rate") and r["trigger_rate"]["rate"] is not None]

    return {
        "event_count":          len(valid),
        "Mall_range":           [round(min(malls),2), round(max(malls),2)],
        "Mall_std_avg":         round(sum(stds)/len(stds), 2),
        "Gap_avg":              round(sum(gaps)/len(gaps), 1),
        "avg_P_residual":       round(sum(perrs)/len(perrs), 3),
        "stable_proc_avg_s":    round(sum(stable_t)/len(stable_t), 1) if stable_t else None,
        "trigger_rate_avg":     round(sum(tr_rates)/len(tr_rates), 3) if tr_rates else None,
        "events_with_issues":   sum(1 for r in valid if r["issues"]),
        "total_issues":         sum(len(r["issues"]) for r in valid),
        "total_warnings":       sum(len(r["warnings"]) for r in valid),
    }


# ---------------------------------------------------------------------------
# 報表輸出
# ---------------------------------------------------------------------------

BOLD  = "\033[1m"
CYAN  = "\033[36m"
YEL   = "\033[33m"
RED   = "\033[91m"
GRN   = "\033[32m"
DIM   = "\033[2m"
RESET = "\033[0m"

def print_event_report(result, idx=None):
    label = f"事件 {idx}  [{result['event_id']}]" if idx else result["event_id"]
    print(f"\n{BOLD}{CYAN}{'='*65}{RESET}")
    print(f"{BOLD}{CYAN}  {label}{RESET}")
    print(f"{CYAN}{'='*65}{RESET}")

    ep = result["epicenter"]
    print(f"  發震時刻  : {result['origin_time']}")
    print(f"  震央      : {ep['lat']:.4f}°N  {ep['lon']:.4f}°E  深度={ep['dep']:.1f} km")
    print(f"  報告數    : {result['report_count']} 份")

    mg = result["magnitude"]
    print(f"\n{BOLD}【規模】{RESET}")
    print(f"  末報  Mall={mg['Mall_final']:.2f}  Mpd={mg['Mpd_final']:.2f}  Mtc={mg['Mtc_final']:.2f}")
    print(f"  統計  平均={mg['Mall_mean']:.2f}  標準差=±{mg['Mall_std']:.2f}  初末偏移={mg['mag_drift']:+.2f}")
    if mg["stable_at_seq"]:
        print(f"  收斂  第 {mg['stable_at_seq']} 報 ({mg['stable_proc_s']:.1f}s 後) 規模趨於穩定")
    else:
        print(f"  收斂  {YEL}未偵測到穩定收斂{RESET}")

    lc = result["location"]
    print(f"\n{BOLD}【定位】{RESET}")
    print(f"  散布範圍  {lc['spread_km']} km  (Δlat={lc['spread_lat_deg']:.4f}°  Δlon={lc['spread_lon_deg']:.4f}°)")
    print(f"  深度範圍  {lc['dep_range'][0]:.1f} ~ {lc['dep_range'][1]:.1f} km")

    sq = result["solution_quality"]
    print(f"\n{BOLD}【解算品質】{RESET}")
    print(f"  Q={sq['final_Q']}  Gap={sq['final_Gap_deg']}°  "
          f"P殘差={sq['avg_P_residual_s']:.3f}s  "
          f"高權重測站={sq['high_weight_sta_pct']:.0f}%")
    print(f"  末報測站數={sq['stations_final']}  "
          f"最大Pd測站={sq['max_pd_station']} ({sq['max_pd_value']:.4f} cm)")
    print(f"  預警時間  初報={result['first_warning_proc_s']:.1f}s  末報={result['last_proc_s']:.1f}s")

    # 觸發率
    tr = result.get("trigger_rate")
    if tr:
        rate_pct = f"{tr['rate']*100:.0f}%" if tr["rate"] is not None else "N/A"
        color = GRN if (tr["rate"] or 0) >= 0.7 else (YEL if (tr["rate"] or 0) >= 0.5 else RED)
        print(f"\n{BOLD}【觸發率（{tr['radius_km']} km 內）】{RESET}")
        print(f"  觸發 {tr['triggered']} / 可用 {tr['available']} 站  →  {color}{rate_pct}{RESET}")
        if tr["untriggered_list"]:
            print(f"  未觸發測站：{', '.join(tr['untriggered_list'])}")
    else:
        print(f"\n{DIM}  （未提供 sta_hisn_Z，無法計算觸發率）{RESET}")

    # 規模演變摘要（每5報）
    print(f"\n{BOLD}【規模演變】{RESET}")
    series = result["mag_series"]
    milestone_seqs = {1,5,10,15,20,25,30,35,40,45,50}
    printed = []
    for r in series:
        if r["seq"] in milestone_seqs or r == series[-1]:
            if r["seq"] not in [p["seq"] for p in printed]:
                printed.append(r)
    for r in printed:
        bar_len = int(r["Mall"] * 4)
        bar = "█" * bar_len
        print(f"  n{r['seq']:>2} ({r['proc']:>5.1f}s)  Mall={r['Mall']:.2f}  "
              f"n={r['n']:>2}  Q={r['Q']:>3}  {GRN}{bar}{RESET}")

    # 問題與警告
    if result["issues"]:
        print(f"\n{BOLD}【問題】{RESET}")
        for iss in result["issues"]:
            print(f"  {RED}✗{RESET} {iss}")
    if result["warnings"]:
        print(f"\n{BOLD}【警告】{RESET}")
        for w in result["warnings"]:
            print(f"  {YEL}⚠{RESET} {w}")

    # 改進建議
    if result["suggestions"]:
        print(f"\n{BOLD}【改進建議】{RESET}")
        for i, sg in enumerate(result["suggestions"], 1):
            print(f"  {CYAN}{i}. [{sg['target']}]{RESET}")
            if sg["param"]:
                print(f"     參數: {sg['param']}")
            print(f"     建議: {sg['action']}")


def print_summary(comparison, results):
    print(f"\n{BOLD}{'='*65}{RESET}")
    print(f"{BOLD}  跨事件綜合摘要  ({comparison['event_count']} 個事件){RESET}")
    print(f"{'='*65}")
    print(f"  規模範圍      : {comparison['Mall_range'][0]} ~ {comparison['Mall_range'][1]}")
    print(f"  規模平均標準差: ±{comparison['Mall_std_avg']}")
    print(f"  平均方位角空隙: {comparison['Gap_avg']}°")
    print(f"  平均 P 波殘差 : {comparison['avg_P_residual']} s")
    if comparison["stable_proc_avg_s"]:
        print(f"  平均收斂時間  : {comparison['stable_proc_avg_s']} s")
    if comparison.get("trigger_rate_avg") is not None:
        tr_avg = comparison["trigger_rate_avg"]
        color = GRN if tr_avg >= 0.7 else (YEL if tr_avg >= 0.5 else RED)
        print(f"  平均觸發率    : {color}{tr_avg*100:.0f}%{RESET}")
    print(f"  有問題的事件  : {comparison['events_with_issues']} / {comparison['event_count']}")
    print(f"  總問題數      : {comparison['total_issues']} 個問題  {comparison['total_warnings']} 個警告")

    # Collect all unique suggestions across events
    seen = set()
    all_suggestions = []
    for r in results:
        if r:
            for sg in r["suggestions"]:
                key = sg["target"]
                if key not in seen:
                    seen.add(key)
                    all_suggestions.append(sg)
    if all_suggestions:
        print(f"\n{BOLD}  共同改進方向:{RESET}")
        for sg in all_suggestions:
            print(f"  → [{sg['target']}]  {sg['action'][:70]}{'...' if len(sg['action'])>70 else ''}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="分析 EEW .rep 檔案序列，診斷解算品質並提供改進建議。"
    )
    parser.add_argument(
        "directory", nargs="?",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_eew", "params"),
        help="包含 .rep 檔案的資料夾路徑",
    )
    parser.add_argument("--json",  "-j", action="store_true", help="輸出 JSON 格式（供 AI 讀取）")
    parser.add_argument("--event", "-e", type=int, default=None, help="只分析第 N 個事件（從 1 開始）")
    parser.add_argument("--summary", "-s", action="store_true", help="只顯示跨事件摘要")
    parser.add_argument("--radius", "-r", type=float, default=50.0,
                        help="觸發率計算半徑（km，預設 50）")
    parser.add_argument("--sta-file", default=DEFAULT_STA_FILE,
                        help="測站檔路徑（預設 run_eew/params/sta_hisn_Z）")
    args = parser.parse_args()

    all_stations = load_station_file(args.sta_file)
    if all_stations:
        print(f"{DIM}  已載入測站檔：{len(all_stations)} 站  "
              f"觸發率半徑 {args.radius:.0f} km{RESET}", file=sys.stderr)
    else:
        print(f"{YEL}  ⚠ 找不到測站檔 {args.sta_file}，觸發率計算停用{RESET}", file=sys.stderr)

    events = group_events(args.directory)
    if not events:
        print(f"在 {args.directory} 中找不到 .rep 檔案")
        sys.exit(1)

    if args.event:
        if args.event < 1 or args.event > len(events):
            print(f"事件編號超出範圍（共 {len(events)} 個事件）")
            sys.exit(1)
        selected = [events[args.event - 1]]
        indices  = [args.event]
    else:
        selected = events
        indices  = list(range(1, len(events) + 1))

    results = [analyze_event(ev, all_stations=all_stations, radius_km=args.radius) for ev in selected]
    comparison = compare_events(results)

    if args.json:
        output = {
            "events":     results,
            "comparison": comparison,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return

    if not args.summary:
        for idx, result in zip(indices, results):
            if result:
                print_event_report(result, idx)

    if len(results) > 1 or args.summary:
        print_summary(comparison, results)


if __name__ == "__main__":
    main()
