// Three.js STL viewer (spin with mouse / touch) and an off-screen renderer that
// makes card thumbnails for STLs that have no picture yet.
import * as THREE from "three";
import { STLLoader } from "three/addons/STLLoader.js";
import { OrbitControls } from "three/addons/OrbitControls.js";

const loader = new STLLoader();

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function prepareGeometry(geometry) {
  // STLs are Z-up; three.js is Y-up.
  geometry.rotateX(-Math.PI / 2);
  geometry.computeBoundingBox();
  const box = geometry.boundingBox;
  const center = new THREE.Vector3();
  box.getCenter(center);
  geometry.translate(-center.x, -box.min.y, -center.z); // sit on the bed, centred
  geometry.computeBoundingBox();
  if (!geometry.attributes.normal) geometry.computeVertexNormals();
  const size = new THREE.Vector3();
  geometry.boundingBox.getSize(size);
  return size;
}

function buildScene(color) {
  const scene = new THREE.Scene();
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a7a66, 2.2));
  const key = new THREE.DirectionalLight(0xffffff, 2.2);
  key.position.set(1, 2, 1.4);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xffffff, 0.8);
  rim.position.set(-1.5, 0.6, -1);
  scene.add(rim);
  const material = new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.05 });
  return { scene, material, key };
}

function frameCamera(camera, size, controls) {
  const radius = size.length() * 0.5 || 10; // bounding-sphere radius
  const dist = radius / Math.sin((camera.fov * Math.PI) / 360) * 1.05;
  const target = new THREE.Vector3(0, size.y / 2, 0);
  camera.position.set(target.x + dist * 0.75, target.y + dist * 0.55, target.z + dist * 0.85);
  camera.near = dist / 100;
  camera.far = dist * 100;
  camera.updateProjectionMatrix();
  camera.lookAt(target);
  if (controls) {
    controls.target.copy(target);
    controls.update();
  }
  return { radius, dist };
}

export function createViewer(container, { color = "#e9a23b", onInfo } = {}) {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);

  const { scene, material } = buildScene(new THREE.Color(color));
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.autoRotateSpeed = 1.5;
  controls.addEventListener("start", () => (controls.autoRotate = false));

  let mesh = null;
  let grid = null;
  let frame = 0;
  let disposed = false;

  function resize() {
    const w = container.clientWidth || 400;
    const h = container.clientHeight || 300;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(container);
  resize();

  function tick() {
    if (disposed) return;
    frame = requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  }
  tick();

  function clear() {
    if (mesh) { scene.remove(mesh); mesh.geometry.dispose(); mesh = null; }
    if (grid) { scene.remove(grid); grid.geometry.dispose(); grid = null; }
  }

  let lastSize = null;
  async function load(url) {
    clear();
    const geometry = await loader.loadAsync(url);
    if (disposed) { geometry.dispose(); return; }
    const size = prepareGeometry(geometry);
    lastSize = size;
    mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);
    const span = Math.max(size.x, size.z, 40) * 1.6;
    const step = span > 300 ? 50 : 10;
    const cells = Math.ceil(span / step);
    grid = new THREE.GridHelper(cells * step, cells, cssVar("--ink-2", "#888"), cssVar("--line", "#ccc"));
    grid.material.transparent = true;
    grid.material.opacity = 0.6;
    scene.add(grid);
    frameCamera(camera, size, controls);
    controls.autoRotate = true;
    const tris = geometry.index ? geometry.index.count / 3 : geometry.attributes.position.count / 3;
    onInfo?.({ x: size.x, y: size.z, z: size.y, triangles: tris });
    return size;
  }

  return {
    load,
    resetView() { if (lastSize) frameCamera(camera, lastSize, controls); },
    toggleSpin() { controls.autoRotate = !controls.autoRotate; return controls.autoRotate; },
    setColor(c) { material.color.set(c); },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      ro.disconnect();
      clear();
      controls.dispose();
      material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}

// ---------------------------------------------------------------- thumbnails
let thumbRenderer = null;
let queue = Promise.resolve();

