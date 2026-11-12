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
