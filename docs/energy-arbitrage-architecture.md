# Energy Arbitrage — архитектурная схема

> **Просмотр диаграмм**
> - **Браузер (рекомендуется):** откройте [`energy-arbitrage-architecture.html`](energy-arbitrage-architecture.html) в Chrome/Edge (двойной клик или drag-and-drop). После правок MD: `python scripts/build-architecture-html.py`.
> - **Cursor preview MD:** `Ctrl+Shift+V` + расширение **Markdown Preview Mermaid Support** (`bierner.markdown-mermaid`) + Reload Window. В workspace включено `"markdown.mermaid.enabled": true`.

Документ описывает логику вкладки **Energy arbitrage**: pipeline симуляции, классификацию часов, unified q15 sim chain и карты состояний.

Ключевые модули: `plan_simulation.py`, `simulation.py`, `plan_hourly_actuals.py`, `plan_optimizer.py`, `forecast_cache.py`.

---

## 1. Контекст и границы системы

```mermaid
flowchart TB
    subgraph External["Внешние системы"]
        SA[Solar Assistant MQTT]
        IFX[InfluxDB 10m/1h]
        OM[Open-Meteo 15min]
        PSE[PSE RCE]
        UI[Browser EA tab]
    end

    subgraph Smart["Solar Smart"]
        API["/api/simulation"]
        PS[plan_simulation]
        SIM[simulation.run_simulation]
        OPT[plan_optimizer DP q15]
        PHA[plan_hourly_actuals]
        FC[forecast_cache]
    end

    UI -->|refresh=0/1| API
    API --> PS
    PS -->|forecast, metrics, rules, rce| SIM
    SIM --> OPT
    SIM --> PHA
    FC --> PS
    SA --> IFX
    OM --> FC
    IFX --> PS
    PSE --> PS
```

**Ответственность слоёв:**

| Слой | Роль |
|------|------|
| `plan_simulation` | Оркестрация, in-memory cache, триггеры (:00/:15/:30/:45) |
| `simulation` | Сборка horizon rows, связка optimizer + blend + replay |
| `plan_optimizer` | DP по q15: grid charge / battery export, минимум G12 cost |
| `plan_hourly_actuals` | Факты, blend текущего часа, physics sim, forward replay |
| `forecast_cache` | File cache PV/Load, Open-Meteo refresh, overrides |

---

## 2. Главный pipeline (end-to-end)

```mermaid
flowchart TD
    START([Триггер]) --> CACHE{Plan cache<br/>свежий?}
    CACHE -->|да, refresh=0| SERVE[Отдать _cache]
    CACHE -->|нет / refresh=1| INPUTS

    subgraph INPUTS["fetch_plan_inputs (parallel)"]
        M[Live metrics + SOC]
        R[SA timer rules]
        RC[RCE 15min]
        H[Influx today hourly + 10m series]
        F[Forecast file cache<br/>PV q15 + Load q15]
    end

    INPUTS --> RS[run_simulation]

    subgraph RS["run_simulation"]
        direction TB
        M1[merge_today_hourly_profile<br/>факт до H + прогноз]
        M2[run_today_smart_q15_plan<br/>DP optimizer]
        M3[apply_current_hour_blend<br/>patch hourly H]
        M4[hourly_profile_to_q15<br/>merged → 96 slots]
        M5[Build rows H..23 today]
        M6{h == plan_from_hour?}
        M7[build_blended_current_hour_q15<br/>+ sync_blended_current_hour_row]
        M8[replay_forward_soc_on_rows<br/>H+1 .. horizon]
        M9[build_completed_history_rows<br/>0 .. H-1]
        M10[derive_timer_schedule_q15]

        M1 --> M2 --> M3 --> M4
        M4 --> M5
        M5 --> M6
        M6 -->|да| M7
        M6 -->|нет| M5
        M7 --> M5
        M5 --> M8
        M8 --> M9 --> M10
    end

    RS --> STORE[Сохранить _cache]
    STORE --> SERVE
    SERVE --> UI2[renderSimulationView]
```

**Триггеры пересчёта:**

- Scheduler каждые 15 мин (`:00/:15/:30/:45`)
- UI: кнопка Refresh → `GET /api/simulation?refresh=1`
- Config/EV change → `invalidate_plan_cache` + `force_refresh`

---

## 3. Классификация часов (temporal model)

Horizon = `plan_from_hour` (текущий час `:00`) + `horizon_hours` (обычно 24–48).

