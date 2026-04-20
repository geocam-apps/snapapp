"use strict";

const PROJECT_ID = window.PROJECT_ID;

const selected = new Set();

async function loadProject() {
  const r = await fetch(`/api/projects/${PROJECT_ID}`);
  if (!r.ok) {
    document.querySelector("main").innerHTML = `<div style="padding:20px;color:#991b1b">
      Failed to load project.</div>`;
    return;
  }
  const p = await r.json();
  document.getElementById("projTitle").textContent = p.name;
  document.getElementById("projTag").textContent = `${p.kind} • ${p.photos.length} photos`;

  renderShots(p.shots || []);
  renderPhotos(p.photos || []);
  if (p.kind === "sqlite" && p.sqlite) {
    renderSqlite(p.sqlite);
  }
}

function renderShots(shots) {
  const host = document.getElementById("shotsList");
  host.innerHTML = "";
  if (!shots.length) {
    host.innerHTML = `<div style="color:#6b7280;font-size:13px">No shots yet.</div>`;
    return;
  }
  for (const s of shots) {
    const el = document.createElement("div");
    el.className = "shot";
    el.dataset.shotId = s.id;

    const statusLine = formatStatus(s);
    const phaseLabel = s.phase_label || s.phase || "";
    const progress = Math.round((s.progress || 0) * 100);

    const meta = s.meta || {};
    const metaBits = [];
    if (meta.n_photos) metaBits.push(`${meta.n_photos} photos`);
    if (meta.anchor_stem) metaBits.push(`anchor: ${meta.anchor_stem}`);
    if (s.anchor_score != null) metaBits.push(`score ${s.anchor_score.toFixed(3)}`);
    if (s.n_registered != null && s.n_queries != null) metaBits.push(`${s.n_registered}/${s.n_queries} registered`);

    const canRun = s.status === "pending" || s.status === "failed";
    const canView = s.status === "done";
    const canDelete = s.status !== "running";

    el.innerHTML = `
      <div class="top">
        <div>
          <h3>${escapeHtml(s.name)}</h3>
          <div class="meta">${metaBits.join(" • ")}</div>
          <div class="meta status-${s.status}">${statusLine}</div>
          ${s.status === "running" || (s.progress && s.progress < 1)
            ? `<div class="progress" style="margin-top:6px"><div style="width:${progress}%"></div></div>
               <div class="meta">${escapeHtml(phaseLabel)} (${progress}%)</div>`
            : ""}
          ${s.error ? `<div class="meta" style="color:#991b1b">${escapeHtml(s.error)}</div>` : ""}
        </div>
        <div class="actions">
          ${canView ? `<a class="btn-view" href="/shot/${PROJECT_ID}/${s.id}">
             <button class="small">View 3D</button></a>` : ""}
          ${canRun ? `<button class="small btn-run" data-id="${s.id}">
             ${s.status === "failed" ? "Retry" : "Run"}</button>` : ""}
          <button class="small secondary btn-log" data-id="${s.id}">Log</button>
          ${canDelete ? `<button class="small secondary btn-del" data-id="${s.id}">✕</button>` : ""}
        </div>
      </div>
      <pre class="log" id="log-${s.id}"></pre>
    `;
    host.appendChild(el);
  }

  host.querySelectorAll(".btn-run").forEach(b => b.addEventListener("click", async () => {
    const id = b.dataset.id;
    b.disabled = true;
    await fetch(`/api/shots/${id}/run`, {method: "POST"});
    loadProject();
  }));
  host.querySelectorAll(".btn-del").forEach(b => b.addEventListener("click", async () => {
    const id = b.dataset.id;
    if (!confirm("Delete this shot?")) return;
    await fetch(`/api/shots/${id}`, {method: "DELETE"});
    loadProject();
  }));
  host.querySelectorAll(".btn-log").forEach(b => b.addEventListener("click", async () => {
    const id = b.dataset.id;
    const logEl = document.getElementById(`log-${id}`);
    if (logEl.classList.contains("open")) {
      logEl.classList.remove("open");
      return;
    }
    logEl.classList.add("open");
    await refreshLog(id);
  }));
}

