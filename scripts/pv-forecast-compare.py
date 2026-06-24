"""Generate SVG: PV forecast smooth vs stepped (no smoothing)."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
data = json.loads((ROOT / ".tmp-forecast.json").read_text())
hourly = data["today"]["pv_forecast"] or data["today"]["pv"]
N = 96
SPH = 4
W, H, PL, PR, PT, PB = 700, 320, 48, 20, 28, 36


def smooth(hourly_vals: list) -> list[float]:
    out: list[float] = []
    for i in range(N):
        t = (i + 0.5) / SPH
        h0 = min(int(t), 23)
        h1 = min(h0 + 1, 23)
        frac = t - h0
        v0 = float(hourly_vals[h0] or 0)
        v1 = float(hourly_vals[h1] or 0)
        out.append(v0 + (v1 - v0) * frac)
    return out


def stepped(hourly_vals: list) -> list[float]:
    out: list[float] = []
    for h in range(24):
        kw = float(hourly_vals[h] if h < len(hourly_vals) else 0)
        out.extend([kw] * 4)
    return out


smooth_kw = smooth(hourly)
step_kw = stepped(hourly)
ymax = max(max(smooth_kw), max(step_kw)) * 1.08 or 1.0
plot_w = W - PL - PR
plot_h = H - PT - PB


def smooth_points(vals: list[float]) -> str:
    pts = []
    for i, v in enumerate(vals):
        x = PL + (i + 0.5) / N * plot_w
        y = PT + (1 - v / ymax) * plot_h
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def step_points(vals: list[float]) -> str:
    pts: list[str] = []
    for h in range(24):
        kw = vals[h * 4]
        x0 = PL + (h * 4) / N * plot_w
        x1 = PL + ((h + 1) * 4) / N * plot_w
        y = PT + (1 - kw / ymax) * plot_h
        if h == 0:
            pts.append(f"{x0:.1f},{y:.1f}")
        else:
            prev_y = pts[-1].split(",")[1]
            pts.append(f"{x0:.1f},{prev_y}")
        pts.append(f"{x0:.1f},{y:.1f}")
        pts.append(f"{x1:.1f},{y:.1f}")
    return " ".join(pts)


grid_parts: list[str] = []
for i in range(5):
    yv = ymax * i / 4
    y = PT + (1 - yv / ymax) * plot_h
    grid_parts.append(
        f'<line x1="{PL}" y1="{y:.1f}" x2="{W - PR}" y2="{y:.1f}" stroke="#e2e8f0" stroke-width="1"/>'
    )
    grid_parts.append(
        f'<text x="{PL - 6}" y="{y + 4:.1f}" text-anchor="end" font-size="11" fill="#64748b">{yv:.1f}</text>'
    )

xlabels = []
for hour in range(0, 25, 2):
    x = PL + hour / 24 * plot_w
    xlabels.append(
        f'<text x="{x:.1f}" y="{H - 10}" text-anchor="middle" font-size="11" fill="#64748b">{hour:02d}</text>'
    )

svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  <text x="{W / 2}" y="18" text-anchor="middle" font-size="14" font-weight="bold" fill="#0f172a">PV forecast today — with vs without interpolation</text>
  {''.join(grid_parts)}
  {''.join(xlabels)}
  <polyline fill="none" stroke="#f59e0b" stroke-width="2.5" opacity="0.95" points="{smooth_points(smooth_kw)}"/>
  <polyline fill="none" stroke="#dc2626" stroke-width="2" stroke-dasharray="8 5" opacity="0.95" points="{step_points(step_kw)}"/>
  <rect x="{PL}" y="{PT}" width="250" height="42" fill="white" fill-opacity="0.92" rx="6"/>
  <line x1="{PL + 12}" y1="{PT + 14}" x2="{PL + 42}" y2="{PT + 14}" stroke="#f59e0b" stroke-width="2.5"/>
  <text x="{PL + 50}" y="{PT + 18}" font-size="12" fill="#334155">With interpolation (current)</text>
  <line x1="{PL + 12}" y1="{PT + 32}" x2="{PL + 42}" y2="{PT + 32}" stroke="#dc2626" stroke-width="2" stroke-dasharray="8 5"/>
  <text x="{PL + 50}" y="{PT + 36}" font-size="12" fill="#334155">Without smoothing (hourly steps)</text>
  <text x="{PL}" y="{H - 22}" font-size="11" fill="#64748b">kW</text>
</svg>"""

out = ROOT / "assets" / "pv-forecast-smooth-vs-stepped.svg"
out.parent.mkdir(exist_ok=True)
out.write_text(svg, encoding="utf-8")
print(f"saved {out}")
print(f"smooth peak {max(smooth_kw):.2f} kW, stepped peak {max(step_kw):.2f} kW")