```mermaid
flowchart LR
    subgraph Today["Сегодня"]
        PAST["0 .. H-1<br/>COMPLETED"]
        CURR["H<br/>IN-PROGRESS"]
        FUT["H+1 .. 23<br/>PLANNED"]
    end

    subgraph Tomorrow["Завтра"]
        TOM["0 .. need_tomorrow<br/>PLANNED"]
        REM["остаток<br/>uncalculated UI only"]
    end

    PAST -->|Influx факт| HR[history_rows]
    CURR -->|blend q15 + sim| BR[rows H, soc_blended=true]
    FUT -->|merged q15 + replay sim| FR[rows H+1..]
    TOM --> FR
    REM -->|PV/Load/price only| TR[tomorrow_remainder_rows]
```

| Сегмент | PV/Load | Battery/Grid/SOC | Optimizer controls |
|---------|---------|------------------|-------------------|
| **Past** `0..H-1` | Influx hourly | Influx факт | Retro timer schedule |
| **Current** `H` | Per-q15 blend (10m + OM q15) | `simulate_blended_current_hour_q15` | DP slots + sim physics |
| **Future today/tomorrow** | merged hourly ÷ 4 | `replay_forward_soc` | DP slots + sim physics |
| **Tomorrow remainder** | forecast hourly | `null` (нет sim) | — |

---

## 4. Unified sim chain (ядро EA)

```mermaid
flowchart LR
    subgraph Inputs["На вход sim"]
        PV[pv_by_q 4 slots]
        LD[load_by_q 4 slots]
        CTRL[optimizer HourControl<br/>grid_charge_kw, export_kwh]
        SOC0[soc_start_kwh]
    end

    subgraph Chain["simulate_blended_current_hour_q15"]
        Q0["q0: БД или sim"]
        Q1["q1: БД или sim"]
        Q2["q2: БД или sim"]
        Q3["q3: sim"]
    end

    SOC0 --> Q0
    PV --> Q0
    LD --> Q0
    CTRL --> Q0
    Q0 -->|soc_end| Q1 --> Q2 --> Q3
    Q3 --> SOC1[soc_end hour]

    Chain --> ROW[q15 row + hourly sums<br/>bat/grid/soc]
```

**Anchor SOC текущего часа:** end of hour `H-1` из Influx (`_plan_start_soc_kwh`), не live MQTT SOC.

**Anchor для future replay:** end SOC blended current hour (`blended_anchor_kwh`).

---

## 5. Blend текущего часа — dataflow

```mermaid
flowchart TD
    NOW[now + series_10min Influx] --> SLOT[blended_q15_pv_load_slots<br/>4 × PV, Load]

    OM[pv_forecast_q15 / load_q15<br/>Open-Meteo + weekday load] --> SLOT
    MERGED[pv_merged H / load_merged H] --> SLOT

    SLOT --> SIM[simulate_blended_current_hour_q15]
    OPT[optimizer opt_slots hour H] --> SIM
    ANCHOR[plan_start_soc_kwh] --> SIM

    SIM --> SYNC[sync_blended_current_hour_row]
    SYNC --> DISPLAY[production, consumption, soc<br/>soc_blended=true]
```

Обновление на каждом refresh `:00/:15/:30/:45` (текущий час **H**).  
Индексы **q0–q3** = четверти `:15`, `:30`, `:45`, `:00` (конец часа).  
**frozen** = значение не пересчитывается (детерминировано из БД на прошлом refresh).  
**прогноз** = Open-Meteo q15 (load — weekday profile).  
**БД** = Influx 10-min, те же окна, что и для PV blend.  
**sim** = `simulate_hour` по прогнозным PV/load + optimizer controls.

### PV / Load (q)

| Refresh | q0 (:15) | q1 (:30) | q2 (:45) | q3 (:00) |
|---------|----------|----------|----------|----------|
| **:00–:14** | прогноз | прогноз | прогноз | прогноз |
| **:15** | **БД** (10 мин × 1.5) | прогноз | прогноз | прогноз |
| **:30** | frozen | **БД** | прогноз | прогноз |
| **:45** | frozen | frozen | **БД** | прогноз |
| **:00** след. часа | — | — | — | час **H** → `history_rows` (полный факт) |

### Battery / Grid (n)

`battery` = charge − discharge; `grid_import` / `grid_export` — из тех же q15-окон Influx.

