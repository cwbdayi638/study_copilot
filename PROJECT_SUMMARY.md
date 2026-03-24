# Earthworm EEW System — Project Summary

## Overview

This is an offline **Earthquake Early Warning (EEW)** system built on the [Earthworm](http://gitlab.com/seismic-software/earthworm/) seismic processing platform (v8.0), configured for macOS. It uses historical waveform replay (`tankplayer`) to simulate and test EEW algorithms developed by Dr. Da-Yi Chen at CWB (Central Weather Bureau), Taiwan.

---

## Data Flow

```
tankplayer (replay .tank files)
    ↓  TRACEBUF messages
  WAVE_RING  (shared memory)
    ↓
  pick_eew   →  picks + Pa/Pv/Pd amplitudes
    ↓  EEW_Pick messages
  PICK_RING  (shared memory)
    ↓
  tcpd       →  association, hypocenter, magnitude (iterative)
    ↓  EEW_Report messages
  EEW_RING   (shared memory)
    ↓
  dcsn_xml   →  XML EEW alerts
              →  .rep report files  →  run_eew/params/
```

---

## CWB EEW Modules

| Module | Binary | Purpose |
|--------|--------|---------|
| `pick_eew/` | `pick_ew` | P-wave picker; computes Pa, Pv, Pd within 3-sec P-wave window |
| `tcpd/` | `tcpd` | Associates picks, iteratively estimates hypocenter & magnitude |
| `dcsn_xml/` | `dcsn_xml` | Decision logic; outputs XML EEW alerts |

Source code: `earthworm_8.0/src/eew/cwb/code/`

---

## Python Scripts

### `watch_rep.py`
Monitors `run_eew/params/` for new `.rep` files and triggers an alarm when a new EEW report arrives. Optionally plots epicenter and triggered stations on an interactive HTML map.

**Usage:**
```bash
python3 watch_rep.py                      # watch, no map
python3 watch_rep.py --map                # watch + plot map
python3 watch_rep.py --map --open         # also auto-open map in browser
```

**Key features:**
- Real-time directory polling for new `.rep` files
- Alarm output with event details (origin time, lat/lon, depth, magnitude)
- Interactive Leaflet map with epicenter marker and station circles
- Haversine distance calculation for epicentral distances

---

### `analyze_rep.py`
Parses and analyzes `.rep` EEW report files. Can be used as a standalone CLI tool or imported as a skill module by AI agents.

**Usage:**
```bash
python3 analyze_rep.py run_eew/params/             # analyze all events
python3 analyze_rep.py run_eew/params/ --json      # JSON output (for AI)
python3 analyze_rep.py run_eew/params/ --event 2   # analyze event N only
```

**Key features:**
- Groups `.rep` files by event (timestamp prefix)
- Parses header, hypocenter, and per-station lines
- Computes solution quality metrics: azimuthal gap, RMS residual, magnitude spread
- Detects problematic stations (high residual, low weight, large distance)
- Outputs structured JSON for AI consumption

---

### `eew_agent.py`
GitHub Copilot CLI–driven continuous improvement agent for the EEW system. Watches for new events, runs diagnostics, and applies rule-based parameter adjustments to `tcpd.d`.

**Usage:**
```bash
python3 eew_agent.py                   # start agent, watch continuously
python3 eew_agent.py --dry-run         # simulate, no file changes
python3 eew_agent.py --review          # show items pending Copilot review
python3 eew_agent.py --history         # show improvement history
python3 eew_agent.py --analyze-only    # analyze existing events only
```

**Key features:**
- Waits for event `.rep` series to stabilize before analysis
- Rule engine auto-tunes `tcpd.d` parameters based on diagnostics
- Logs before/after parameter changes for traceability
- Flags complex issues for human (Copilot) review

---

## Shell Scripts

### `ew_macos_eew.sh`
Environment setup script. Must be sourced before running any Earthworm command.

```bash
source ew_macos_eew.sh
```

Sets: `EW_HOME`, `EW_VERSION`, `EW_PARAMS`, `EW_LOG`, `EW_DATA_DIR`, `PATH`, and compiler flags (`GLOBALFLAGS`, `CFLAGS`, `CPPFLAGS`, `LDFLAGS`, `PLATFORM=LINUX`).

### `monitor.sh`
Convenience script for monitoring Earthworm module status.

---

## `.rep` File Format

EEW reports are written to `run_eew/params/` with naming: `YYYYMMDDHHMMSS_nNN.rep`

**Header line:**
```
Reporting time <time>  averr=<avg_residual> Q=<quality> Gap=<gap_deg> Avg_wei=<weight> n=<triggered> n_c=<used_loc> n_m=<used_mag> no_eq=<seq>
```

**Hypocenter line:**
```
year month day hour min sec  lat  lon  dep  Mall  Mpd_s  Mpv  Mpd  Mtc  process_time  first_ptime
```

**Per-station lines:**
```
Sta C N L  lat  lon  pa  pv  pd  tc  Mtc  MPv  MPd  Perr  Dis  H_Wei  Parr  Pk_wei  Upd_sec  P_S  usd_sec
```

---

## Environment Setup & Operations

```bash
# Source environment (required before any Earthworm command)
source /Users/dayichen/Earthworm/ew_macos_eew.sh

# Check module status
status

# Restart a specific module by PID
restart <PID>
```

---

## Development Goals

1. **Python `.rep` watcher** — real-time alarm when new EEW reports arrive (`watch_rep.py` ✅)
2. **Automated diagnostics** — parse and score EEW solution quality (`analyze_rep.py` ✅)
3. **Self-improving agent** — rule-based + AI-assisted parameter tuning (`eew_agent.py` ✅)
4. **CWB EEW source improvements** — ongoing improvements to `pick_eew`, `tcpd`, `dcsn_xml`