export function renderThumbnail(url, color = "#e9a23b", px = 384) {
  // Serialise: one off-screen WebGL context, one model at a time.
  const job = queue.then(async () => {
    if (!thumbRenderer) {
      thumbRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    }
    thumbRenderer.setPixelRatio(1);
    thumbRenderer.setSize(px, px, false);
    const geometry = await loader.loadAsync(url);
    const size = prepareGeometry(geometry);
    const { scene, material } = buildScene(new THREE.Color(color));
    const mesh = new THREE.Mesh(geometry, material);
    scene.add(mesh);
    const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 10000);
    frameCamera(camera, size);
    thumbRenderer.setClearColor(0x000000, 0);
    thumbRenderer.render(scene, camera);
    const blob = await new Promise((res) => thumbRenderer.domElement.toBlob(res, "image/png"));
    geometry.dispose();
    material.dispose();
    return blob;
  });
  queue = job.catch(() => {});
  return job;
}

// ---------------------------------------------------------------- build set picture
// All the pieces of a build set laid out side by side (each copy), one colour
// per file, rendered to an image. Geometries are kept so sets sharing files
// (most of them) only download each STL once.
const setGeoms = new Map();
function setGeometry(url) {
  if (!setGeoms.has(url)) {
    setGeoms.set(url, loader.loadAsync(url).then((g) => { const size = prepareGeometry(g); g.computeVertexNormals(); return { g, size }; }));
  }
  return setGeoms.get(url);
}

/** parts: [{url, qty, color}] -> PNG blob of the pieces laid out on a bed. */
export function renderSet(parts, { width = 640, height = 400 } = {}) {
  const job = queue.then(async () => {
    if (!thumbRenderer) {
      thumbRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
    }
    thumbRenderer.setPixelRatio(1);
    thumbRenderer.setSize(width, height, false);
    const loaded = await Promise.all(parts.map(async (p) => ({ ...p, ...(await setGeometry(p.url)) })));
    // Biggest first, shelf-packed in rows about as wide as the set is deep.
    const pieces = loaded.flatMap((p) => Array.from({ length: Math.max(1, p.qty) }, () => p))
      .sort((a, b) => b.size.x * b.size.z - a.size.x * a.size.z);
    const gap = Math.max(4, Math.max(...pieces.map((p) => Math.max(p.size.x, p.size.z))) * 0.06);
    const area = pieces.reduce((n, p) => n + (p.size.x + gap) * (p.size.z + gap), 0);
    const rowWidth = Math.max(Math.sqrt(area) * 1.5, ...pieces.map((p) => p.size.x + gap));
    const { scene, key } = buildScene(new THREE.Color("#e9a23b"));
    const mats = [];
    let x = 0; let z = 0; let rowDepth = 0; let maxX = 0; let maxY = 0;
    for (const p of pieces) {
      if (x > 0 && x + p.size.x > rowWidth) { x = 0; z += rowDepth + gap; rowDepth = 0; }
      const mat = new THREE.MeshStandardMaterial({ color: new THREE.Color(p.color || "#e9a23b"), roughness: 0.55, metalness: 0.05 });
      mats.push(mat);
      const mesh = new THREE.Mesh(p.g, mat);
      mesh.position.set(x + p.size.x / 2, 0, z + p.size.z / 2);
      scene.add(mesh);
      x += p.size.x + gap;
      maxX = Math.max(maxX, x - gap);
      maxY = Math.max(maxY, p.size.y);
      rowDepth = Math.max(rowDepth, p.size.z);
    }
    const depth = z + rowDepth;
    scene.position.set(-maxX / 2, 0, -depth / 2);
    const size = new THREE.Vector3(maxX, maxY, depth);
    const camera = new THREE.PerspectiveCamera(30, width / height, 0.1, 100000);
    const radius = Math.max(size.x, size.z, size.y) * (pieces.length > 1 ? 0.46 : 0.6) || 10;
    const dist = radius / Math.sin((camera.fov * Math.PI) / 360);
    camera.position.set(dist * 0.1, dist * 0.78, dist * 0.62);
    camera.near = dist / 100;
    camera.far = dist * 100;
    camera.updateProjectionMatrix();
    camera.lookAt(0, 0, 0);
    key.position.set(0.6, 2, 1.2);
    thumbRenderer.setClearColor(0x000000, 0);
    thumbRenderer.render(scene, camera);
    const blob = await new Promise((res) => thumbRenderer.domElement.toBlob(res, "image/png"));
    mats.forEach((m) => m.dispose());
    return blob;
  });
  queue = job.catch(() => {});
  return job;
}

