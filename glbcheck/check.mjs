// node glbcheck/check.mjs file.glb  -> JSON summary on stdout
import { readFileSync } from 'node:fs';
import { parseGlb, summarise } from './inspect.mjs';
const buf = readFileSync(process.argv[2]);
const gltf = await parseGlb(buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength));
process.stdout.write(JSON.stringify(summarise(gltf)));
