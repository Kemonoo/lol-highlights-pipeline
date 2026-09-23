// The funnel: one cell per clip fetched on the featured night. The clips that reached
// the GPU carry their real thumbnail and verdicts (assets/funnel.json, written by
// scripts/export_site.py from the pipeline's own work files). The rest never got a
// thumbnail, so they are plain cells that only know at which step they were dropped.
(function () {
  const grid = document.getElementById("grid");
  const note = document.getElementById("step-note");
  const detail = document.getElementById("detail");
  const tabs = [...document.querySelectorAll(".steps button")];
  let nightKept = 52;

  // step notes; the numbers come from assets/funnel.json ("night"), written by
  // scripts/export_site.py from that night's log
  const notes = (n) => [
    `<strong>${n.fetched} clips</strong> in that day's Twitch top list for League of Legends. So far only titles and numbers; nothing has been downloaded.`,
    `<strong>${n.fetched - n.scored} dropped</strong> because the channel's language tag isn't one the pipeline accepts. No download needed.`,
    `<strong>${n.scored - n.passed} dropped</strong> after a low-quality download: too quiet or too still to be a highlight.`,
    `<strong>${n.passed - n.kept} dropped</strong> by the cap. The rest are ranked by title keyword, then loudness, then views, and only the best ${n.kept} go to the GPU.`,
    `<strong>${n.kept - n.vlm_kept} rejected</strong> by the local vision model's answers: not gameplay, looked like a pro broadcast, or no kills and not loud enough.`,
    `<strong>${n.vlm_kept - n.final} dropped</strong> on Gemini's scores. The last ${n.final} become the countdown, from #${n.final} to #1.`,
  ];

  // seeded shuffle so the layout is stable between visits
  let seed = 21092026;
  const rnd = () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296);
  const shuffle = (a) => { for (let i = a.length - 1; i > 0; i--) { const j = Math.floor(rnd() * (i + 1)); [a[i], a[j]] = [a[j], a[i]]; } return a; };

  // Hero backdrop: the page's one orchestrated moment. The 52 examined clips light up
  // across the grid, then everything but the 17 that made the video fades out.
  function hero(clips) {
    const box = document.getElementById("hero-grid");
    if (!box) return;
    const size = window.innerWidth <= 720 ? 52 : 72;
    const cols = Math.floor(box.clientWidth / size), rows = Math.ceil(box.clientHeight / size);
    const slots = [];
    for (let i = 0; i < cols * rows; i++) {
      const s = document.createElement("span"), t = document.createElement("i");
      s.appendChild(t); box.appendChild(s);
      const x = (i % cols) / cols - 0.5, y = Math.floor(i / cols) / rows - 0.36;
      // inside the unmasked ellipse, but not behind the headline and lede
      const behindText = Math.abs(x) < 0.3 && y > -0.22 && y < 0.24;
      if (x * x / 0.21 + y * y / 0.13 < 1 && !behindText) slots.push(t);
    }
    const picks = shuffle(slots).slice(0, clips.length);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    picks.forEach((t, k) => {
      const c = clips[k];
      t.style.backgroundImage = `url(assets/clips/${String(c.i).padStart(2, "0")}.jpg)`;
      const keep = c.stage === 4;
      if (still) { if (keep) t.classList.add("keep"); return; }
      setTimeout(() => t.classList.add("on"), 250 + k * 28);
      setTimeout(() => { t.classList.remove("on"); if (keep) t.classList.add("keep"); }, 2600 + (keep ? 0 : k * 12));
    });
  }

  fetch("assets/funnel.json").then((r) => r.json()).then(({ night: n, clips }) => {
    nightKept = n.kept;
    hero(clips);
    const NOTES = notes(n);
    const cells = [];
    // dropAt = the first step at which this cell is no longer in the running. Only the
    // clips that reached the GPU have pictures; the rest are counted from the log.
    [[1, n.fetched - n.scored], [2, n.scored - n.passed], [3, n.passed - n.kept]].forEach(([at, n]) => { for (let i = 0; i < n; i++) cells.push({ dropAt: at }); });
    clips.forEach((c) => cells.push({ clip: c, dropAt: c.stage === 2 ? 4 : c.stage === 3 ? 5 : 99 }));
    shuffle(cells);

    const frag = document.createDocumentFragment();
    cells.forEach((cell) => {
      let el;
      if (cell.clip) {
        const c = cell.clip;
        el = document.createElement("button");
        el.type = "button";
        el.setAttribute("aria-pressed", "false");
        el.setAttribute("aria-label", `${c.who}: ${c.title}`);
        const img = document.createElement("img");
        img.src = `assets/clips/${String(c.i).padStart(2, "0")}.jpg`;
        img.alt = "";
        img.loading = "lazy";
        el.appendChild(img);
        el.addEventListener("click", () => select(el, c));
      } else {
        el = document.createElement("span");
        el.setAttribute("aria-hidden", "true");
      }
      el.classList.add("cell");
      cell.el = el;
      frag.appendChild(el);
    });
    grid.appendChild(frag);

    function setStep(s) {
      tabs.forEach((t) => t.setAttribute("aria-selected", String(+t.dataset.step === s)));
      cells.forEach((cell) => cell.el.classList.toggle("out", s >= cell.dropAt));
      note.innerHTML = NOTES[s];
    }
    tabs.forEach((t) => t.addEventListener("click", () => setStep(+t.dataset.step)));
    // arrow keys move between steps, like any tab list
    tabs.forEach((t, i) => t.addEventListener("keydown", (e) => {
      const d = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
      if (!d) return;
      const n = (i + d + tabs.length) % tabs.length;
      tabs[n].focus(); setStep(n);
    }));
    setStep(0);
  });

  const esc = (s) => String(s).replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

  function select(el, c) {
    grid.querySelectorAll('[aria-pressed="true"]').forEach((b) => b.setAttribute("aria-pressed", "false"));
    el.setAttribute("aria-pressed", "true");
    const row = (ok, text, sub) => `<li class="${ok ? "" : "no"}"><div>${text}${sub ? `<span>${sub}</span>` : ""}</div></li>`;
    const steps = [
      row(true, "Supported language"),
      row(true, c.audio < 0.22 ? "Quiet, but the title keyword let it through" : "Loud and moving enough", `audio ${c.audio}, motion ${c.motion}`),
      row(true, `Ranked into the top ${nightKept}`),
      row(c.stage >= 3, c.stage >= 3 ? "Kept by the local model" : "Rejected by the local model", esc(c.local)),
    ];
    if (c.stage >= 3) {
      steps.push(row(c.stage >= 4, c.stage >= 4 ? "Kept by Gemini" : "Dropped by Gemini",
        `entertainment ${c.ent}/10, play quality ${c.pq}/10, focus: ${esc(c.focus)}`));
    }
    detail.innerHTML =
      `<img src="assets/clips/${String(c.i).padStart(2, "0")}.jpg" alt="" width="240" height="135">` +
      `<p class="who">${esc(c.who)}${c.rank ? ` <span class="rank">#${c.rank}</span>` : ""}</p>` +
      `<p class="title">“${esc(c.title)}”</p>` +
      `<ol>${steps.join("")}</ol>`;
  }
})();