| Refresh | q0 | q1 | q2 | q3 |
|---------|----|----|----|-----|
| **:00–:14** | sim | sim | sim | sim |
| **:15** | **БД** | sim | sim | sim |
| **:30** | frozen | **БД** | sim | sim |
| **:45** | frozen | frozen | **БД** | sim |
| **:00** след. часа | — | — | — | час **H** → `history_rows` |

### SOC (m)

Якорь: **SOC конца часа H−1** из Influx (`plan_start_soc_kwh`).  
На **БД-слотах**: `soc += battery_delta` (факт из Influx).  
На **sim-слотах**: `soc_end` из `simulate_hour` (physics + optimizer).

| Refresh | m0 (SOC @ :15) | m1 (@ :30) | m2 (@ :45) | m3 (@ :00) |
|---------|----------------|------------|------------|------------|
| **:00–:14** | sim(q0) | sim(q1) | sim(q2) | sim(q3) |
| **:15** | anchor + **Δ БД q0** | sim(q1) | sim(q2) | sim(q3) |
| **:30** | frozen | m0 + **Δ БД q1** | sim(q2) | sim(q3) |
| **:45** | frozen | frozen | m1 + **Δ БД q2** | sim(q3) |
| **:00** след. часа | — | — | — | час **H** → факт Influx |

### Будущие часы (H+1 … horizon)

| | PV / Load | Battery / Grid | SOC |
|---|-----------|----------------|-----|
| Все q15 | merged hourly ÷ 4 | **sim** (прогноз) | цепочка от `blended_anchor_kwh` |

Функция: `replay_forward_soc_on_rows` → `simulate_q15_slots` (без blend, без Influx).

---

## 6. Optimizer vs Physics (разделение ответственности)

```mermaid
flowchart TB
    subgraph Optimizer["plan_optimizer (DP)"]
        IN1[PV/Load hourly merged]
        IN2[RCE q15, G12 tariffs]
        IN3[day_start_soc, live_soc]
        OUT[ q15_by_hour:<br/>grid_charge_kw,<br/>battery_export_kwh,<br/>reserve, soc_pct plan ]
    end

    subgraph Physics["simulate_hour (physics)"]
        IN4[blended/merged PV/Load q15]
        IN5[HourControl from optimizer]
        OUT2[ battery_delta,<br/>grid_import/export,<br/>soc_end ]
    end

    Optimizer -->|controls only| Physics
    Physics -->|не меняет| Optimizer
```

Optimizer решает **что делать** (charge/export). Physics считает **что получится** (SOC, сети, потери η).

---

# Карты состояний

## A. Состояние plan cache

```mermaid
stateDiagram-v2
    [*] --> Empty: старт / invalidate

    Empty --> Stale: первый запрос
    Stale --> Computing: build_plan_simulation

    Computing --> Fresh: run_simulation OK
    Computing --> Stale: exception

    Fresh --> Fresh: API refresh=0<br/>cache not stale
    Fresh --> Computing: scheduler :15<br/>или refresh=1<br/>или config change

    note right of Fresh
        _cache содержит:
        rows, history_rows,
        proposed_schedule,
        plan_soc_q15, totals
    end note
```

---

## B. Состояние часовой строки (row lifecycle)

```mermaid
stateDiagram-v2
    [*] --> Completed: hour < plan_from_hour

    [*] --> BlendedCurrent: hour == plan_from_hour
    [*] --> ForecastReplay: hour > plan_from_hour
    [*] --> Uncalculated: tomorrow remainder

    Completed --> Completed: только Influx<br/>build_completed_history_rows
    note right of Completed
        q15 = equal split display
        SOC/bat/grid = факт
        без forward sim
    end note

    BlendedCurrent --> BlendedCurrent: каждые 15 min refresh
    note right of BlendedCurrent
        q15 slots: blend + sim chain
        soc_blended = true
        anchor для replay
    end note

    ForecastReplay --> ForecastReplay: refresh пересчитывает<br/>от blended anchor
    note right of ForecastReplay
        PV/Load: merged hourly→q15
        SOC: replay_forward_soc_on_rows
    end note

    Uncalculated --> Uncalculated
    note right of Uncalculated
        PV/Load/price only
        battery/soc = null
    end note
```

---

## C. Q15-слот текущего часа (blend FSM)

Зависит от `(now.minute, now.hour == H)`:

