// Lecteurs PLY et OBJ de la visionneuse.
//
// Lance par tests/test_viewer.py, ou directement :  node tests/test_viewer.js
// Seuls les lecteurs sont testables hors navigateur ; le rendu WebGL ne l'est
// pas, mais c'est l'arithmetique d'offsets qui est fragile, pas les shaders.

global.window = {};
require(require('path').join(__dirname, '..', 'app', 'static', 'viewer.js'));
const { lirePly, lireObj } = window.PhotoGramViewer;

function plyBinaire(avecFaces) {
  const sommets = [
    { x: 1, y: 2, z: 3, r: 255, v: 0, b: 0 },
    { x: 4, y: 5, z: 6, r: 0, v: 255, b: 0 },
    { x: 7, y: 8, z: 9, r: 0, v: 0, b: 255 },
  ];
  let e = 'ply\nformat binary_little_endian 1.0\n';
  e += `element vertex ${sommets.length}\n`;
  e += 'property float x\nproperty float y\nproperty float z\n';
  e += 'property float nx\nproperty float ny\nproperty float nz\n';
  e += 'property uchar red\nproperty uchar green\nproperty uchar blue\n';
  if (avecFaces) e += 'element face 1\nproperty list uchar int vertex_indices\n';
  e += 'end_header\n';
  const buf = Buffer.alloc(Buffer.byteLength(e) + sommets.length * 27 + (avecFaces ? 13 : 0));
  buf.write(e, 0, 'ascii');
  let o = Buffer.byteLength(e);
  for (const s of sommets) {
    buf.writeFloatLE(s.x, o); o += 4; buf.writeFloatLE(s.y, o); o += 4; buf.writeFloatLE(s.z, o); o += 4;
    buf.writeFloatLE(0.5, o); o += 4; buf.writeFloatLE(0.5, o); o += 4; buf.writeFloatLE(0.5, o); o += 4;
    buf.writeUInt8(s.r, o++); buf.writeUInt8(s.v, o++); buf.writeUInt8(s.b, o++);
  }
  if (avecFaces) {
    buf.writeUInt8(3, o++);
    buf.writeInt32LE(0, o); o += 4; buf.writeInt32LE(1, o); o += 4; buf.writeInt32LE(2, o);
  }
  return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
}

let echecs = 0;
function verifier(nom, obtenu, attendu) {
  const ok = JSON.stringify(obtenu) === JSON.stringify(attendu);
  console.log(`  ${ok ? '[ok]' : '[KO]'} ${nom}` + (ok ? '' : `\n       attendu ${JSON.stringify(attendu)}\n       obtenu  ${JSON.stringify(obtenu)}`));
  if (!ok) echecs++;
}

console.log('PLY binaire avec normales intercalees :');
let g = lirePly(plyBinaire(false));
verifier('positions exactes malgre les normales sautees',
  Array.from(g.positions), [1,2,3,4,5,6,7,8,9]);
verifier('couleurs normalisees',
  Array.from(g.couleurs).map(v => Math.round(v * 255)), [255,0,0, 0,255,0, 0,0,255]);

console.log('PLY binaire avec faces :');
g = lirePly(plyBinaire(true));
verifier('faces lues apres les sommets', Array.from(g.indices), [0,1,2]);
verifier('positions intactes', Array.from(g.positions), [1,2,3,4,5,6,7,8,9]);

console.log('PLY ascii :');
const ascii = 'ply\nformat ascii 1.0\nelement vertex 2\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n1 2 3 255 128 0\n4 5 6 0 0 255\n';
g = lirePly(new TextEncoder().encode(ascii).buffer);
verifier('positions', Array.from(g.positions), [1,2,3,4,5,6]);
verifier('couleurs', Array.from(g.couleurs).map(v => Math.round(v*255)), [255,128,0, 0,0,255]);

console.log('OBJ :');
g = lireObj('v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 3\nf 2 4 3\n');
verifier('triangles', Array.from(g.indices), [0,1,2, 1,3,2]);
verifier('couleur neutre fournie', g.couleurs.length, 12);
g = lireObj('v 0 0 0\nv 1 0 0\nv 0 1 0\nv 1 1 0\nf 1 2 4 3\n');
verifier('quadrilatere triangule', Array.from(g.indices), [0,1,3, 0,3,2]);

process.exit(echecs ? 1 : 0);
