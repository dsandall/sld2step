// Shared by the node check (check.mjs) and the browser page (entry.mjs):
// load a .glb with three r180 GLTFLoader + MeshoptDecoder and summarise it.
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';

export async function parseGlb(arrayBuffer) {
  await MeshoptDecoder.ready;
  const loader = new GLTFLoader();
  loader.setMeshoptDecoder(MeshoptDecoder);
  return await new Promise((res, rej) => loader.parse(arrayBuffer, '', res, rej));
}

export function summarise(gltf) {
  const scene = gltf.scene;
  scene.updateMatrixWorld(true);
  const named = [];
  const geoms = new Set();
  let meshes = 0, placedTris = 0, uniqueTris = 0;
  scene.traverse((o) => {
    if (o.userData && o.userData.kind) {
      named.push({ name: o.userData.name, kind: o.userData.kind, component: o.userData.component, id: o.userData.id });
    }
    if (o.isMesh) {
      meshes++;
      const g = o.geometry;
      const t = (g.index ? g.index.count : g.attributes.position.count) / 3;
      placedTris += t;
      if (!geoms.has(g)) { geoms.add(g); uniqueTris += t; }
      // every mesh must hang below a named part/body node
      let p = o, owner = null;
      while (p) { if (p.userData && (p.userData.kind === 'part' || p.userData.kind === 'body')) { owner = p; break; } p = p.parent; }
      if (!owner) named.push({ orphanMesh: o.name });
    }
  });
  const box = new THREE.Box3().setFromObject(scene, true);
  return {
    sceneExtras: scene.userData,
    named, meshes, uniqueGeometries: geoms.size, uniqueTris, placedTris,
    bbox_mm: [...box.min.toArray(), ...box.max.toArray()].map((x) => Math.round(x * 1e6) / 1e3),
  };
}
