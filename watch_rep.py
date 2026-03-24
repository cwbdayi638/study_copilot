#!/usr/bin/env python3
"""
watch_rep.py — Monitor run_eew/params/ for new .rep files and print an alarm.
               Optionally plot epicenter and triggered stations on an interactive map.

Usage:
    python3 watch_rep.py                      # watch default directory, no map
    python3 watch_rep.py --map                # watch + plot map for each new report
    python3 watch_rep.py --map --open         # also auto-open map in browser
    python3 watch_rep.py /path/to/params      # custom directory
    python3 watch_rep.py /path/to/params --map --open
"""

import argparse
import math
import os
import sys
import time
import webbrowser

WATCH_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_eew", "params")
DEFAULT_STA_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "run_eew", "params", "sta_hisn_Z"
)
DEFAULT_RADIUS_KM = 50


# ---------------------------------------------------------------------------
# Station file utilities (also used by analyze_rep.py)
# ---------------------------------------------------------------------------

def haversine(lat1, lon1, lat2, lon2):
    """回傳兩點大圓距離（km）。"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def load_station_file(filepath):
    """
    解析 sta_hisn_Z，回傳 {sta_name: (lat, lon)} dict（每站保留一筆）。
    格式：STA  CHANNEL  NET  LOC  LAT  LON  ELEV  SENSFACTOR  UNIT
    """
    stations = {}
    if not filepath or not os.path.isfile(filepath):
        return stations
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) < 6:
                continue
            sta = parts[0]
            try:
                lat, lon = float(parts[4]), float(parts[5])
            except ValueError:
                continue
            if sta not in stations:
                stations[sta] = (lat, lon)
    return stations
POLL_INTERVAL = 1.0  # seconds between directory scans


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_lines(filepath):
    """Return non-blank lines from a .rep file."""
    with open(filepath) as f:
        return [l for l in f.readlines() if l.strip()]


def parse_rep_data(filepath):
    """
    Parse a .rep file and return a dict with:
      header   : dict of key=value tokens from header line
      rep_time : reporting timestamp string
      hypo     : dict with origin time, lat, lon, dep, magnitudes, process_time
      stations : list of dicts, one per station line
    """
    lines = _parse_lines(filepath)

    # ---- Header (line 0) ---------------------------------------------------
    header_str = lines[0].strip()
    rep_time = header_str.split()[2] + " " + header_str.split()[3]
    header = {}
    for part in header_str.split():
        if "=" in part:
            k, v = part.rstrip(",").split("=", 1)
            header[k] = v

    # ---- Hypocenter (line 2, line 1 is column labels) ----------------------
    h = lines[2].split()
    hypo = {
        "year": h[0], "month": h[1], "day": h[2],
        "hour": h[3], "min":   h[4], "sec": h[5],
        "lat":  float(h[6]),
        "lon":  float(h[7]),
        "dep":  float(h[8]),
        "Mall": h[9],
        "Mpd_s": h[10],
        "Mpv":  h[11],
        "Mpd":  h[12],
        "Mtc":  h[13],
        "process_time": h[14],
    }

    # ---- Stations (line 4 is column labels, stations start at line 5) ------
    # Fields: sta C N L lat lon pa pv pd tc Mtc MPv MPd Perr Dis H_Wei
    #         Parr_date Parr_time Pk_wei Upd_sec P_S usd_sec
    stations = []
    for line in lines[4:]:
        f = line.split()
        if len(f) < 20:
            continue
        try:
            sta = {
                "sta":     f[0],
                "channel": f[1],
                "network": f[2],
                "location":f[3],
                "lat":     float(f[4]),
                "lon":     float(f[5]),
                "pa":      float(f[6]),
                "pv":      float(f[7]),
                "pd":      float(f[8]),
                "tc":      float(f[9]),
                "Mtc":     float(f[10]),
                "MPv":     float(f[11]),
                "MPd":     float(f[12]),
                "Perr":    float(f[13]),
                "Dis":     float(f[14]),
                "H_Wei":   float(f[15]),
                "Parr":    f[16] + " " + f[17],
                "Pk_wei":  f[18],
                "Upd_sec": f[19],
                "P_S":     float(f[20]),
                "usd_sec": f[21],
            }
            stations.append(sta)
        except (ValueError, IndexError):
            continue

    return {
        "rep_time": rep_time,
        "header":   header,
        "hypo":     hypo,
        "stations": stations,
    }


def format_summary(data):
    """Format parsed .rep data as a human-readable string."""
    h  = data["hypo"]
    hd = data["header"]
    return (
        f"  Report time : {data['rep_time']}\n"
        f"  Origin      : {h['year']}/{int(h['month']):02d}/{int(h['day']):02d} "
        f"{int(h['hour']):02d}:{int(h['min']):02d}:{float(h['sec']):05.2f}  "
        f"lat={h['lat']}  lon={h['lon']}  dep={h['dep']} km\n"
        f"  Magnitude   : Mall={h['Mall']}  Mpd={h['Mpd']}  Mtc={h['Mtc']}\n"
        f"  Stations    : triggered={hd.get('n','?')}  "
        f"location={hd.get('n_c','?')}  magnitude={hd.get('n_m','?')}\n"
        f"  Quality     : Q={hd.get('Q','?')}  Gap={hd.get('Gap','?')}°  "
        f"process_time={h['process_time']}s"
    )


# ---------------------------------------------------------------------------
# Disaster prevention advice
# ---------------------------------------------------------------------------

# CWB seismic intensity scale thresholds (approximate, based on magnitude + depth)
# Advice follows Taiwan's standard EEW public guidelines.

def _estimate_intensity(mall, dep):
    """
    Rough intensity class (1–7) from magnitude and depth.
    Shallower + larger magnitude → higher intensity.
    """
    if mall < 4.0:
        return 1
    base = mall - 0.5 * (dep / 30.0)   # depth penalty
    if base < 4.0:  return 2
    if base < 4.8:  return 3
    if base < 5.5:  return 4
    if base < 6.0:  return 5
    if base < 6.5:  return 6
    return 7

_ADVICE = {
    1: {
        "level":   "第一級 – 微震",
        "color":   "\033[32m",
        "summary": "預估輕微搖晃，無需特別行動。",
        "actions": [],
    },
    2: {
        "level":   "第二級 – 輕震",
        "color":   "\033[32m",
        "summary": "輕微震動，保持警覺。",
        "actions": [
            "保持冷靜，注意周遭環境。",
            "遠離高大書架或懸掛物品。",
        ],
    },
    3: {
        "level":   "第三級 – 弱震",
        "color":   "\033[33m",
        "summary": "中等搖晃，請採取預防措施。",
        "actions": [
            "遠離窗戶與重型家具。",
            "若在室內：留在室內，躲在門框旁或堅固桌子下方。",
            "若在行車中：放慢車速，安全靠邊停車。",
        ],
    },
    4: {
        "level":   "第四級 – 中震",
        "color":   "\033[33m",
        "summary": "強烈搖晃，立即採取保護行動。",
        "actions": [
            "趴下（DROP）── 雙手撐地跪下。",
            "掩護（COVER）── 躲入堅固桌下或靠近內牆。",
            "抓緊（HOLD ON）── 保護頭部與頸部。",
            "遠離窗戶、外牆及重物。",
            "切勿使用電梯。",
            "若在戶外：遠離建築物、電線桿與樹木。",
        ],
    },
    5: {
        "level":   "第五級 – 強震",
        "color":   "\033[91m",
        "summary": "強烈震動，建築物可能受損。",
        "actions": [
            "立即趴下、掩護、抓緊（趴掩抓）。",
            "預期物品掉落，建築物可能受損。",
            "搖晃期間切勿奔跑至室外。",
            "搖晃停止後：小心疏散，檢查瓦斯是否洩漏。",
            "切勿使用電梯或電扶梯。",
            "若在海岸附近：搖晃停止後立即往內陸撤離，注意海嘯風險。",
            "收聽氣象局或各官方緊急廣播。",
        ],
    },
    6: {
        "level":   "第六級 – 烈震",
        "color":   "\033[91m",
        "summary": "劇烈搖晃，重大災害可能發生。",
        "actions": [
            "立即趴下、掩護、抓緊，不計一切保護頭頸部。",
            "預期嚴重結構損壞，建築物可能局部倒塌。",
            "搖晃完全停止前切勿移動。",
            "搖晃停止後：立即徒步走樓梯疏散，禁用電梯。",
            "協助傷者，勿任意移動重傷者。",
            "確認安全後關閉瓦斯與電源。",
            "海嘯警報：若在海岸地區，立即撤往高地。",
            "避免行走損毀道路與橋樑。",
            "服從緊急救援人員指示。",
        ],
    },
    7: {
        "level":   "第七級 – 劇震",
        "color":   "\033[91m",
        "summary": "毀滅性搖晃，生命面臨極大危險。",
        "actions": [
            "立即趴下、掩護、抓緊，原地不動。",
            "建築物可能倒塌，保持最低姿勢。",
            "搖晃停止後立即逃生，僅可走樓梯。",
            "海嘯警報：所有沿海地區立即撤往高地，刻不容緩。",
            "預期大規模基礎設施損毀（道路、橋樑、水電）。",
            "非生命危急情況勿佔用緊急報案電話。",
            "全力服從所有官方緊急指示。",
            "強烈餘震隨時可能發生，持續保持警戒。",
        ],
    },
}

def generate_advice(data):
    """
    Return a formatted disaster prevention advice string (Traditional Chinese)
    based on magnitude, depth, and estimated remaining warning time.
    """
    h          = data["hypo"]
    mall       = float(h["Mall"])
    dep        = float(h["dep"])
    proc_time  = float(h["process_time"])

    intensity  = _estimate_intensity(mall, dep)
    advice     = _ADVICE[intensity]

    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    color  = advice["color"]

    # Rough S-wave travel time estimate to nearest triggered station
    nearest_dis = min((s["Dis"] for s in data["stations"]), default=0)
    # S-wave velocity ~3.5 km/s
    s_travel = nearest_dis / 3.5 if nearest_dis > 0 else 0
    warning_sec = s_travel - proc_time
    if warning_sec > 0:
        warning_str = f"距最近測站約剩 {warning_sec:.0f} 秒預警時間"
    else:
        warning_str = "S波可能已抵達最近測站"

    lines = [
        f"{BOLD}{color}⚠  地震速報 — 防災行動建議{RESET}",
        f"{color}{BOLD}{advice['level']}{RESET}  |  {advice['summary']}",
        f"   規模 Mall={mall}  深度={dep} km  |  {warning_str}",
    ]
    if advice["actions"]:
        lines.append("")
        for i, action in enumerate(advice["actions"], 1):
            lines.append(f"   {i}. {action}")

    tsunami = (mall >= 6.5 and dep <= 35.0)
    if tsunami and intensity < 5:
        lines.append("")
        lines.append(f"   {color}{BOLD}⚡ 海嘯風險：規模≥6.5 淺層地震，請密切注意沿海海嘯警報。{RESET}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Map
# ---------------------------------------------------------------------------

def plot_map(filepath, data, open_browser=False, all_stations=None, radius_km=DEFAULT_RADIUS_KM):
    """
    Build a folium map showing:
      - Epicenter (red star)
      - Triggered stations (colored by Pd, blue→red)
      - Missed stations within radius (orange hollow circles)
      - Stations outside radius (small gray circles)
      - Dashed circle at `radius_km` from epicenter
    Saves HTML to maps/<rep_basename>.html beside the .rep file.
    """
    try:
        import folium
        from folium import MacroElement
        from jinja2 import Template
    except ImportError:
        print("  [map] folium not installed — run: pip install folium")
        return None

    hypo     = data["hypo"]
    stations = data["stations"]
    rep_name = os.path.splitext(os.path.basename(filepath))[0]

    maps_dir = os.path.join(os.path.dirname(filepath), "maps")
    os.makedirs(maps_dir, exist_ok=True)
    out_html = os.path.join(maps_dir, rep_name + ".html")

    epi_lat = float(hypo["lat"])
    epi_lon = float(hypo["lon"])

    # ---- Folium map ---------------------------------------------------------
    m = folium.Map(
        location=[epi_lat, epi_lon],
        zoom_start=8,
        tiles="CartoDB positron",
    )

    # ---- Dashed radius circle (injected via Leaflet JS) --------------------
    radius_m = radius_km * 1000

    class DashedCircle(MacroElement):
        def __init__(self, lat, lon, radius_m, color="#555555",
                     dash="8, 6", label=""):
            super().__init__()
            self._template = Template(u"""
                {% macro script(this, kwargs) %}
                var dashedCircle = L.circle(
                    [{{ this.lat }}, {{ this.lon }}],
                    {
                        radius: {{ this.radius_m }},
                        color: "{{ this.color }}",
                        weight: 2,
                        fill: false,
                        dashArray: "{{ this.dash }}",
                        opacity: 0.75
                    }
                );
                dashedCircle.addTo({{ this._parent.get_name() }});
                {% if this.label %}
                dashedCircle.bindTooltip("{{ this.label }}", {permanent:true, direction:'right', opacity:0.7});
                {% endif %}
                {% endmacro %}
            """)
            self.lat     = lat
            self.lon     = lon
            self.radius_m = radius_m
            self.color   = color
            self.dash    = dash
            self.label   = label

    DashedCircle(
        lat=epi_lat, lon=epi_lon, radius_m=radius_m,
        color="#555555", dash="8, 6",
        label=f"{radius_km:.0f} km"
    ).add_to(m)

    # ---- Epicenter marker --------------------------------------------------
    folium.Marker(
        location=[epi_lat, epi_lon],
        popup=folium.Popup(
            f"<b>震央</b><br>"
            f"時刻：{hypo['year']}/{int(hypo['month']):02d}/{int(hypo['day']):02d} "
            f"{int(hypo['hour']):02d}:{int(hypo['min']):02d}:{float(hypo['sec']):05.2f}<br>"
            f"Lat={epi_lat}  Lon={epi_lon}<br>"
            f"深度={hypo['dep']} km<br>"
            f"Mall={hypo['Mall']}  Mpd={hypo['Mpd']}  Mtc={hypo['Mtc']}<br>"
            f"process_time={hypo['process_time']} s",
            max_width=280,
        ),
        tooltip="震央",
        icon=folium.Icon(color="red", icon="star", prefix="fa"),
    ).add_to(m)

    # ---- Pd colormap -------------------------------------------------------
    pd_values = [s["pd"] for s in stations if s["pd"] > 0]
    pd_min = min(pd_values) if pd_values else 0.001
    pd_max = max(pd_values) if pd_values else 1.0

    def pd_color(pd):
        if pd <= 0:
            return "#aaaaaa"
        t = (math.log10(pd) - math.log10(pd_min)) / (
            math.log10(pd_max) - math.log10(pd_min) + 1e-9)
        t = max(0.0, min(1.0, t))
        stops = [
            (0.00, (0,   0,   255)),
            (0.25, (0,   200, 200)),
            (0.50, (0,   200, 0)),
            (0.75, (255, 200, 0)),
            (1.00, (255, 0,   0)),
        ]
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t <= t1:
                f = (t - t0) / (t1 - t0)
                r = int(c0[0] + f * (c1[0] - c0[0]))
                g = int(c0[1] + f * (c1[1] - c0[1]))
                b = int(c0[2] + f * (c1[2] - c0[2]))
                return f"#{r:02x}{g:02x}{b:02x}"
        return "#ff0000"

    # ---- Layer groups -------------------------------------------------------
    grp_triggered = folium.FeatureGroup(name="觸發測站（Pd 著色）", show=True)
    grp_missed    = folium.FeatureGroup(name=f"未觸發測站（{radius_km:.0f} km 內）", show=True)
    grp_outside   = folium.FeatureGroup(name=f"其他測站（{radius_km:.0f} km 外）", show=True)

    triggered_names = {s["sta"] for s in stations}

    # ---- Triggered stations ------------------------------------------------
    for s in stations:
        color = pd_color(s["pd"])
        dist  = haversine(epi_lat, epi_lon, s["lat"], s["lon"])
        scnl  = f"{s['sta']}.{s['channel']}.{s['network']}.{s['location']}"
        popup_html = (
            f"<b>✅ {scnl}</b><br>"
            f"距震央：{dist:.1f} km（.rep 中距={s['Dis']} km）<br>"
            f"Pa={s['pa']:.4f}  Pv={s['pv']:.4f}  <b>Pd={s['pd']:.4f}</b><br>"
            f"Tc={s['tc']:.3f} s<br>"
            f"Mtc={s['Mtc']}  MPv={s['MPv']}  MPd={s['MPd']}<br>"
            f"P arrival：{s['Parr']}<br>"
            f"P-S interval：{s['P_S']} s<br>"
            f"Perr={s['Perr']} s  H_Wei={s['H_Wei']}"
        )
        folium.CircleMarker(
            location=[s["lat"], s["lon"]],
            radius=8,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            popup=folium.Popup(popup_html, max_width=280),
            tooltip=f"{s['sta']}  Pd={s['pd']:.4f} cm  {dist:.1f} km",
        ).add_to(grp_triggered)
        folium.PolyLine(
            locations=[[epi_lat, epi_lon], [s["lat"], s["lon"]]],
            color=color, weight=1, opacity=0.4,
        ).add_to(grp_triggered)

    # ---- Missed / outside stations (from sta_hisn_Z) -----------------------
    if all_stations:
        for sta, (slat, slon) in all_stations.items():
            if sta in triggered_names:
                continue
            dist = haversine(epi_lat, epi_lon, slat, slon)
            popup_html = (
                f"<b>{'⚠️ 未觸發' if dist <= radius_km else '📍'} {sta}</b><br>"
                f"距震央：{dist:.1f} km<br>"
                f"{'⚠️ 在 ' + str(radius_km) + ' km 內但未觸發' if dist <= radius_km else '半徑外測站'}"
            )
            if dist <= radius_km:
                folium.CircleMarker(
                    location=[slat, slon],
                    radius=7,
                    color="#e65c00",
                    fill=True,
                    fill_color="#ff8c00",
                    fill_opacity=0.4,
                    weight=2,
                    popup=folium.Popup(popup_html, max_width=200),
                    tooltip=f"⚠️ {sta}  未觸發  {dist:.1f} km",
                ).add_to(grp_missed)
                folium.PolyLine(
                    locations=[[epi_lat, epi_lon], [slat, slon]],
                    color="#e65c00", weight=1, opacity=0.25, dash_array="4",
                ).add_to(grp_missed)
            else:
                folium.CircleMarker(
                    location=[slat, slon],
                    radius=4,
                    color="#999999",
                    fill=True,
                    fill_color="#cccccc",
                    fill_opacity=0.5,
                    weight=1,
                    popup=folium.Popup(popup_html, max_width=200),
                    tooltip=f"{sta}  {dist:.1f} km",
                ).add_to(grp_outside)

    grp_triggered.add_to(m)
    grp_missed.add_to(m)
    grp_outside.add_to(m)
    folium.LayerControl(collapsed=False).add_to(m)

    # ---- Legend ------------------------------------------------------------
    # 分母：sta_hisn_Z 中距震央 <= radius_km 的測站
    avail_set = {
        sta for sta, (slat, slon) in (all_stations or {}).items()
        if haversine(epi_lat, epi_lon, slat, slon) <= radius_km
    }
    # 分子：.rep 末報中、站名在 avail_set 內的測站
    n_trig_in_radius = sum(1 for s in stations if s["sta"] in avail_set)
    n_avail          = len(avail_set)
    n_missed_count   = n_avail - n_trig_in_radius
    rate_str = (f"{n_trig_in_radius/n_avail:.0%}" if n_avail > 0 else "N/A")

    legend_html = f"""
    <div style="position:fixed;bottom:30px;left:30px;z-index:1000;
                background:white;padding:12px 16px;border:1px solid #ccc;
                border-radius:6px;font-size:12px;line-height:2em;min-width:220px">
      <b>Peak displacement Pd (cm)</b><br>
      <div style="background:linear-gradient(to right,#0000ff,#00c8c8,#00c800,#ffc800,#ff0000);
                  width:160px;height:12px;border-radius:3px;margin:4px 0"></div>
      <div style="display:flex;justify-content:space-between;width:160px;font-size:10px">
        <span>{pd_min:.4f}</span><span>{pd_max:.4f}</span>
      </div>
      <span style="color:#aaa">●</span> Pd=0（無量測）<br>
      <hr style="margin:4px 0">
      <b>觸發率（{radius_km:.0f} km 半徑內）</b><br>
      <span style="color:#1a7abf">●</span> 觸發：{n_trig_in_radius} 站<br>
      <span style="color:#e65c00">●</span> 未觸發：{n_missed_count} 站<br>
      <b>觸發率 = {n_trig_in_radius} / {n_avail} = {rate_str}</b><br>
      <hr style="margin:4px 0">
      <span style="color:red">★</span> 震央 M{hypo['Mall']}  深={hypo['dep']} km<br>
      <span style="color:#555">- - -</span> {radius_km:.0f} km 範圍
    </div>
    """
    m.get_root().html.add_child(folium.Element(legend_html))

    m.save(out_html)
    print(f"  [map] 已儲存 → {out_html}")

    if open_browser:
        webbrowser.open("file://" + os.path.abspath(out_html))

    return out_html


# ---------------------------------------------------------------------------
# Alarm & watcher
# ---------------------------------------------------------------------------

def alarm(filepath, plot=False, open_browser=False, all_stations=None, radius_km=DEFAULT_RADIUS_KM):
    """Print a visible alarm with disaster prevention advice; optionally generate a map."""
    name = os.path.basename(filepath)
    try:
        data    = parse_rep_data(filepath)
        summary = format_summary(data)
        advice  = generate_advice(data)
    except Exception as e:
        data    = None
        summary = f"  (could not parse: {e})"
        advice  = ""

    BOLD  = "\033[1m"
    RESET = "\033[0m"

    print("\n" + "=" * 60)
    print(f"{BOLD}🚨  NEW EEW REPORT DETECTED: {name}{RESET}")
    print("=" * 60)
    print(summary)
    if advice:
        print("-" * 60)
        print(advice)
    print("=" * 60)
    sys.stdout.write("\a")
    sys.stdout.flush()

    if plot and data:
        plot_map(filepath, data, open_browser=open_browser,
                 all_stations=all_stations, radius_km=radius_km)


def watch(directory, plot=False, open_browser=False, radius_km=DEFAULT_RADIUS_KM):
    if not os.path.isdir(directory):
        print(f"ERROR: directory not found: {directory}")
        sys.exit(1)

    # Load station file once at startup
    sta_file     = os.path.join(directory, "sta_hisn_Z")
    all_stations = load_station_file(sta_file)
    if all_stations:
        print(f"測站檔    : {sta_file} ({len(all_stations)} 站)")
    else:
        print(f"測站檔    : 未找到 {sta_file}（觸發率圖層停用）")

    print(f"Watching  : {directory}")
    print(f"Map output: {'enabled' if plot else 'disabled'}  "
          f"(auto-open: {'yes' if open_browser else 'no'})")
    print(f"觸發率半徑: {radius_km} km")
    print(f"Interval  : {POLL_INTERVAL}s  |  Press Ctrl-C to stop\n")

    seen = set(f for f in os.listdir(directory) if f.endswith(".rep"))
    print(f"Existing .rep files at startup: {len(seen)}")

    try:
        while True:
            time.sleep(POLL_INTERVAL)
            current = set(f for f in os.listdir(directory) if f.endswith(".rep"))
            for fname in sorted(current - seen):
                alarm(os.path.join(directory, fname),
                      plot=plot, open_browser=open_browser,
                      all_stations=all_stations, radius_km=radius_km)
            seen = current
    except KeyboardInterrupt:
        print("\nStopped.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Watch for new EEW .rep files and optionally plot them on a map."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=WATCH_DIR,
        help=f"Directory to watch (default: {WATCH_DIR})",
    )
    parser.add_argument(
        "--map", "-m",
        action="store_true",
        help="Generate an interactive folium HTML map for each new report",
    )
    parser.add_argument(
        "--open", "-o",
        action="store_true",
        help="Auto-open the map in the default browser (implies --map)",
    )
    parser.add_argument(
        "--radius", "-r",
        type=float,
        default=DEFAULT_RADIUS_KM,
        help=f"觸發率圓圈半徑 km（預設 {DEFAULT_RADIUS_KM}）",
    )
    args = parser.parse_args()

    watch(
        directory=args.directory,
        plot=args.map or args.open,
        open_browser=args.open,
        radius_km=args.radius,
    )
