// Cross-check the phone's sensor math against pc/sensors.py.
//
//   python tools/test_web_grid.py dump     (writes the grid)
//   node tools/test_web_grid.js            (compares it)
//
// The grid file holds sensor names computed by the Python side for two panels: the
// whole frame and a detected (off-centre, stretched) circle.  This script extracts
// toPanel()/fromPanel()/sensorAt()/sensorPoint() out of web/app.js and recomputes them.
const fs = require("fs");
const path = require("path");

const here = __dirname;
const gridPath = path.join(here, "probe_out", "web_grid.json");
if (!fs.existsSync(gridPath)) {
  console.error(`[!] ${gridPath} missing - run: python tools/test_web_grid.py dump`);
  process.exit(2);
}

const src = fs.readFileSync(path.join(here, "..", "web", "app.js"), "utf8");
function extract(name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`cannot find ${name} in web/app.js`);
  // brace matching from the first '{' after the signature
  let i = src.indexOf("{", start);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}") {
      depth--;
      if (depth === 0) return src.slice(start, j + 1);
    }
  }
  throw new Error(`unbalanced braces for ${name}`);
}

const factory = new Function(
  `${extract("toPanel")}\n${extract("fromPanel")}\n${extract("sensorAt")}\n${extract("sensorPoint")}\n` +
    "return { toPanel, fromPanel, sensorAt, sensorPoint };"
);
const { sensorAt, sensorPoint } = factory();

const BASE = { r_centre: 0.2241, r_inner: 0.5518, r_max: 1.0, sectors: 8 };
const FULL = Object.assign({ cx: 0.5, cy: 0.5, rx: 0.5, ry: 0.5 }, BASE);

const data = JSON.parse(fs.readFileSync(gridPath, "utf8"));

let checked = 0;
let mismatch = 0;

function model(panel) {
  if (!panel) return FULL;
  return Object.assign({}, BASE, panel);
}

for (const [key, dataset] of Object.entries({ full: data.full, detected: data.detected })) {
  const panel = model(dataset.panel);
  for (const [x, y, expected] of dataset.grid) {
    const got = sensorAt(x, y, panel);
    const name = got === null ? null : got.name;
    checked++;
    if (name !== expected) {
      mismatch++;
      if (mismatch <= 10) console.log(`[x] ${key} (${x}, ${y}) web=${name} py=${expected}`);
    }
  }
  // the PC's representative point for every sensor must map back to that sensor
  let snapMismatch = 0;
  for (const [name, position] of dataset.sensor_points) {
    const back = sensorAt(position[0], position[1], panel);
    const backName = back === null ? null : back.name;
    checked++;
    if (backName !== name) {
      snapMismatch++;
      mismatch++;
      if (mismatch <= 10) {
        console.log(`[x] ${key} point ${name} (${position[0]}, ${position[1]}) -> ${backName}`);
      }
    }
    // and the phone's own snap point must agree with the PC's
    const sensor = name === "C" ? { name: "C", sector: -1 } : { name, sector: Number(name.slice(1)) / 45 };
    const [px, py] = sensorPoint(sensor, panel);
    checked++;
    if (Math.abs(px - position[0]) > 1e-6 || Math.abs(py - position[1]) > 1e-6) {
      mismatch++;
      if (mismatch <= 10) {
        console.log(
          `[x] ${key} snap ${name} web=(${px.toFixed(6)}, ${py.toFixed(6)}) py=(${position[0]}, ${position[1]})`
        );
      }
    }
  }
  console.log(`${key.padEnd(9)} panel: ${checked} comparisons, ${snapMismatch} bad round trips`);
}

console.log(`comparisons : ${checked}`);
console.log(`mismatches  : ${mismatch}`);
console.log(mismatch === 0 ? "WEBGRID: PASS" : "WEBGRID: FAIL");
process.exit(mismatch === 0 ? 0 : 1);
