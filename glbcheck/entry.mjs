// Browser page for the headless-Chrome check: view.html?glb=<url>
// Loads the .glb with three r180 GLTFLoader + MeshoptDecoder, renders an
// isometric view, and writes the summary JSON into <pre id="out">.
import * as THREE from 'three';
import { parseGlb, summarise } from './inspect.mjs';

const out = document.getElementById('out');
const url = new URLSearchParams(location.search).get('glb');
try {
  const buf = await (await fetch(url)).arrayBuffer();
  const gltf = await parseGlb(buf);
  const s = summarise(gltf);
  const W = innerWidth, H = innerHeight;
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setSize(W, H);
  document.body.prepend(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xf4f5f7);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8890a0, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 1.8); sun.position.set(1, 2, 3); scene.add(sun);
  scene.add(gltf.scene);
  // colour instances by component so grouping is visible
  const comps = [...new Set(s.named.filter((n) => n.kind === 'part' || n.kind === 'body').map((n) => n.component + (n.kind === 'body' ? n.id : '')))];
  gltf.scene.traverse((o) => {
    if (!o.isMesh) return;
    let p = o; while (p && !(p.userData.kind === 'part' || p.userData.kind === 'body')) p = p.parent;
    const key = p ? p.userData.component + (p.userData.kind === 'body' ? p.userData.id : '') : '';
    const h = (comps.indexOf(key) * 0.618034) % 1;
    o.material = new THREE.MeshStandardMaterial({ color: new THREE.Color().setHSL(h, 0.45, 0.6), metalness: 0.1, roughness: 0.7 });
  });
  const box = new THREE.Box3().setFromObject(gltf.scene);
  const c = box.getCenter(new THREE.Vector3()), r = box.getSize(new THREE.Vector3()).length() / 2;
  const view = (new URLSearchParams(location.search).get('view') || '1,0.8,1.3').split(',').map(Number);
  const cam = new THREE.PerspectiveCamera(35, W / H, r / 100, r * 100);
  cam.position.copy(c).add(new THREE.Vector3(...view).normalize().multiplyScalar(r * 2.6));
  cam.lookAt(c);
  renderer.render(scene, cam);
  out.textContent = JSON.stringify({ ok: true, ...s, named: s.named.length });
} catch (e) {
  out.textContent = JSON.stringify({ ok: false, error: String(e && e.stack || e) });
}
document.title = 'done';
