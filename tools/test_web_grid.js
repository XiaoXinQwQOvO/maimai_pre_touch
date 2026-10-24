// Cross-check the phone's sensor math against pc/sensors.py.
//
//   python -c "import tools.test_web_grid as t; t.dump()"   (writes the grid)
//   node tools/test_web_grid.js                            (compares it)
//
// The grid file holds sensor names computed by the Python side; this script
// extracts sensorAt()/sensorPoint() out of web/app.js and recomputes them.
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

const PANEL = { r_centre: 0.2241, r_inner: 0.5518, r_max: 1.0, sectors: 8 };
const factory = new Function(
  `${extract("sensorAt")}\n${extract("sensorPoint")}\nreturn { sensorAt, sensorPoint };`
);
const { sensorAt, sensorPoint } = factory();

const data = JSON.parse(fs.readFileSync(gridPath, "utf8"));
let mismatch = 0;
for (const [x, y, expected] of data.grid) {
  const got = sensorAt(x, y, PANEL);
  const name = got === null ? null : got.name;
  if (name !== expected) {
    mismatch++;
    if (mismatch <= 10) console.log(`[x] (${x}, ${y}) web=${name} py=${expected}`);
  }
}

// snapped points must map back to the same sensor
let snapMismatch = 0;
for (const name of data.labels) {
  const sensor = name === "C" ? { name: "C", sector: -1 } : { name, sector: Number(name.slice(1)) / 45 };
  const [px, py] = sensorPoint(sensor, PANEL);
  const back = sensorAt(px, py, PANEL);
  const backName = back === null ? null : back.name;
  if (backName !== name) {
    snapMismatch++;
    if (snapMismatch <= 10) console.log(`[x] snap ${name} -> (${px.toFixed(4)}, ${py.toFixed(4)}) -> ${backName}`);
  }
}

const total = data.grid.length + data.labels.length;
const bad = mismatch + snapMismatch;
console.log(`grid points compared : ${data.grid.length}`);
console.log(`snap round trips     : ${data.labels.length}`);
console.log(`mismatches           : ${bad}`);
console.log(bad === 0 ? "WEBGRID: PASS" : "WEBGRID: FAIL");
process.exit(bad === 0 ? 0 : 1);
