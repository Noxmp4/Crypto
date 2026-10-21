// Réticule + infobulle des courbes. Les libellés sont insérés avec textContent.
(function () {
  const fmt = new Intl.NumberFormat("fr-FR", { style: "currency", currency: "EUR" });
  const dfmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });

  function nearest(pts, t) {
    let best = null;
    for (const p of pts) if (p[0] <= t) best = p;
    return best || pts[0];
  }

  document.querySelectorAll(".chart").forEach(function (fig) {
    const raw = fig.querySelector(".chart-data");
    const svg = fig.querySelector(".chart-svg");
    const tip = fig.querySelector(".tooltip");
    if (!raw || !svg || !tip) return;
    const d = JSON.parse(raw.textContent);
    const cross = svg.querySelector(".crosshair");
    const vb = svg.viewBox.baseVal;

    function show(evt) {
      const r = svg.getBoundingClientRect();
      const x = (evt.clientX - r.left) * (vb.width / r.width);
      const t = d.t0 + Math.min(Math.max((x - d.x0) / d.w, 0), 1) * d.span;
      const cx = d.x0 + (t - d.t0) / d.span * d.w;
      cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
      svg.classList.add("hover");
      tip.replaceChildren();
      const head = document.createElement("div");
      head.className = "t-date"; head.textContent = dfmt.format(new Date(t * 1000));
      tip.appendChild(head);
      d.series.forEach(function (s) {
        const p = nearest(s.pts, t);
        const row = document.createElement("div"); row.className = "t-row";
        const key = document.createElement("span"); key.className = "swatch-line s" + s.slot;
        const val = document.createElement("strong"); val.textContent = fmt.format(p[1]);
        const name = document.createElement("span"); name.textContent = s.name;
        row.append(key, val, name); tip.appendChild(row);
      });
      tip.hidden = false;
      const px = cx * (r.width / vb.width);
      tip.style.left = Math.min(px + 12, r.width - tip.offsetWidth - 4) + "px";
    }
    function hide() { svg.classList.remove("hover"); tip.hidden = true; }
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerleave", hide);
    svg.setAttribute("tabindex", "0");
    svg.addEventListener("focus", function () {
      const r = svg.getBoundingClientRect();
      show({ clientX: r.right - 1 });
    });
    svg.addEventListener("blur", hide);
  });
})();
