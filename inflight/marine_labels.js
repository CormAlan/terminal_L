// Extracts sea/ocean label points from cached vector tiles.
// Usage: node marine_labels.js <tile dir> <z> <x-y> [x-y ...]  -> JSON [{name, lat, lon, rank}]
'use strict';
const fs = require('fs');
const path = require('path');
const zlib = require('zlib');
const { VectorTile } = require('@mapbox/vector-tile');
const Protobuf = require('pbf');

const [dir, zs, ...tiles] = process.argv.slice(2);
const z = Number(zs);
const out = [];
for (const t of tiles) {
  const [x, y] = t.split('-').map(Number);
  let buf;
  try { buf = fs.readFileSync(path.join(dir, zs, `${t}.pbf`)); } catch (e) { continue; }
  if (buf[0] === 0x1f && buf[1] === 0x8b) buf = zlib.gunzipSync(buf);
  const layer = new VectorTile(new Protobuf(buf)).layers.marine_label;
  if (!layer) continue;
  for (let i = 0; i < layer.length; i++) {
    const f = layer.feature(i);
    const name = f.properties.name_en || f.properties.name;
    const p = f.loadGeometry()[0][0];
    const n = Math.PI - 2 * Math.PI * (y + p.y / layer.extent) / Math.pow(2, z);
    out.push({
      name,
      rank: f.properties.labelrank || 0,
      lon: (x + p.x / layer.extent) / Math.pow(2, z) * 360 - 180,
      lat: 180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))),
    });
  }
}
process.stdout.write(JSON.stringify(out));
