"use strict";

const PROJECT_ID = window.PROJECT_ID;

let latestProject = null;
let references = [];                  // cached from /api/references
const selected = new Set();           // shot ids selected for "Run selected"
const openLogs = new Set();           // shot ids whose log pane is expanded
const logCache = new Map();           // shot_id -> last fetched log text
const logScrollState = new Map();     // shot_id -> {scrollTop, pinnedBottom}

async function loadReferences() {
  try {
    const r = await fetch("/api/references");
    const data = await r.json();
    references = data.items || [];
  } catch (e) {
    references = [];
  }
}
loadReferences();

async function loadProject() {
  const r = await fetch(`/api/projects/${PROJECT_ID}`);
  if (!r.ok) {
    document.querySelector("main").innerHTML =
      `<div style="padding:20px;color:#991b1b">Failed to load project.</div>`;
    return;
  }
  const p = await r.json();
  latestProject = p;
  document.getElementById("projTitle").textContent = p.name;
  const tag = [
    p.kind,
    `${(p.shots || []).length} shots`,
    p.megaloc_running ? `<span style="color:#1e40af">MegaLoc running…</span>`
                      : (p.megaloc_ready ? `<span style="color:#065f46">MegaLoc ready</span>`
                                         : `MegaLoc pending`),
  ];
  document.getElementById("projTag").innerHTML = tag.join(" • ");

  renderShotTiles(p);
  updateActions(p);

  if (p.kind === "sqlite") {
    renderSqlite(p.sqlite, p.meta || {});
  }
}