```mermaid
stateDiagram-v2
    direction LR
    [*] --> AllForecast: :00-:14
    AllForecast --> Slot0Live: :15
    Slot0Live --> Slot1Live: :30
    Slot1Live --> Slot2Live: :45
    Slot2Live --> [*]: hour ends

    note right of AllForecast
        q0-q3 forecast (OM q15)
    end note
    note right of Slot0Live
        q0 actual, q1-q3 forecast
    end note
    note right of Slot1Live
        q0-q1 actual, q2-q3 forecast
    end note
    note right of Slot2Live
        q0-q2 actual, q3 forecast
    end note
```

| `refresh_idx` | Frozen (actual) | Tail (forecast) |
|---------------|-----------------|-----------------|
| -1 (:00–:14) | — | q0–q3 |
| 0 (:15–:29) | q0 | q1–q3 |
| 1 (:30–:44) | q0–q1 | q2–q3 |
| 2 (:45–:59) | q0–q2 | q3 |

---

## D. Источник SOC для отображения

```mermaid
stateDiagram-v2
    [*] --> CheckHour

    CheckHour --> InfluxSOC: hour < H
    CheckHour --> SimBlendedSOC: hour == H
    CheckHour --> ReplaySOC: hour > H

    InfluxSOC: hourly.soc from Influx
    SimBlendedSOC: q15 chain from plan_start_soc
    ReplaySOC: chain from blended_anchor_kwh

    note right of InfluxSOC
        Прошлое — факт
    end note
    note right of SimBlendedSOC
        Текущий — unified sim
    end note
    note right of ReplaySOC
        Будущее — forward replay
        не optimizer soc_pct напрямую
    end note
```

---

## E. Open-Meteo PV cache

```mermaid
stateDiagram-v2
    [*] --> FileFresh: forecast_cache.json<br/>effective_q15 populated

    FileFresh --> Fetching: refresh_intraday_pv<br/>scheduler :00

    Fetching --> FileFresh: OM OK → update cache
    Fetching --> StaleServe: OM failed

    StaleServe --> StaleServe: log "OM fetch failed,<br/>serving stale cache"
    StaleServe --> Fetching: next :00 retry

    note right of StaleServe
        File cache NOT overwritten
        simulation uses last good q15
    end note
```

---

## F. Smart Mode × Scheduler (побочный контур)

```mermaid
stateDiagram-v2
    [*] --> PlanOnly: smart_mode=false

    PlanOnly --> PlanOnly: q15 refresh → EA table + timer preview

    PlanOnly --> SmartActive: smart_mode=true

    SmartActive --> SmartActive: :15/:30/:45 plan refresh
    SmartActive --> HourBoundary: :00 + smart on

    HourBoundary --> ApplyTimer: sync timer from row H+1
    ApplyTimer --> SmartActive

    note right of HourBoundary
        hour_boundary_scheduler
        не меняет EA sim logic
    end note
```

---

## 7. Выход API → UI

```mermaid
flowchart LR
    API["/api/simulation"] --> T1[history_rows<br/>свёрнутая PROD таблица]
    API --> T2[rows<br/>H .. horizon]
    API --> T3[tomorrow_remainder_rows]
    API --> T4[proposed_schedule q15]
    API --> T5[totals = history + today plan]

    T2 --> CHART[EA chart + q15 drill-down]
    T1 --> CHART
    T4 --> TIMER[Timer Schedule Apply]
```

---

## Ключевые инварианты

1. **Один physics engine** — `simulate_blended_current_hour_q15` (текущий час) и `simulate_q15_slots` (future replay); past — Influx.
2. **Optimizer отделён от display SOC** — future SOC идёт через replay, не через «rebase» optimizer SOC.
3. **Merged hourly — источник для future q15** — `hourly_profile_to_q15`, не сырой OM q15 в replay.
4. **Current hour — исключение** — per-slot blend (10m + OM q15) для точности внутри часа.
5. **Plan cache in-memory** — UI читает snapshot; Refresh = полный recompute pipeline.

---

## Связанные файлы

| Файл | Назначение |
|------|------------|
| `src/plan_simulation.py` | Cache, `build_plan_simulation`, scheduler hook |
| `src/simulation.py` | `run_simulation`, сборка rows |
| `src/plan_hourly_actuals.py` | Blend, sim chain, replay, history |
| `src/plan_optimizer.py` | DP q15 optimizer |
| `src/forecast_cache.py` | File cache, OM refresh, load profile |
| `src/routes/data.py` | `GET /api/simulation` |
| `src/templates/index.html` | EA UI, Refresh, charts |
