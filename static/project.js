"use strict";

const PROJECT_ID = window.PROJECT_ID;

const selected = new Set();

let latestProject = null;
const openLogs = new Set();
const logCache = new Map();         // shot_id -> last fetched log text
const logScrollState = new Map();   // shot_id -> {scrollTop, pinnedBottom}

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
  // Capture scroll state of any currently-open log panes before the
  // DOM gets wiped — new <pre> elements otherwise default to scrollTop=0.
  for (const id of openLogs) {
    const el = document.getElementById(`log-${id}`);
    if (el) {
      const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      logScrollState.set(id, {scrollTop: el.scrollTop, pinnedBottom: pinned});
    }
  }
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
      <pre class="log${openLogs.has(s.id) ? " open" : ""}" id="log-${s.id}">${escapeHtml(logCache.get(s.id) || "")}</pre>
    `;
    host.appendChild(el);
    // Restore the user's previous scroll position (or keep pinned to bottom
    // if they were already there when the last poll hit).
    if (openLogs.has(s.id)) {
      const logEl = el.querySelector(".log");
      const saved = logScrollState.get(s.id);
      if (logEl) {
        if (saved?.pinnedBottom ?? true) {
          logEl.scrollTop = logEl.scrollHeight;
        } else {
          logEl.scrollTop = saved.scrollTop;
        }
      }
    }
  }

  host.querySelectorAll(".btn-run").forEach(b => b.addEventListener("click", async () => {
    const id = b.dataset.id;
    const shot = (latestProject?.shots || []).find(s => s.id === id);
    const mm = latestProject?.megaloc_matches || {};
    const stems = shot?.meta?.photo_stems || [];
    // No early block on single-photo shots anymore — the pipeline branches
    // to register_photo.py (PnP against the ref-triangulated scene) for
    // those. Only warn if the best anchor is GPS-far and score-weak.
    // Independent mode picks an anchor per photo. Surface a count of how
    // many photos in the selection have at least one GPS-valid match —
    // those are the ones likely to register cleanly. The pure-GPS
    // fallback will still try the rest, but yield drops on those.
    let withGps = 0, total = 0;
    for (const ps of stems) {
      const m = mm[ps];
      if (!m) continue;
      total += 1;
      if (m.gps_best) withGps += 1;
    }
    if (total > 0 && withGps === 0) {
      const msg =
        `None of the ${total} selected photos have a MegaLoc match within ` +
        `the phone's GPS accuracy radius.\n\n` +
        `The pipeline will fall back to picking the geographically nearest ` +
        `reference shot for each, but PnP success is less likely.\n\n` +
        `Run anyway?`;
      if (!confirm(msg)) return;
    }
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
      openLogs.delete(id);
      logScrollState.delete(id);
      return;
    }
    logEl.classList.add("open");
    openLogs.add(id);
    // New opens start pinned to bottom.
    logScrollState.set(id, {scrollTop: 0, pinnedBottom: true});
    await refreshLog(id);
    logEl.scrollTop = logEl.scrollHeight;
  }));
  // Refresh content for any open logs. The pre element is pre-populated
  // with cached text during render so there's no flicker; this just
  // pulls any new lines since the last poll.
  for (const id of openLogs) {
    if (document.getElementById(`log-${id}`)) {
      refreshLog(id);
    } else {
      openLogs.delete(id);  // shot was deleted
    }
  }
}

async function refreshLog(id) {
  const r = await fetch(`/api/shots/${id}/log?tail=400`);
  const text = await r.text();
  logCache.set(id, text);
  // Element may have been replaced by a re-render since we started; re-fetch.
  const el = document.getElementById(`log-${id}`);
  if (!el || !el.classList.contains("open")) return;
  if (el.textContent === text) return;  // no change, skip the scroll jump
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.textContent = text;
  if (nearBottom) el.scrollTop = el.scrollHeight;
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
    // Pick the best match to display: prefer the GPS-valid one if we have
    // any, else fall back to the MegaLoc top-1 (what `match` already is).
    const displayMatch = (match && match.gps_best) || match;
    const scoreBadge = match
      ? `<div class="score ${scoreClass(displayMatch.score)}" title="${escapeHtml(displayMatch.shot_key || "")}">${displayMatch.score.toFixed(2)}</div>`
      : "";
    // Distance badge: how far the picked ref shot is from the phone's GPS.
    let gpsBadge = "";
    if (displayMatch && displayMatch.distance_m != null) {
      const d = displayMatch.distance_m;
      const cls = displayMatch.gps_valid ? "gps-ok" : "gps-far";
      const label = d < 1000 ? `${Math.round(d)} m` : `${(d/1000).toFixed(1)} km`;
      gpsBadge = `<div class="gps-dist ${cls}" title="distance from phone GPS to ref shot">${label}</div>`;
    }
    const gpsLine = meta && meta.lat != null && meta.lon != null
      ? `<div class="gps">${meta.lat.toFixed(5)},${meta.lon.toFixed(5)}${
           meta.bearing_deg != null ? ` · ${Math.round(meta.bearing_deg)}°` : ""}</div>`
      : "";
    th.innerHTML = `
      <img src="/api/projects/${PROJECT_ID}/photo/${encodeURIComponent(name)}" loading="lazy">
      ${scoreBadge}
      ${gpsBadge}
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
  const name = prompt(
    `Name this shot (${selected.size} photo${selected.size === 1 ? "" : "s"}):`,
    `Shot ${new Date().toISOString().slice(11,19)}`
  );
  if (name === null) return;
  // Each photo is registered independently against the reference model
  // using its own GPS-picked anchor, so there's no shot-wide anchor to
  // set here. Just name + selected photos.
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
// Poll every 2s so running shots update fast; open logs survive re-renders
// because renderShots() re-applies the .open class from the openLogs set.
setInterval(loadProject, 2000);