function renderShotTiles(p) {
  const host = document.getElementById("shotTiles");
  // Capture scroll state for any open logs before wiping the DOM
  for (const id of openLogs) {
    const el = document.getElementById(`log-${id}`);
    if (el) {
      const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      logScrollState.set(id, {scrollTop: el.scrollTop, pinnedBottom: pinned});
    }
  }
  host.innerHTML = "";

  const shots = p.shots || [];
  const matches = p.megaloc_matches || {};
  if (!shots.length) {
    host.innerHTML = `<div style="color:#6b7280;font-size:13px;padding:20px">
      No shots yet.</div>`;
    return;
  }

  for (const s of shots) {
    const meta = s.meta || {};
    const wide = meta.wide_filename;  // e.g. "shot_00001/wide.jpg"
    const shotKey = meta.shot_dir;     // "shot_00001"  ← MegaLoc match key
    const m = shotKey ? matches[shotKey] : null;
    const displayMatch = (m && m.gps_best) || m;

    const tile = document.createElement("div");
    tile.className = "shot-tile";
    if (selected.has(s.id)) tile.classList.add("selected");
    tile.dataset.shotId = s.id;

    const scoreBadge = displayMatch
      ? `<div class="score ${scoreClass(displayMatch.score)}"
              title="${escapeHtml(displayMatch.shot_key || "")}">
           ${displayMatch.score.toFixed(2)}
         </div>` : "";
    let gpsBadge = "";
    if (displayMatch && displayMatch.distance_m != null) {
      const d = displayMatch.distance_m;
      const cls = displayMatch.gps_valid ? "gps-ok" : "gps-far";
      const label = d < 1000 ? `${Math.round(d)} m` : `${(d/1000).toFixed(1)} km`;
      gpsBadge = `<div class="gps-dist ${cls}"
                       title="distance from phone GPS to ref shot">${label}</div>`;
    }

    const statusBadge = `<span class="pill status-${s.status}">${formatStatus(s)}</span>`;
    const phaseLabel = s.phase_label || s.phase || "";
    const progress = Math.round((s.progress || 0) * 100);
    const progressBar = (s.status === "running" || (s.progress > 0 && s.progress < 1))
      ? `<div class="progress"><div style="width:${progress}%"></div></div>
         <div class="meta phase-line">${escapeHtml(phaseLabel)} (${progress}%)</div>`
      : "";

    const reg = (s.n_registered != null && s.n_queries != null)
      ? `${s.n_registered}/${s.n_queries} registered` : "";

    const canView = s.status === "done";
    const canRun = s.status === "pending" || s.status === "failed";
    const canDelete = s.status !== "running";

    const photoSrc = wide
      ? `/api/projects/${PROJECT_ID}/photo/${encodeURI(wide)}`
      : "";

    tile.innerHTML = `
      <div class="thumb">
        ${photoSrc ? `<img src="${photoSrc}" loading="lazy">` : ""}
        ${scoreBadge}${gpsBadge}
        <div class="check"></div>
      </div>
      <div class="body">
        <div class="hdr">
          <span class="name">${escapeHtml(s.name)}</span>
          ${statusBadge}
        </div>
        <div class="meta">
          ${meta.n_burst != null ? `${(meta.photo_stems || []).length} photos` : ""}
          ${reg ? ` · ${reg}` : ""}
        </div>
        ${meta.lat != null ? `<div class="meta">${meta.lat.toFixed(5)}, ${meta.lon.toFixed(5)}${
          meta.bearing_deg != null ? ` · ${Math.round(meta.bearing_deg)}°` : ""}</div>` : ""}
        ${meta.reference_override ? `<div class="meta">ref: <b>${escapeHtml(meta.reference_override)}</b></div>` : ""}
        ${progressBar}
        ${s.error ? `<div class="meta err">${escapeHtml(s.error)}</div>` : ""}
        <div class="actions">
          ${canView ? `<a href="/shot/${PROJECT_ID}/${s.id}"><button class="small">View 3D</button></a>` : ""}
          ${canRun ? `<button class="small btn-run" data-id="${s.id}">${s.status === "failed" ? "Retry" : "Run"}</button>` : ""}
          <button class="small secondary btn-log" data-id="${s.id}">Log</button>
          ${canDelete ? `<button class="small secondary btn-del" data-id="${s.id}">✕</button>` : ""}
        </div>
        <pre class="log${openLogs.has(s.id) ? " open" : ""}" id="log-${s.id}">${escapeHtml(logCache.get(s.id) || "")}</pre>
      </div>
    `;
    host.appendChild(tile);

    // Restore scroll position for open logs
    if (openLogs.has(s.id)) {
      const logEl = tile.querySelector(".log");
      const saved = logScrollState.get(s.id);
      if (logEl) {
        if (saved?.pinnedBottom ?? true) {
          logEl.scrollTop = logEl.scrollHeight;
        } else {
          logEl.scrollTop = saved.scrollTop;
        }
      }
    }

    // Tile click toggles selection (but not when clicking buttons / log)
    tile.addEventListener("click", (e) => {
      if (e.target.closest("button") || e.target.closest("a") ||
          e.target.closest(".log")) return;
      if (selected.has(s.id)) {
        selected.delete(s.id); tile.classList.remove("selected");
      } else {
        selected.add(s.id); tile.classList.add("selected");
      }
      updateActions(latestProject);
    });
  }

  host.querySelectorAll(".btn-run").forEach(b =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.id;
      const ref = await pickReference(
        "Run this shot against which reference?",
        getShotRef(id),
      );
      if (ref === null) return;  // cancelled
      b.disabled = true;
      const r = await fetch(`/api/shots/${id}/run`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({reference: ref}),
      });
      if (!r.ok) alert("Failed to run: " + (await r.text()));
      loadProject();
    })
  );
  host.querySelectorAll(".btn-del").forEach(b =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.id;
      if (!confirm("Delete this shot?")) return;
      await fetch(`/api/shots/${id}`, {method: "DELETE"});
      selected.delete(id); openLogs.delete(id);
      logCache.delete(id); logScrollState.delete(id);
      loadProject();
    })
  );
  host.querySelectorAll(".btn-log").forEach(b =>
    b.addEventListener("click", async (ev) => {
      ev.stopPropagation();
      const id = b.dataset.id;
      const logEl = document.getElementById(`log-${id}`);
      if (logEl.classList.contains("open")) {
        logEl.classList.remove("open");
        openLogs.delete(id);
        logScrollState.delete(id);
        return;
      }
      logEl.classList.add("open");
      openLogs.add(id);
      logScrollState.set(id, {scrollTop: 0, pinnedBottom: true});
      await refreshLog(id);
      logEl.scrollTop = logEl.scrollHeight;
    })
  );

  // Refresh content for any open logs.
  for (const id of openLogs) {
    if (document.getElementById(`log-${id}`)) refreshLog(id);
    else openLogs.delete(id);
  }
}

