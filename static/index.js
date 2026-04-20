"use strict";

const drop = document.getElementById("drop");
const fileInput = document.getElementById("fileInput");
const dropFiles = document.getElementById("dropFiles");
const uploadBtn = document.getElementById("uploadBtn");
const uploadStatus = document.getElementById("uploadStatus");
const pickBtn = document.getElementById("pickBtn");
const kindRadios = document.getElementsByName("kind");

let pendingFiles = [];

function getKind() {
  for (const r of kindRadios) if (r.checked) return r.value;
  return "images";
}

function updateAcceptAttr() {
  const kind = getKind();
  if (kind === "sqlite") {
    fileInput.setAttribute("accept", ".gcdb,.sqlite,.sqlite3,.db");
    fileInput.multiple = false;
  } else {
    fileInput.setAttribute("accept", ".heic,.heif,.jpg,.jpeg,.png");
    fileInput.multiple = true;
  }
}
for (const r of kindRadios) r.addEventListener("change", () => {
  pendingFiles = [];
  renderFiles();
  updateAcceptAttr();
});
updateAcceptAttr();

pickBtn.addEventListener("click", (e) => {
  e.preventDefault();
  fileInput.click();
});
fileInput.addEventListener("change", () => {
  addFiles(Array.from(fileInput.files));
});

["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, (e) => {
  e.preventDefault(); e.stopPropagation();
  drop.classList.add("drag-over");
}));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, (e) => {
  e.preventDefault(); e.stopPropagation();
  drop.classList.remove("drag-over");
}));
drop.addEventListener("drop", (e) => {
  addFiles(Array.from(e.dataTransfer.files));
});

function addFiles(files) {
  const kind = getKind();
  if (kind === "sqlite") {
    pendingFiles = files.slice(0, 1);
  } else {
    pendingFiles = pendingFiles.concat(files);
  }
  renderFiles();
}

function renderFiles() {
  dropFiles.innerHTML = "";
  for (const f of pendingFiles) {
    const el = document.createElement("span");
    el.className = "f";
    el.textContent = `${f.name} (${formatSize(f.size)})`;
    dropFiles.appendChild(el);
  }
  uploadBtn.disabled = pendingFiles.length === 0;
}

function formatSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  if (n < 1024 * 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + " MB";
  return (n / 1024 / 1024 / 1024).toFixed(2) + " GB";
}

// Chunk size well under Cloudflare's 100 MB free-tier body cap.
const CHUNK_SIZE = 16 * 1024 * 1024;  // 16 MB

function uuid() {
  return ([1e7]+-1e3+-4e3+-8e3+-1e11).replace(/[018]/g, c =>
    (c ^ crypto.getRandomValues(new Uint8Array(1))[0] & 15 >> c / 4).toString(16));
}

async function uploadOneFile(file, onProgress) {
  const uploadId = uuid();
  let offset = 0;
  while (offset < file.size) {
    const end = Math.min(offset + CHUNK_SIZE, file.size);
    const slice = file.slice(offset, end);
    const fd = new FormData();
    fd.append("upload_id", uploadId);
    fd.append("offset", String(offset));
    fd.append("chunk", slice);
    const r = await fetch("/api/upload/chunk", {method: "POST", body: fd});
    if (!r.ok) {
      let msg = r.status + "";
      try { msg = (await r.json()).error || msg; } catch (e) {}
      throw new Error(`chunk upload failed: ${msg}`);
    }
    offset = end;
    onProgress(offset, file.size);
  }
  return uploadId;
}

uploadBtn.addEventListener("click", async () => {
  const kind = getKind();
  const name = document.getElementById("projName").value.trim();

  uploadBtn.disabled = true;
  const totalBytes = pendingFiles.reduce((s, f) => s + f.size, 0);
  let doneBytes = 0;
  uploadStatus.innerHTML = `<div id="upMsg">Uploading ${pendingFiles.length} file(s) (${formatSize(totalBytes)})...</div>
    <div class="progress"><div id="uploadBar" style="width:0%"></div></div>`;
  const bar = document.getElementById("uploadBar");
  const msgEl = document.getElementById("upMsg");

  try {
    const uploaded = [];
    for (let i = 0; i < pendingFiles.length; i++) {
      const f = pendingFiles[i];
      msgEl.textContent = `Uploading ${i + 1}/${pendingFiles.length}: ${f.name} (${formatSize(f.size)})`;
      const startedAt = doneBytes;
      const uploadId = await uploadOneFile(f, (sent, total) => {
        const cur = startedAt + sent;
        bar.style.width = (cur / totalBytes * 100).toFixed(1) + "%";
      });
      doneBytes += f.size;
      uploaded.push({upload_id: uploadId, filename: f.name});
    }

    msgEl.textContent = `Finalizing...`;
    const finalRes = await fetch("/api/upload/finalize", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name, kind, files: uploaded}),
    });
    if (!finalRes.ok) {
      let msg = finalRes.status + "";
      try { msg = (await finalRes.json()).error || msg; } catch (e) {}
      throw new Error(`finalize failed: ${msg}`);
    }
    const data = await finalRes.json();
    uploadStatus.innerHTML = `<div style="color:#065f46">Uploaded. Redirecting...</div>`;
    setTimeout(() => { location.href = `/project/${data.project_id}`; }, 400);
  } catch (e) {
    uploadStatus.innerHTML = `<div style="color:#991b1b">Upload failed: ${e.message}</div>`;
    uploadBtn.disabled = false;
  }
});

// Projects list
async function loadProjects() {
  const list = document.getElementById("projectList");
  const r = await fetch("/api/projects");
  if (!r.ok) {
    list.textContent = "Failed to load projects";
    return;
  }
  const projects = await r.json();
  list.innerHTML = "";
  if (projects.length === 0) {
    list.innerHTML = `<div style="color:#6b7280">No projects yet. Upload something above.</div>`;
    return;
  }
  for (const p of projects) {
    const created = new Date(p.created_at * 1000).toLocaleString();
    const ss = p.shots_summary;
    const pills = [];
    if (ss.running) pills.push(`<span class="pill running">${ss.running} running</span>`);
    if (ss.done) pills.push(`<span class="pill done">${ss.done} done</span>`);
    if (ss.failed) pills.push(`<span class="pill failed">${ss.failed} failed</span>`);
    if (ss.pending) pills.push(`<span class="pill">${ss.pending} pending</span>`);

    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <h3>${escapeHtml(p.name)}</h3>
      <div class="meta">${p.kind} • ${created}</div>
      <div class="shot-stats">
        <span class="pill">${p.n_shots} shot${p.n_shots === 1 ? "" : "s"}</span>
        ${pills.join(" ")}
      </div>
    `;
    card.addEventListener("click", () => location.href = `/project/${p.id}`);
    list.appendChild(card);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, m => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  })[m]);
}

loadProjects();
setInterval(loadProjects, 5000);
