"use strict";

const PROJECT_ID = window.PROJECT_ID;

const selected = new Set();

let latestProject = null;

async function loadProject() {
  const r = await fetch(`/api/projects/${PROJECT_ID}`);
  if (!r.ok) {
    document.querySelector("main").innerHTML = `<div style="padding:20px;color:#991b1b">
      Failed to load project.</div>`;
    return;
  }
  const p = await r.json();
  latestProject = p;
  document.getElementById("projTitle").textContent = p.name;
  const tagBits = [p.kind, `${p.photos.length} photos`];
  if (p.megaloc_running) tagBits.push(`<span style="color:#1e40af">MegaLoc running…</span>`);
  else if (p.megaloc_ready) tagBits.push(`<span style="color:#065f46">MegaLoc ready</span>`);
  document.getElementById("projTag").innerHTML = tagBits.join(" • ");

  renderShots(p.shots || []);
  const photoMeta = (p.meta && p.meta.photo_meta) || {};
  renderPhotos(p.photos || [], p.megaloc_matches || {}, photoMeta);
  if (p.kind === "sqlite") {
    renderSqlite(p.sqlite, p.meta || {});
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

function renderPhotos(photos, megalocMatches, photoMeta) {
  const grid = document.getElementById("photosGrid");
  grid.innerHTML = "";
  if (!photos.length) {
    grid.innerHTML = `<div style="color:#6b7280;font-size:13px">No photos in this project.</div>`;
    return;
  }
  for (const name of photos) {
    const stem = name.replace(/\.[^/.]+$/, "");
    const match = megalocMatches[stem];
    const meta = photoMeta[name];
    const th = document.createElement("div");
    th.className = "th";
    th.dataset.stem = stem;
    if (selected.has(stem)) th.classList.add("selected");
    const scoreBadge = match
      ? `<div class="score ${scoreClass(match.score)}" title="${escapeHtml(match.shot_key)}">${match.score.toFixed(2)}</div>`
      : "";
    const gpsLine = meta && meta.lat != null && meta.lon != null
      ? `<div class="gps">${meta.lat.toFixed(5)},${meta.lon.toFixed(5)}${
           meta.bearing_deg != null ? ` · ${Math.round(meta.bearing_deg)}°` : ""}</div>`
      : "";
    th.innerHTML = `
      <img src="/api/projects/${PROJECT_ID}/photo/${encodeURIComponent(name)}" loading="lazy">
      ${scoreBadge}
      <div class="lbl">${escapeHtml(name)}${gpsLine}</div>
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

function scoreClass(score) {
  if (score >= 0.5) return "score-high";
  if (score >= 0.3) return "score-ok";
  if (score >= 0.15) return "score-weak";
  return "score-bad";
}

function renderSqlite(info, projMeta) {
  const sec = document.getElementById("sqliteSection");
  const body = document.getElementById("sqliteInfo");
  sec.style.display = "";
  if (projMeta.source === "snapapp") {
    // Extracted SnapApp session — shown as a richer summary.
    const b = projMeta.bounds;
    const bounds = b
      ? `${b.lat_min.toFixed(5)}..${b.lat_max.toFixed(5)}, ${b.lon_min.toFixed(5)}..${b.lon_max.toFixed(5)}`
      : "n/a";
    body.innerHTML = `
      <div class="sqlite-summary">
        <div><span class="k">source:</span> SnapApp capture session</div>
        <div><span class="k">original file:</span> ${escapeHtml(projMeta.original_filename || "")}</div>
        <div><span class="k">shots extracted:</span> ${projMeta.n_sqlite_shots}</div>
        <div><span class="k">GPS bounds:</span> ${bounds}</div>
      </div>
      <div style="margin-top:10px; color:#6b7280; font-size:12px">
        Each shot's 1× wide JPEG was extracted to the photo grid below — tap
        any to pick a subset, then use <b>New shot from selection</b> to run
        shotmatch_pose on just those frames. The default shot above runs
        SFM across every extracted photo.
      </div>
    `;
    return;
  }
  if (!info || !info.ok) {
    const err = info ? info.error : "no sqlite info";
    body.innerHTML = `<div style="color:#991b1b">Could not read SQLite: ${escapeHtml(err)}</div>`;
    return;
  }
  const sample = (info.poses_sample || []).slice(0, 6);
  const bounds = info.bounds
    ? `${info.bounds.lat_min.toFixed(5)}..${info.bounds.lat_max.toFixed(5)}, ${info.bounds.lon_min.toFixed(5)}..${info.bounds.lon_max.toFixed(5)}`
    : "n/a";
  body.innerHTML = `
    <div class="sqlite-summary">
      <div><span class="k">format:</span> legacy GCDB</div>
      <div><span class="k">capture:</span> ${escapeHtml(info.capture_name || "(none)")}</div>
      <div><span class="k">tables:</span> ${(info.tables || []).join(", ")}</div>
      <div><span class="k">poses:</span> ${info.pose_count}</div>
      <div><span class="k">bounds:</span> ${bounds}</div>
      ${sample.length ? `<div><span class="k">sample poses:</span>
        <ul>${sample.map(p =>
          `<li>id=${p.id} (${p.lat && p.lat.toFixed(5)}, ${p.lon && p.lon.toFixed(5)})</li>`
        ).join("")}</ul></div>` : ""}
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

  // Offer anchor override if we have MegaLoc scores
  let anchorOverride = null;
  const matches = (latestProject && latestProject.megaloc_matches) || {};
  const scoredStems = [...selected].filter(s => matches[s])
    .sort((a, b) => matches[b].score - matches[a].score);
  if (scoredStems.length >= 2) {
    const choices = scoredStems.map(s =>
      `  ${s} — ${matches[s].score.toFixed(3)} → ${matches[s].shot_key.split('/').pop()}`
    ).join("\n");
    const top = scoredStems[0];
    const picked = prompt(
      `Anchor photo (MegaLoc top = ${top}, score ${matches[top].score.toFixed(3)}).\n` +
      `Enter a photo stem to override, or leave blank to auto-pick:\n\n${choices}`,
      ""
    );
    if (picked === null) return;
    if (picked.trim()) anchorOverride = picked.trim();
  }
  const body = {name, photo_stems: [...selected]};
  if (anchorOverride) body.anchor_override = anchorOverride;
  const r = await fetch(`/api/projects/${PROJECT_ID}/shots`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
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