function updateActions(p) {
  const shots = (p && p.shots) || [];
  document.getElementById("selCount").textContent =
    selected.size ? `${selected.size} selected` : "";
  const runSelBtn = document.getElementById("runSelectedBtn");
  runSelBtn.disabled = selected.size === 0;
  const anyDone = shots.some(s => s.status === "done");
  const sceneBtn = document.getElementById("combinedSceneBtn");
  sceneBtn.style.display = anyDone ? "" : "none";
  sceneBtn.href = `/scene/${PROJECT_ID}`;
}

async function refreshLog(id) {
  const r = await fetch(`/api/shots/${id}/log?tail=400`);
  const text = await r.text();
  logCache.set(id, text);
  const el = document.getElementById(`log-${id}`);
  if (!el || !el.classList.contains("open")) return;
  if (el.textContent === text) return;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = text;
  if (nearBottom) el.scrollTop = el.scrollHeight;
}

function formatStatus(s) {
  switch (s.status) {
    case "pending": return "pending";
    case "running": return "running";
    case "done":    return "done ✓";
    case "failed":  return "failed ✗";
    default:        return s.status;
  }
}

function scoreClass(score) {
  if (score >= 0.5)  return "score-high";
  if (score >= 0.3)  return "score-ok";
  if (score >= 0.15) return "score-weak";
  return "score-bad";
}

function renderSqlite(info, projMeta) {
  const sec = document.getElementById("sqliteSection");
  const body = document.getElementById("sqliteInfo");
  sec.style.display = "";
  if (projMeta.source === "snapapp") {
    const b = projMeta.bounds;
    const bounds = b
      ? `${b.lat_min.toFixed(5)}..${b.lat_max.toFixed(5)}, ${b.lon_min.toFixed(5)}..${b.lon_max.toFixed(5)}`
      : "n/a";
    body.innerHTML = `
      <div class="sqlite-summary">
        <div><span class="k">source:</span> SnapApp capture session</div>
        <div><span class="k">original:</span> ${escapeHtml(projMeta.original_filename || "")}</div>
        <div><span class="k">shots:</span> ${projMeta.n_sqlite_shots}</div>
        <div><span class="k">GPS bounds:</span> ${bounds}</div>
      </div>`;
    return;
  }
  if (!info || !info.ok) {
    const err = info ? info.error : "no sqlite info";
    body.innerHTML = `<div style="color:#991b1b">Could not read SQLite: ${escapeHtml(err)}</div>`;
    return;
  }
  body.innerHTML = `<div class="sqlite-summary">
    <div><span class="k">tables:</span> ${(info.tables || []).join(", ")}</div>
  </div>`;
}

document.getElementById("runSelectedBtn").addEventListener("click", async () => {
  if (selected.size === 0) return;
  const ref = await pickReference(
    `Run ${selected.size} selected shot(s) against which reference?`,
  );
  if (ref === null) return;
  const r = await fetch(`/api/projects/${PROJECT_ID}/run`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({shot_ids: [...selected], reference: ref}),
  });
  if (!r.ok) { alert("Failed: " + await r.text()); return; }
  const data = await r.json();
  selected.clear();
  loadProject();
  alert(`Queued ${data.count} shot(s)${ref ? ` against ${ref}` : ""}.`);
});

document.getElementById("runAllBtn").addEventListener("click", async () => {
  const ref = await pickReference(
    "Run SFM on every pending or failed shot — against which reference?",
  );
  if (ref === null) return;
  const r = await fetch(`/api/projects/${PROJECT_ID}/run`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({reference: ref}),
  });
  if (!r.ok) { alert("Failed: " + await r.text()); return; }
  const data = await r.json();
  loadProject();
  alert(`Queued ${data.count} shot(s)${ref ? ` against ${ref}` : ""}.`);
});

function getShotRef(shotId) {
  const s = (latestProject?.shots || []).find(x => x.id === shotId);
  return (s?.meta || {}).reference_override || "";
}