// ---------------------------------------------------------------- per-object preview
// Shows the pieces of a model (from /api/slicer/objects/mesh), each in the
// colour of the filament it will print with. Click a piece to pick it.
export function createPartsViewer(container, buffer, { onPick, bed = null } = {}) {
  const dv = new DataView(buffer);
  let off = 0;
  const count = dv.getUint32(off, true); off += 4;
  const geos = [];
  for (let i = 0; i < count; i++) {
    const n = dv.getUint32(off, true); off += 4;
    const src = new Float32Array(buffer.slice(off, off + n * 36)); off += n * 36;
    // Z-up mm -> three.js Y-up
    const pos = new Float32Array(src.length);
    for (let k = 0; k < src.length; k += 3) { pos[k] = src[k]; pos[k + 1] = src[k + 2]; pos[k + 2] = -src[k + 1]; }
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.computeVertexNormals();
    g.computeBoundingBox();
    geos.push(g);
  }
  const box = new THREE.Box3();
  geos.forEach((g) => box.union(g.boundingBox));
  const center = new THREE.Vector3();
  box.getCenter(center);
  geos.forEach((g) => g.translate(-center.x, -box.min.y, -center.z));
  const size = new THREE.Vector3();
  box.getSize(size);

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);
  const { scene } = buildScene(new THREE.Color("#999"));
  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const meshes = geos.map((g, i) => {
    const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color: "#999", roughness: 0.55, metalness: 0.05, transparent: true }));
    m.userData.index = i;
    scene.add(m);
    return m;
  });
  // The build plate, when known: a plate-sized slab with a 10 mm grid, the
  // model in the middle (the slicer centres / arranges it the same way).
  let grid, plate = null;
  if (bed) {
    const [bw, bd] = bed;
    plate = new THREE.Mesh(new THREE.BoxGeometry(bw, 1, bd),
      new THREE.MeshStandardMaterial({ color: "#2e2e30", roughness: 0.9 }));  // dark like a PEI plate
    plate.position.y = -0.55;
    scene.add(plate);
    grid = new THREE.GridHelper(Math.max(bw, bd), Math.round(Math.max(bw, bd) / 10), "#777", "#4a4a4c");
    grid.scale.set(bw / Math.max(bw, bd), 1, bd / Math.max(bw, bd));
  } else {
    const span = Math.max(size.x, size.z, 40) * 1.6;
    const step = span > 300 ? 50 : 10;
    const cells = Math.ceil(span / step);
    grid = new THREE.GridHelper(cells * step, cells, cssVar("--ink-2", "#888"), cssVar("--line", "#ccc"));
  }
  grid.material.transparent = true;
  grid.material.opacity = 0.5;
  scene.add(grid);
  // Frame the model, but show enough of the plate to judge its size on it.
  const view = bed ? new THREE.Vector3(Math.max(size.x, bed[0] * 0.3), size.y, Math.max(size.z, bed[1] * 0.3)) : size;
  frameCamera(camera, view, controls);

  function resize() {
    const w = container.clientWidth || 400;
    const h = container.clientHeight || 240;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(container);
  resize();
  let frame = 0, disposed = false;
  (function tick() {
    if (disposed) return;
    frame = requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  })();

  // A click (not a drag) picks the piece under the pointer.
  const ray = new THREE.Raycaster();
  let down = null;
  const el = renderer.domElement;
  el.addEventListener("pointerdown", (e) => (down = [e.clientX, e.clientY]));
  el.addEventListener("pointerup", (e) => {
    if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 5) return;
    const r = el.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1), camera);
    const hit = ray.intersectObjects(meshes.filter((m) => m.visible), false)[0];
    if (hit) onPick?.(hit.object.userData.index);
  });

  let lit = null;
  function paint(colours) {
    meshes.forEach((m, i) => {
      const off = colours[i] === null;  // left off the plate: a faint ghost you can still click
      m.material.color.set(off ? "#999" : colours[i] || "#999");
      m.material.opacity = off ? 0.12 : lit === null || lit === i ? 1 : 0.18;
      m.material.depthWrite = !off && (lit === null || lit === i);
      m.material.emissive.set(lit === i ? "#333" : "#000");
    });
  }
  // Lay groups of pieces out side by side, the way the slicer arranges
  // separate objects; null puts everything back where the file has it.
  const home = geos.map(() => new THREE.Vector3());
  let clones = [];
  // ``places`` (copies): {c, g, x, y} per group per copy, centres in mm from
  // the middle of the bed; copy 0 moves the pieces, later copies are clones.
  function layout(groups, places = null) {
    clones.forEach((m) => scene.remove(m));
    clones = [];
    meshes.forEach((m, i) => m.position.copy(home[i]));
    if (!groups) return;
    const order = [...new Set(groups)];
    const boxes = order.map((g) => {
      const b = new THREE.Box3();
      meshes.forEach((m, i) => { if (groups[i] === g && m.visible) b.union(m.geometry.boundingBox); });
      return b;
    });
    if (places) {
      for (const p of places) {
        const b = boxes[order.indexOf(p.g)];
        if (!b || b.isEmpty()) continue;
        const dx = p.x - (b.min.x + b.max.x) / 2, dz = -p.y - (b.min.z + b.max.z) / 2, dy = -b.min.y;
        meshes.forEach((m, i) => {
          if (groups[i] !== p.g) return;
          let t = m;
          if (p.c > 0) {
            t = new THREE.Mesh(m.geometry, m.material);  // same colour as the original
            t.raycast = () => {};
            t.visible = m.visible;
            scene.add(t);
            clones.push(t);
          }
          t.position.set(dx, dy, dz);
        });
      }
      return;
    }
    const gap = 8;
    const widths = boxes.map((b) => (b.isEmpty() ? 0 : b.max.x - b.min.x));
    const total = widths.reduce((a, w) => a + w, 0) + gap * Math.max(0, widths.filter(Boolean).length - 1);
    let x = -total / 2;
    order.forEach((g, k) => {
      const b = boxes[k];
      if (b.isEmpty()) return;
      const dx = x - b.min.x, dz = -(b.min.z + b.max.z) / 2, dy = -b.min.y;
      meshes.forEach((m, i) => { if (groups[i] === g) m.position.set(dx, dy, dz); });
      x += widths[k] + gap;
    });
  }
  let last = [];
  return {
    layout,
    setColours(colours) { last = colours; paint(colours); },
    highlight(i) { lit = i ?? null; paint(last); },
    show(visible) { meshes.forEach((m, i) => (m.visible = visible[i] !== false)); },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      meshes.forEach((m) => { m.geometry.dispose(); m.material.dispose(); });
      grid.geometry.dispose();
      if (plate) { plate.geometry.dispose(); plate.material.dispose(); }
      renderer.dispose();
      el.remove();
    },
  };
}

