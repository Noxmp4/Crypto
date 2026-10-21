"""Graphique en lignes rendu côté serveur (SVG inline, sans bibliothèque JS).

Règles (skill dataviz) : lignes de 2 px, une seule échelle Y, couleurs par entité
(slot fixe par poche, jamais par rang), légende + étiquettes en bout de ligne,
grille en filet discret, réticule + infobulle au survol (static/app.js), et une
vue tableau pour l'accessibilité. Les couleurs viennent des variables CSS --s1…--s3.
"""
from __future__ import annotations

import json
import math
from datetime import datetime

from markupsafe import Markup, escape

# Slot fixe par poche : la couleur suit l'entité, pas son rang.
SLOTS = {"total": 1, "trend": 2, "dca": 3}
LABELS = {"total": "Total", "trend": "Tendance", "dca": "DCA (valeur)"}


def eur(v: float, compact: bool = False) -> str:
    if compact:
        if abs(v) >= 1000:
            return f"{v / 1000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " k€"
        return f"{v:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " €"
    return f"{v:,.2f} €".replace(",", " ").replace(".", ",")


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=mag * 10)
    ticks = [math.floor(lo / step) * step]
    while ticks[-1] < hi:                       # le dernier repère couvre toujours le maximum
        ticks.append(round(ticks[-1] + step, 10))
    if len(ticks) == 1:
        ticks.append(ticks[0] + step)
    return ticks


def line_chart(series: dict[str, list[tuple[str, float]]], title: str,
               width: int = 760, height: int = 260) -> Markup:
    """`series` = {poche: [(iso, valeur), …]}. Renvoie le bloc HTML complet."""
    series = {k: v for k, v in series.items() if len(v) >= 2}
    if not series:
        return Markup('<p class="empty">Pas encore assez de points pour tracer une courbe '
                      '(un point par heure).</p>')
    pad_l, pad_r, pad_t, pad_b = 56, 150, 12, 28
    xs = sorted({t for pts in series.values() for t, _ in pts})
    t0, t1 = datetime.fromisoformat(xs[0].replace("Z", "+00:00")), datetime.fromisoformat(
        xs[-1].replace("Z", "+00:00"))
    span = max((t1 - t0).total_seconds(), 1)
    vals = [v for pts in series.values() for _, v in pts]
    ticks = _nice_ticks(min(vals), max(vals))
    lo, hi = ticks[0], ticks[-1]
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(t: str) -> float:
        dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        return pad_l + (dt - t0).total_seconds() / span * w

    def y(v: float) -> float:
        return pad_t + (1 - (v - lo) / ((hi - lo) or 1)) * h

    parts = [f'<svg class="chart-svg" viewBox="0 0 {width} {height}" role="img" '
             f'aria-label="{escape(title)}">']
    for tv in ticks:
        parts.append(f'<line class="grid" x1="{pad_l}" x2="{pad_l + w}" y1="{y(tv):.1f}" '
                     f'y2="{y(tv):.1f}"/>')
        parts.append(f'<text class="tick" x="{pad_l - 8}" y="{y(tv) + 4:.1f}" '
                     f'text-anchor="end">{escape(eur(tv, compact=True))}</text>')
    for k in range(4):
        t = t0.timestamp() + span * k / 3
        tx = pad_l + w * k / 3
        label = datetime.fromtimestamp(t).strftime("%d/%m")
        parts.append(f'<text class="tick" x="{tx:.1f}" y="{height - 8}" '
                     f'text-anchor="middle">{label}</text>')
    data = {"x0": pad_l, "w": w, "t0": t0.timestamp(), "span": span, "top": pad_t, "h": h,
            "series": []}
    for name, pts in series.items():
        slot = SLOTS.get(name, 1)
        d = " ".join(f"{'M' if i == 0 else 'L'}{x(t):.1f},{y(v):.1f}" for i, (t, v) in enumerate(pts))
        parts.append(f'<path class="line s{slot}" d="{d}"/>')
        lx, ly = x(pts[-1][0]), y(pts[-1][1])
        parts.append(f'<circle class="dot s{slot}" cx="{lx:.1f}" cy="{ly:.1f}" r="4"/>')
        parts.append(f'<text class="end-label" x="{lx + 8:.1f}" y="{ly + 4:.1f}">'
                     f'{escape(LABELS.get(name, name))} {escape(eur(pts[-1][1], compact=True))}</text>')
        data["series"].append({"name": LABELS.get(name, name), "slot": slot,
                               "pts": [[datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp(), v]
                                       for t, v in pts]})
    parts.append(f'<line class="crosshair" x1="0" x2="0" y1="{pad_t}" y2="{pad_t + h}"/>')
    parts.append("</svg>")
    legend = "".join(f'<span class="key"><span class="swatch-line s{SLOTS.get(n, 1)}"></span>'
                     f'{escape(LABELS.get(n, n))}</span>' for n in series) if len(series) > 1 else ""
    rows = "".join(
        f"<tr><td>{escape(LABELS.get(n, n))}</td><td>{escape(pts[0][0][:10])}</td>"
        f"<td>{escape(eur(pts[0][1]))}</td><td>{escape(pts[-1][0][:16].replace('T', ' '))}</td>"
        f"<td>{escape(eur(pts[-1][1]))}</td></tr>" for n, pts in series.items())
    # Contenu brut d'un <script> : pas d'échappement HTML (il ne serait pas décodé),
    # mais neutralisation de « < > & » pour qu'aucune donnée ne puisse fermer la balise.
    payload = (json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e")
               .replace("&", "\\u0026"))
    return Markup(
        f'<figure class="chart"><figcaption>{escape(title)}</figcaption>'
        f'<div class="legend">{legend}</div><div class="plot">{"".join(parts)}'
        f'<div class="tooltip" hidden></div></div>'
        f'<script type="application/json" class="chart-data">{payload}</script>'
        f'<details class="table-view"><summary>Voir en tableau</summary><table>'
        f'<thead><tr><th>Série</th><th>Début</th><th>Valeur</th><th>Dernier point</th>'
        f'<th>Valeur</th></tr></thead><tbody>{rows}</tbody></table></details></figure>')