async function refreshLog(id) {
  const el = document.getElementById(`log-${id}`);
  if (!el || !el.classList.contains("open")) return;
  const r = await fetch(`/api/shots/${id}/log?tail=400`);
  el.textContent = await r.text();
  el.scrollTop = el.scrollHeight;
}

function formatStatus(s) {
  switch (s.status) {
    case "pending": return "Pending";
    case "running": return `Running — ${s.phase_label || s.phase || ""}`;
    case "done": return "Done ✓";
    case "failed": return "Failed ✗";
    default: return s.status;
  }
}

function renderPhotos(photos) {
  const grid = document.getElementById("photosGrid");
  grid.innerHTML = "";
  if (!photos.length) {
    grid.innerHTML = `<div style="color:#6b7280;font-size:13px">No photos in this project.</div>`;
    return;
  }
  for (const name of photos) {
    const stem = name.replace(/\.[^/.]+$/, "");
    const th = document.createElement("div");
    th.className = "th";
    th.dataset.stem = stem;
    if (selected.has(stem)) th.classList.add("selected");
    th.innerHTML = `
      <img src="/api/projects/${PROJECT_ID}/photo/${encodeURIComponent(name)}" loading="lazy">
      <div class="lbl">${escapeHtml(name)}</div>
    `;
    th.addEventListener("click", () => {
      if (selected.has(stem)) {
        selected.delete(stem);
        th.classList.remove("selected");
      } else {
        selected.add(stem);
        th.classList.add("selected");
      }
    });
    grid.appendChild(th);
  }
}

function renderSqlite(info) {
  const sec = document.getElementById("sqliteSection");
  const body = document.getElementById("sqliteInfo");
  sec.style.display = "";
  if (!info.ok) {
    body.innerHTML = `<div style="color:#991b1b">Could not read SQLite: ${escapeHtml(info.error)}</div>`;
    return;
  }
  const sample = (info.poses_sample || []).slice(0, 6);
  const bounds = info.bounds
    ? `${info.bounds.lat_min.toFixed(5)}..${info.bounds.lat_max.toFixed(5)}, ${info.bounds.lon_min.toFixed(5)}..${info.bounds.lon_max.toFixed(5)}`
    : "n/a";
  body.innerHTML = `
    <div class="sqlite-summary">
      <div><span class="k">capture:</span> ${escapeHtml(info.capture_name || "(none)")}</div>
      <div><span class="k">tables:</span> ${(info.tables || []).join(", ")}</div>
      <div><span class="k">poses:</span> ${info.pose_count}</div>
      <div><span class="k">bounds:</span> ${bounds}</div>
      ${sample.length ? `<div><span class="k">sample poses:</span>
        <ul>${sample.map(p =>
          `<li>id=${p.id} (${p.lat && p.lat.toFixed(5)}, ${p.lon && p.lon.toFixed(5)})</li>`
        ).join("")}</ul></div>` : ""}
    </div>
    <div style="margin-top:10px; color:#6b7280; font-size:12px">
      Note: SQLite-only projects require companion imagery on disk to run shotmatch_pose.
      This build displays SQLite metadata only.
    </div>
  `;
}

document.getElementById("newShotBtn").addEventListener("click", async () => {
  if (selected.size === 0) {
    alert("Select some photos first (click thumbnails).");
    return;
  }
  const name = prompt("Name this shot:", `Shot ${new Date().toISOString().slice(11,19)}`);
  if (name === null) return;
  const r = await fetch(`/api/projects/${PROJECT_ID}/shots`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({name, photo_stems: [...selected]}),
  });
  if (!r.ok) {
    alert("Failed to create shot: " + await r.text());
    return;
  }
  selected.clear();
  loadProject();
});

document.getElementById("deleteProjectBtn").addEventListener("click", async () => {
  if (!confirm("Delete this entire project and all shots? This cannot be undone.")) return;
  await fetch(`/api/projects/${PROJECT_ID}`, {method: "DELETE"});
  location.href = "/";
});

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, m => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[m]);
}

loadProject();
// Poll every 2s so running shots update fast
setInterval(async () => {
  await loadProject();
  // Refresh any open logs
  document.querySelectorAll(".log.open").forEach(el => {
    const id = el.id.replace(/^log-/, "");
    refreshLog(id);
  });
}, 2000);