// ---------------------------------------------------------------- sliced toolpath preview
// Draws the extrusion lines of a sliced file (from /api/files/{id}/toolpath) on
// the build plate, each filament in the colour of the toolhead it prints with.
export function createToolpathViewer(container, buffer) {
  const dv = new DataView(buffer);
  if (String.fromCharCode(...new Uint8Array(buffer, 0, 4)) !== "PPTP") throw new Error("Not a toolpath");
  const [bx0, by0, bx1, by1] = [4, 8, 12, 16].map((o) => dv.getFloat32(o, true));
  const ntools = dv.getUint32(20, true);
  const cx = (bx0 + bx1) / 2, cy = (by0 + by1) / 2, bw = bx1 - bx0, bd = by1 - by0;
  let off = 24, maxZ = 1;
  const parts = [];
  for (let t = 0; t < ntools; t++) {
    const tool = dv.getUint32(off, true), n = dv.getUint32(off + 4, true);
    off += 8;
    const src = new Int16Array(buffer.slice(off, off + n * 12));
    off += n * 12;
    const pos = new Float32Array(n * 6);
    for (let k = 0; k < src.length; k += 3) {
      const z = src[k + 2] / 20;
      pos[k] = src[k] / 20 - cx; pos[k + 1] = z; pos[k + 2] = -(src[k + 1] / 20 - cy);
      if (z > maxZ) maxZ = z;
    }
    parts.push({ tool, pos });
  }

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  container.appendChild(renderer.domElement);
  const { scene } = buildScene(new THREE.Color("#999"));
  const plate = new THREE.Mesh(new THREE.BoxGeometry(bw, 1, bd), new THREE.MeshStandardMaterial({ color: "#2e2e30", roughness: 0.9 }));
  plate.position.y = -0.55;
  scene.add(plate);
  const grid = new THREE.GridHelper(Math.max(bw, bd), Math.round(Math.max(bw, bd) / 10), "#777", "#4a4a4c");
  grid.scale.set(bw / Math.max(bw, bd), 1, bd / Math.max(bw, bd));
  grid.material.transparent = true;
  grid.material.opacity = 0.5;
  scene.add(grid);

  // Lower layers a little darker, so the shape reads without lighting.
  const lines = parts.map(({ tool, pos }) => {
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    g.setAttribute("color", new THREE.BufferAttribute(new Float32Array(pos.length), 3));
    const l = new THREE.LineSegments(g, new THREE.LineBasicMaterial({ vertexColors: true }));
    l.userData.tool = tool;
    scene.add(l);
    return l;
  });

  const camera = new THREE.PerspectiveCamera(35, 1, 0.1, 10000);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  // Frame what's printed (with some plate around it), not the whole empty bed.
  const box = new THREE.Box3();
  lines.forEach((l) => { l.geometry.computeBoundingBox(); box.union(l.geometry.boundingBox); });
  if (box.isEmpty()) box.set(new THREE.Vector3(-bw / 2, 0, -bd / 2), new THREE.Vector3(bw / 2, 1, bd / 2));
  const sz = new THREE.Vector3(), mid = new THREE.Vector3();
  box.getSize(sz); box.getCenter(mid);
  frameCamera(camera, new THREE.Vector3(Math.max(sz.x * 1.1, bw * 0.25), sz.y, Math.max(sz.z * 1.1, bd * 0.25)), controls);
  mid.y = 0;
  camera.position.add(mid);
  controls.target.add(mid);
  controls.update();

  function resize() {
    const w = container.clientWidth || 400, h = container.clientHeight || 240;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  const ro = new ResizeObserver(resize);
  ro.observe(container);
  resize();
  let frame = 0, disposed = false;
  (function tick() {
    if (disposed) return;
    frame = requestAnimationFrame(tick);
    controls.update();
    renderer.render(scene, camera);
  })();

  const c = new THREE.Color();
  return {
    tools: parts.map((p) => p.tool),
    // colours: {filament index: css colour}
    setColours(colours) {
      lines.forEach((l) => {
        c.set(colours[l.userData.tool] || "#999");
        const pos = l.geometry.attributes.position.array, col = l.geometry.attributes.color.array;
        for (let k = 0; k < pos.length; k += 3) {
          const s = 0.55 + 0.45 * (pos[k + 1] / maxZ);
          col[k] = c.r * s; col[k + 1] = c.g * s; col[k + 2] = c.b * s;
        }
        l.geometry.attributes.color.needsUpdate = true;
      });
    },
    dispose() {
      disposed = true;
      cancelAnimationFrame(frame);
      ro.disconnect();
      controls.dispose();
      lines.forEach((l) => { l.geometry.dispose(); l.material.dispose(); });
      grid.geometry.dispose();
      plate.geometry.dispose(); plate.material.dispose();
      renderer.dispose();
      renderer.domElement.remove();
    },
  };
}
