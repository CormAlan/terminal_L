// Renders MapSCII frames from locally cached vector tiles, with the flight route drawn on top.
// Protocol: one JSON request per line on stdin, one JSON response per line on stdout.
'use strict';
const fs = require('fs');
const path = require('path');
const readline = require('readline');
const bresenham = require('bresenham');

const MS = path.join(__dirname, '..', 'node_modules', 'mapscii', 'src');
const config = require(path.join(MS, 'config'));
config.delimeter = '\n';
config.language = 'en';
const Renderer = require(path.join(MS, 'Renderer'));
const Tile = require(path.join(MS, 'Tile'));
const utils = require(path.join(MS, 'utils'));

// MapSCII logs render errors with console.log, which would corrupt the protocol
console.log = (...a) => process.stderr.write(a.join(' ') + '\n');

const TILE_DIR = process.argv[2] || path.join(__dirname, '..', 'data', 'tiles');
const EMPTY = { layers: {} };

class LocalTiles {
  constructor(dir) { this.dir = dir; this.cache = new Map(); }
  useStyler(styler) { this.styler = styler; }
  getTile(z, x, y) {
    const key = `${z}-${x}-${y}`;
    if (this.cache.has(key)) return Promise.resolve(this.cache.get(key));
    let buf;
    try { buf = fs.readFileSync(path.join(this.dir, String(z), `${x}-${y}.pbf`)); }
    catch (e) { return Promise.resolve(EMPTY); }
    return new Tile(this.styler).load(buf).then((tile) => {
      this.cache.set(key, tile);
      if (this.cache.size > 48) this.cache.delete(this.cache.keys().next().value);
      return tile;
    }).catch(() => EMPTY);
  }
}

class FlightRenderer extends Renderer {
  _renderTiles(tiles) {
    if (tiles.length) super._renderTiles(tiles);
    if (this.overlay) this.overlay(this.canvas);
  }
  _getFrame() { return this.canvas.frame(); }
}

const style = JSON.parse(fs.readFileSync(path.join(MS, '..', 'styles', 'dark.json'), 'utf8'));
const renderer = new FlightRenderer(null, new LocalTiles(TILE_DIR), style);

function projector(center, zoom, width, height) {
  const z = utils.baseZoom(zoom);
  const size = utils.tilesizeAtZoom(zoom);
  const c = utils.ll2tile(center.lon, center.lat, z);
  return ([lat, lon]) => {
    const t = utils.ll2tile(lon, Math.max(-85, Math.min(85, lat)), z);
    return { x: Math.round(width / 2 + (t.x - c.x) * size), y: Math.round(height / 2 + (t.y - c.y) * size) };
  };
}

// A braille cell has one colour, so clear the cells under the route first:
// otherwise the route recolours the whole cell (e.g. dotted ocean fill) and looks blocky.
function routePixels(pts, dashed) {
  const out = [];
  let n = 0;
  for (let i = 1; i < pts.length; i++) {
    const a = pts[i - 1], b = pts[i];
    if (Math.abs(a.x - b.x) > 4000 || Math.abs(a.y - b.y) > 4000) continue;
    bresenham(a.x, a.y, b.x, b.y, (x, y) => {
      if (!dashed || (n++ % 6) < 3) out.push([x, y]);
    });
  }
  return out;
}

function clearCells(buffer, pixels) {
  for (const [x, y] of pixels) {
    if (x < 0 || y < 0 || x >= buffer.width || y >= buffer.height) continue;
    const idx = buffer._project(x, y);
    buffer.pixelBuffer[idx] = 0;
    buffer.charBuffer[idx] = undefined;
  }
}

function render(req) {
  const width = Math.max(2, req.cols * 2), height = Math.max(4, req.rows * 4);
  renderer.setSize(width, height);
  const proj = projector(req.center, req.zoom, width, height);
  renderer.overlay = (canvas) => {
    const buf = canvas.buffer;
    const remaining = routePixels((req.remaining || []).map(proj), true);
    const flown = routePixels((req.flown || []).map(proj), false);
    clearCells(buf, remaining.concat(flown));
    for (const [x, y] of remaining) buf.setPixel(x, y, 44);
    for (const [x, y] of flown) buf.setPixel(x, y, 208);
    for (const m of req.marks || []) {
      const p = proj([m.lat, m.lon]);
      canvas.text(m.label, p.x - m.label.length, p.y, m.color || 231);
    }
    if (req.plane) {
      const p = proj([req.plane.lat, req.plane.lon]);
      const arrows = ['↑', '↗', '→', '↘', '↓', '↙', '←', '↖'];
      const ch = arrows[Math.round(((req.plane.heading % 360) + 360) % 360 / 45) % 8];
      canvas.text('✈' + ch, p.x, p.y, 226);
    }
  };
  return renderer.draw(req.center, req.zoom);
}

let queue = Promise.resolve();
readline.createInterface({ input: process.stdin }).on('line', (line) => {
  queue = queue.then(async () => {
    let req;
    try {
      req = JSON.parse(line);
      const frame = await render(req);
      process.stdout.write(JSON.stringify({ id: req.id, frame: frame || '' }) + '\n');
    } catch (e) {
      process.stdout.write(JSON.stringify({ id: req && req.id, error: String(e && e.stack || e) }) + '\n');
    }
  });
});