// A small modal that asks the user to pick one of the registered
// references. Returns: the chosen reference name (string), "" to mean
// "auto-pick by GPS", or null if the user cancelled.
function pickReference(title, current = "") {
  return new Promise(resolve => {
    if (!references.length) { resolve(""); return; }  // registry empty — auto
    const overlay = document.createElement("div");
    overlay.className = "modal";
    overlay.innerHTML = `
      <div class="modal-inner" style="max-width:480px">
        <div class="modal-hdr">
          <h3>${escapeHtml(title)}</h3>
        </div>
        <div class="modal-body">
          <label style="display:block; margin:6px 0"><input type="radio" name="ref-pick" value=""
            ${current === "" ? "checked" : ""}> Auto-pick by GPS <span style="color:#6b7280">(use registry priority)</span></label>
          ${references.map(r => `
            <label style="display:block; margin:6px 0">
              <input type="radio" name="ref-pick" value="${escapeHtml(r.name)}"
                ${current === r.name ? "checked" : ""}>
              ${escapeHtml(r.name)}
              <span style="color:#6b7280; font-size:11px">
                priority ${r.priority}${r.has_model ? "" : " · MegaLoc-only"}
              </span>
            </label>`).join("")}
          <div style="margin-top:16px; display:flex; gap:8px; justify-content:flex-end">
            <button id="_cancel" class="small secondary">Cancel</button>
            <button id="_ok" class="small">Run</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.querySelector("#_ok").addEventListener("click", () => {
      const sel = overlay.querySelector('input[name="ref-pick"]:checked');
      overlay.remove();
      resolve(sel ? sel.value : "");
    });
    overlay.querySelector("#_cancel").addEventListener("click", () => {
      overlay.remove(); resolve(null);
    });
  });
}

document.getElementById("searchCellsBtn").addEventListener("click", async () => {
  const modal = document.getElementById("cellsModal");
  const body = document.getElementById("cellsBody");
  modal.style.display = "";
  body.textContent = "Searching…";
  let data;
  try {
    const r = await fetch(`/api/projects/${PROJECT_ID}/cells-search`, {method: "POST"});
    data = await r.json();
    if (!r.ok && !data.error) data.error = `HTTP ${r.status}`;
  } catch (e) {
    body.innerHTML = `<div style="color:#991b1b">Request failed: ${escapeHtml(e.message)}</div>`;
    return;
  }
  renderCellsResult(data);
});

function renderCellsResult(data) {
  const body = document.getElementById("cellsBody");
  const bits = [];
  if (!data.ok) {
    bits.push(`<div style="color:#991b1b">${escapeHtml(data.error || "Search failed.")}</div>`);
  }
  const cells = data.cells || [];
  if (cells.length === 0 && data.ok) {
    bits.push(`<div>No cells contain any of this project's shot points.</div>`);
  }
  for (const c of cells) {
    bits.push(`
      <div class="cell">
        <div class="name">${escapeHtml(c.name || c.slug)}</div>
        <div class="sub">
          slug: <code>${escapeHtml(c.slug)}</code>
          ${c.address ? `· ${escapeHtml(c.address)}` : ""}
          ${c.reference ? `· ref ${escapeHtml(c.reference)}` : ""}
        </div>
        <div class="sub">
          ${c.matched_points ? `${c.matched_points.length} matched point(s)` : ""}
          ${c.cell_map_slug ? `· collection <code>${escapeHtml(c.cell_map_slug)}</code>` : ""}
          ${c.project_slug ? `· project <code>${escapeHtml(c.project_slug)}</code>` : ""}
        </div>
        <div class="actions">
          <a href="${c.workflow_url}" target="_blank" rel="noopener">
            <button class="small">Open workflow</button></a>
        </div>
      </div>`);
  }
  const diag = [];
  if (data.strategy) diag.push(`strategy: ${data.strategy}`);
  if (data.tried) diag.push(`tried: ${data.tried.join(" · ")}`);
  if (data.points) diag.push(`queried ${data.points.length} point(s)`);
  if (diag.length) bits.push(`<div class="diag">${escapeHtml(diag.join("\n"))}</div>`);
  body.innerHTML = bits.join("\n");
}

document.getElementById("cellsCloseBtn").addEventListener("click", () => {
  document.getElementById("cellsModal").style.display = "none";
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
setInterval(loadProject, 2000);
