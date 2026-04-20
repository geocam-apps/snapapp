"use strict";
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const PROJECT_ID = window.PROJECT_ID;
const SHOT_ID = window.SHOT_ID;

document.getElementById("backLink").href = `/project/${PROJECT_ID}`;

const canvas = document.getElementById("canvas");
const renderer = new THREE.WebGLRenderer({canvas, antialias: true});
renderer.setPixelRatio(window.devicePixelRatio);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a0f);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);
camera.up.set(0, 0, 1);

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

function resize() {
  const w = canvas.clientWidth, h = canvas.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

scene.add(new THREE.AmbientLight(0xffffff, 0.8));
scene.add(new THREE.DirectionalLight(0xffffff, 0.6));
scene.add(new THREE.AxesHelper(2));

const groups = {
  points: new THREE.Group(),
  refFrustums: new THREE.Group(),
  refImages: new THREE.Group(),
  queryFrustum: new THREE.Group(),
  queryImage: new THREE.Group(),
};
for (const g of Object.values(groups)) scene.add(g);

function clearGroup(g) {
  while (g.children.length) {
    const c = g.children[0]; g.remove(c);
    if (c.geometry) c.geometry.dispose();
    if (c.material) { if (c.material.map) c.material.map.dispose(); c.material.dispose(); }
  }
}

function buildFrustum(cam, depth, color) {
  const { fx, fy, cx, cy, width, height, center, rotation } = cam;
  const corners = [
    [-cx / fx * depth, -cy / fy * depth, depth],
    [(width - cx) / fx * depth, -cy / fy * depth, depth],
    [(width - cx) / fx * depth, (height - cy) / fy * depth, depth],
    [-cx / fx * depth, (height - cy) / fy * depth, depth],
  ];
  const R = rotation;
  const RT = [
    [R[0][0], R[1][0], R[2][0]],
    [R[0][1], R[1][1], R[2][1]],
    [R[0][2], R[1][2], R[2][2]],
  ];
  function tf(p) {
    return [
      RT[0][0]*p[0]+RT[0][1]*p[1]+RT[0][2]*p[2]+center[0],
      RT[1][0]*p[0]+RT[1][1]*p[1]+RT[1][2]*p[2]+center[1],
      RT[2][0]*p[0]+RT[2][1]*p[1]+RT[2][2]*p[2]+center[2],
    ];
  }
  const wCorners = corners.map(tf);
  const positions = [];
  for (const c of wCorners) positions.push(...center, ...c);
  for (let i = 0; i < 4; i++) positions.push(...wCorners[i], ...wCorners[(i+1) % 4]);
  const geom = new THREE.BufferGeometry();
  geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  const mat = new THREE.LineBasicMaterial({color, transparent: true, opacity: 0.9});
  return {lines: new THREE.LineSegments(geom, mat), wCorners};
}

function buildBillboard(cam, depth, texUrl, opacity=1.0) {
  const { fx, fy, cx, cy, width, height, center, rotation } = cam;
  const corners = [
    [-cx/fx*depth, -cy/fy*depth, depth],
    [(width-cx)/fx*depth, -cy/fy*depth, depth],
    [(width-cx)/fx*depth, (height-cy)/fy*depth, depth],
    [-cx/fx*depth, (height-cy)/fy*depth, depth],
  ];
  const RT = [
    [rotation[0][0], rotation[1][0], rotation[2][0]],
    [rotation[0][1], rotation[1][1], rotation[2][1]],
    [rotation[0][2], rotation[1][2], rotation[2][2]],
  ];
  const wCorners = corners.map(p => [
    RT[0][0]*p[0]+RT[0][1]*p[1]+RT[0][2]*p[2]+center[0],
    RT[1][0]*p[0]+RT[1][1]*p[1]+RT[1][2]*p[2]+center[1],
    RT[2][0]*p[0]+RT[2][1]*p[1]+RT[2][2]*p[2]+center[2],
  ]);
  const geom = new THREE.BufferGeometry();
  const positions = [
    ...wCorners[0], ...wCorners[1], ...wCorners[2],
    ...wCorners[0], ...wCorners[2], ...wCorners[3],
  ];
  const uvs = [0,1, 1,1, 1,0, 0,1, 1,0, 0,0];
  geom.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
  geom.setAttribute("uv", new THREE.Float32BufferAttribute(uvs, 2));
  const tex = new THREE.TextureLoader().load(texUrl);
  tex.colorSpace = THREE.SRGBColorSpace;
  const mat = new THREE.MeshBasicMaterial({map: tex, side: THREE.DoubleSide,
                                            transparent: opacity < 1, opacity});
  return new THREE.Mesh(geom, mat);
}

function sceneCenter(cams) {
  if (!cams.length) return [0,0,0];
  const avg = [0,0,0];
  for (const c of cams) { avg[0]+=c.center[0]; avg[1]+=c.center[1]; avg[2]+=c.center[2]; }
  return avg.map(x => x/cams.length);
}

let sceneData = null;

async function load() {
  const r = await fetch(`/api/shots/${SHOT_ID}/scene`);
  if (!r.ok) {
    const msg = r.status === 404
      ? `No reconstructed model yet. The shot may still be running — return to the project and wait for the progress to complete.`
      : `Failed to load: HTTP ${r.status}`;
    document.getElementById("summary").innerHTML =
      `<span style="color:#c66">${msg}</span>`;
    return;
  }
  sceneData = await r.json();
  render();
  populateSidebar();
}

function render() {
  for (const g of Object.values(groups)) clearGroup(g);
  const depth = parseFloat(document.getElementById("frustumDepth").value);

  // Points
  if (sceneData.points && sceneData.points.length > 0) {
    const n = sceneData.points.length;
    const positions = new Float32Array(n*3);
    const colors = new Float32Array(n*3);
    sceneData.points.forEach((p, i) => {
      positions[3*i] = p.xyz[0]; positions[3*i+1] = p.xyz[1]; positions[3*i+2] = p.xyz[2];
      colors[3*i] = p.rgb[0]/255; colors[3*i+1] = p.rgb[1]/255; colors[3*i+2] = p.rgb[2]/255;
    });
    const geom = new THREE.BufferGeometry();
    geom.setAttribute("position", new THREE.BufferAttribute(positions, 3));
    geom.setAttribute("color", new THREE.BufferAttribute(colors, 3));
    const mat = new THREE.PointsMaterial({size: 0.05, vertexColors: true, sizeAttenuation: true});
    groups.points.add(new THREE.Points(geom, mat));
  }

  const queryCams = sceneData.cameras.filter(c => c.is_query);
  const refCams = sceneData.cameras.filter(c => !c.is_query);

  for (const c of refCams) {
    const {lines} = buildFrustum(c, depth * 0.3, 0x4a90d9);
    groups.refFrustums.add(lines);
    // Pre-load ref billboards but hidden by default
    const bb = buildBillboard(c, depth * 0.3, c.image_url, 0.9);
    groups.refImages.add(bb);
  }
  for (const c of queryCams) {
    const color = c.is_anchor ? 0xff6677 : 0xffcc33;
    const {lines} = buildFrustum(c, depth, color);
    groups.queryFrustum.add(lines);
    const bb = buildBillboard(c, depth, c.image_url, 0.85);
    groups.queryImage.add(bb);
  }

  applyToggles();

  // Camera fit
  const c = sceneCenter(queryCams.length ? queryCams : refCams);
  controls.target.set(c[0], c[1], c[2]);
  camera.position.set(c[0]+8, c[1]+8, c[2]+5);
  controls.update();

  // Summary
  const s = sceneData.summary || {};
  const info = document.getElementById("summary");
  info.innerHTML = `
    <div><b>${s.n_registered || 0}/${s.n_queries || 0}</b> registered</div>
    <div class="v">anchors: ${Object.keys(s.anchors || {}).join(", ") || "(none)"}</div>
    <div class="v">method: ${escapeHtml(s.method || "")}</div>
    ${(s.failed && s.failed.length)
      ? `<div style="color:#ff9aa2">failed: ${s.failed.join(", ")}</div>` : ""}
  `;
  document.getElementById("shotTitle").textContent = `Shot ${SHOT_ID.slice(0,8)}`;
  document.getElementById("shotTag").textContent =
    `${s.n_registered || 0}/${s.n_queries || 0} registered • ${sceneData.points.length} pts`;
}

function populateSidebar() {
  const list = document.getElementById("camList");
  list.innerHTML = "";
  const q = sceneData.cameras.filter(c => c.is_query);
  const r = sceneData.cameras.filter(c => !c.is_query);
  for (const c of q) {
    const d = document.createElement("div");
    d.className = "cam query" + (c.is_anchor ? " anchor" : "");
    d.textContent = (c.is_anchor ? "⚓ " : "📷 ") + (c.stem || c.name);
    d.addEventListener("click", () => focusCam(c));
    list.appendChild(d);
  }
  if (r.length) {
    const sep = document.createElement("div");
    sep.className = "sep";
    sep.textContent = `Refs (${r.length})`;
    list.appendChild(sep);
  }
  for (const c of r) {
    const d = document.createElement("div");
    d.className = "cam";
    d.textContent = c.name;
    d.addEventListener("click", () => focusCam(c));
    list.appendChild(d);
  }
}

function focusCam(c) {
  controls.target.set(c.center[0], c.center[1], c.center[2]);
  camera.position.set(c.center[0]+4, c.center[1]+4, c.center[2]+3);
  controls.update();
}

function applyToggles() {
  groups.points.visible = document.getElementById("showPoints").checked;
  groups.refFrustums.visible = document.getElementById("showRefFrustums").checked;
  groups.refImages.visible = document.getElementById("showRefImages").checked;
  groups.queryImage.visible = document.getElementById("showQueryImages").checked;
}

["showPoints", "showRefFrustums", "showRefImages", "showQueryImages"].forEach(id =>
  document.getElementById(id).addEventListener("change", applyToggles));

document.getElementById("frustumDepth").addEventListener("input", () => {
  if (sceneData) render();
});

function animate() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
animate();

function escapeHtml(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, m => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  })[m]);
}

load();
