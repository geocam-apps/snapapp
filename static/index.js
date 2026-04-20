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

uploadBtn.addEventListener("click", async () => {
  const kind = getKind();
  const name = document.getElementById("projName").value.trim();
  const fd = new FormData();
  if (name) fd.append("name", name);
  if (kind === "sqlite") {
    fd.append("sqlite", pendingFiles[0]);
  } else {
    for (const f of pendingFiles) fd.append("photos", f);
  }

  uploadBtn.disabled = true;
  uploadStatus.innerHTML = `<div>Uploading ${pendingFiles.length} file(s)...</div>
    <div class="progress"><div id="uploadBar" style="width:0%"></div></div>`;
  const bar = document.getElementById("uploadBar");

  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload");
  xhr.upload.addEventListener("progress", (e) => {
    if (e.lengthComputable) {
      bar.style.width = (e.loaded / e.total * 100).toFixed(1) + "%";
    }
  });
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const data = JSON.parse(xhr.responseText);
        uploadStatus.innerHTML = `<div style="color:#065f46">Uploaded. Redirecting...</div>`;
        setTimeout(() => { location.href = `/project/${data.project_id}`; }, 400);
      } catch (e) {
        uploadStatus.innerHTML = `<div style="color:#991b1b">Error: ${e}</div>`;
      }
    } else {
      let msg = xhr.responseText;
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch (e) {}
      uploadStatus.innerHTML = `<div style="color:#991b1b">Upload failed: ${msg}</div>`;
      uploadBtn.disabled = false;
    }
  };
  xhr.onerror = () => {
    uploadStatus.innerHTML = `<div style="color:#991b1b">Upload failed</div>`;
    uploadBtn.disabled = false;
  };
  xhr.send(fd);
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
