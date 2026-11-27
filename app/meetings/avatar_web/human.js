import * as THREE from "/three.module.js";

const { lerp, clamp, smoothstep } = THREE.MathUtils;
const signs = [-1, 1];

const faceRows = [
  [-1.00, .02, .18, .01], [-.95, .22, .32, -.13], [-.84, .40, .41, -.31],
  [-.66, .52, .45, -.43], [-.44, .60, .50, -.51], [-.20, .67, .52, -.58],
  [.05, .69, .54, -.61], [.25, .68, .54, -.63], [.47, .65, .53, -.61],
  [.66, .60, .47, -.56], [.84, .48, .37, -.46], [.98, .31, .24, -.35],
  [1.05, .02, -.01, -.08],
];

const torsoRows = [
  [-2.40, 1.23, .46, -.44], [-1.95, 1.28, .53, -.48], [-1.53, 1.34, .53, -.45],
  [-1.20, 1.37, .45, -.37], [-.98, 1.21, .32, -.31], [-.80, .72, .23, -.23],
  [-.68, .38, .18, -.19], [-.60, .27, .16, -.17],
];

const designs = {
  female: {
    skin: "#c68666", cheek: "#a95d57", lips: "#a55855", lipLight: "#bd6f66",
    hair: "#3e231d", hairLight: "#713f29", brow: "#452722", iris: "#476b54",
    jacket: "#35746f", lapel: "#4d8c82", shirt: "#e4d6c1", metal: "#d8ab69",
    jaw: .95, shoulder: .91, upper: .98, bob: true,
  },
  male: {
    skin: "#b77b61", cheek: "#9a594e", lips: "#915b50", lipLight: "#ab6c5e",
    hair: "#25242a", hairLight: "#47404a", brow: "#362a29", iris: "#4b4d69",
    jacket: "#354964", lapel: "#486181", shirt: "#bea58b", metal: "#a29486",
    jaw: 1.07, shoulder: 1.03, upper: 1.02, bob: false,
  },
};

function sampleRows(rows, y) {
  let i = 0;
  while (i < rows.length - 2 && y > rows[i + 1][0]) i++;
  const a = rows[Math.max(0, i - 1)];
  const b = rows[i];
  const c = rows[i + 1];
  const d = rows[Math.min(i + 2, rows.length - 1)];
  const t = clamp((y - b[0]) / (c[0] - b[0]), 0, 1);
  return [1, 2, 3].map((k) => {
    const m = (c[k] - a[k]) * (c[0] - b[0]) / (c[0] - a[0]);
    const n = (d[k] - b[k]) * (c[0] - b[0]) / (d[0] - b[0]);
    return lerp(b[k], c[k], t * t * (3 - 2 * t)) + (1 - t) * t * ((1 - t) * m - t * n);
  });
}

function gaussian(x, y, cx, cy, sx, sy) {
  return Math.exp(-(((x - cx) / sx) ** 2 + ((y - cy) / sy) ** 2));
}

function faceDetails(x, y) {
  const mirror = Math.abs(x);
  return (
    .090 * gaussian(x, y, 0, .035, .105, .30)
    + .174 * gaussian(x, y, 0, -.17, .145, .164)
    + .076 * gaussian(mirror, y, .107, -.212, .067, .068)
    - .058 * gaussian(mirror, y, .28, .174, .192, .131)
    + .057 * gaussian(mirror, y, .33, -.15, .215, .192)
    + .031 * gaussian(mirror, y, .27, .343, .18, .105)
    + .05 * gaussian(x, y, 0, -.455, .28, .24)
    + .033 * gaussian(x, y, 0, -.79, .26, .13)
  );
}

function faceWidth(design, width, y) {
  return width * lerp(design.jaw, design.upper, smoothstep(y, -.65, .45));
}

function front(design, x, y) {
  const [width, tip, back] = sampleRows(faceRows, y);
  const u = clamp(x / faceWidth(design, width, y), -1, 1);
  const cosine = Math.sqrt(1 - u * u);
  return (tip + back) / 2 + (tip - back) / 2 * cosine ** .48 + faceDetails(x, y);
}

function mesh(parent, geometry, material) {
  const result = new THREE.Mesh(geometry, material);
  parent.add(result);
  return result;
}

function material(color, options = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: .78, ...options });
}

const sphere = new THREE.SphereGeometry(1, 28, 20);
function oval(parent, paint, x, y, z, sx, sy, sz) {
  const result = mesh(parent, sphere, paint);
  result.position.set(x, y, z);
  result.scale.set(sx, sy, sz);
  return result;
}

function patch(parent, paint, columns, rows, { vertexColors = false, textured = false } = {}) {
  const count = (columns + 1) * (rows + 1);
  const points = new Float32Array(count * 3);
  const colors = vertexColors ? new Float32Array(count * 3) : null;
  const uvs = textured ? new Float32Array(count * 2) : null;
  const indices = [];
  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < columns; x++) {
      const i = y * (columns + 1) + x;
      indices.push(i, i + 1, i + columns + 1, i + 1, i + columns + 2, i + columns + 1);
    }
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(points, 3));
  if (colors) geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  if (uvs) geometry.setAttribute("uv", new THREE.BufferAttribute(uvs, 2));
  geometry.setIndex(indices);
  const result = mesh(parent, geometry, paint);
  result.frustumCulled = false;
  return (sample) => {
    for (let y = 0, i = 0; y <= rows; y++) {
      for (let x = 0; x <= columns; x++, i += 3) {
        const [px, py, pz, detail] = sample(x / columns, y / rows);
        points[i] = px;
        points[i + 1] = py;
        points[i + 2] = pz;
        if (colors) detail.toArray(colors, i);
        if (uvs) uvs.set(detail, i / 3 * 2);
      }
    }
    geometry.attributes.position.needsUpdate = true;
    if (colors) geometry.attributes.color.needsUpdate = true;
    if (uvs) geometry.attributes.uv.needsUpdate = true;
    geometry.computeVertexNormals();
  };
}

function tube(parent, paint, points, radius) {
  const path = new THREE.CatmullRomCurve3(points.map((p) => new THREE.Vector3(...p)));
  return mesh(parent, new THREE.TubeGeometry(path, 28, radius, 8, false), paint);
}

function buildFace(head, design) {
  const tone = new THREE.Color(design.skin);
  const blush = new THREE.Color(design.cheek);
  const shadow = new THREE.Color("#655354");
  const color = new THREE.Color();
  const face = patch(head, material("#ffffff", { vertexColors: true }), 80, 64, { vertexColors: true });
  face((s, t) => {
    const y = lerp(-1, 1.05, t);
    const a = (s - .5) * Math.PI * 2;
    const cosine = Math.cos(a);
    const [width, tip, back] = sampleRows(faceRows, y);
    const x = faceWidth(design, width, y) * Math.sin(a);
    const frontness = smoothstep(cosine, .02, .42);
    const z = (tip + back) / 2 + (tip - back) / 2 * Math.sign(cosine) * Math.abs(cosine) ** .48
      + faceDetails(x, y) * frontness;
    const cheek = gaussian(Math.abs(x), y, .44, -.13, .174, .19);
    const eye = gaussian(Math.abs(x), y, .29, .157, .18, .125);
    const nose = gaussian(x, y, 0, -.205, .095, .072);
    const beard = design.bob ? 0 : smoothstep(-y, .29, .83) * .22;
    color.copy(tone).lerp(blush, frontness * (.36 * cheek + .15 * eye + .20 * nose))
      .lerp(shadow, frontness * beard);
    return [x, y, z, color];
  });

  const skin = material(design.skin);
  const earInner = material(design.cheek);
  const nostril = material("#6e4540");
  for (const side of signs) {
    oval(head, skin, side * .631, -.08, -.005, .133, .228, .135).rotation.z = side * -.13;
    oval(head, earInner, side * .715, -.06, .093, .046, .132, .019).rotation.z = side * -.14;
    oval(head, skin, side * .688, -.08, .112, .028, .09, .021);
    const x = side * .088;
    oval(head, nostril, x, -.246, front(design, x, -.246) + .002, .025, .010, .009)
      .rotation.z = side * .23;
    if (design.bob) {
      const earring = mesh(head, new THREE.TorusGeometry(.072, .011, 9, 24),
        material(design.metal, { metalness: .6, roughness: .3 }));
      earring.position.set(side * .708, -.326, .104);
    }
  }
}

function buildHair(head, design) {
  const dark = new THREE.Color(design.hair);
  const warm = new THREE.Color(design.hairLight);
  const color = new THREE.Color();
  const hairPaint = material("#ffffff", { vertexColors: true, side: THREE.DoubleSide, roughness: .55 });
  patch(head, hairPaint, 100, 42, { vertexColors: true })((s, t) => {
    const a = (s - .5) * Math.PI * 2;
    const angle = Math.abs(a);
    const fall = smoothstep(angle, .52, 1.85);
    const fringe = .71 - .14 * Math.sin(2.3 * a + (design.bob ? .50 : -.70)) ** 2;
    const bottom = lerp(fringe, design.bob ? -1.03 - .080 * Math.cos(4 * a + .6) : -.29, fall);
    const y = lerp(bottom, 1.047, t);
    const [width, tip, back] = sampleRows(faceRows, y);
    const cosine = Math.cos(a);
    const wave = Math.sin(14 * a + 3 * t) * .009 + Math.sin(5 * a + 4 * t) * .014;
    const volume = (.044 + (design.bob ? .060 : .035) * fall + wave) * (1 - t ** 5);
    const sideWidth = design.bob && y < -.23 ? Math.max(width, .62 - .09 * smoothstep(-y, .31, 1.10)) : width;
    const x = (sideWidth * design.upper + volume) * Math.sin(a);
    const z = (tip + back) / 2 + ((tip - back) / 2 + volume) * Math.sign(cosine) * Math.abs(cosine) ** .54
      - (design.bob ? .15 * fall * smoothstep(-y, .12, .93) : 0);
    const crown = (design.bob ? .092 : .138) * t * t;
    color.copy(dark).lerp(warm, clamp(.12 + .17 * Math.sin(9 * a + 3.2 * t) ** 4 + .31 * t, 0, 1));
    return [x, y + crown, z, color];
  });

  const highlight = material(design.hairLight, { roughness: .65, side: THREE.DoubleSide });
  const base = material(design.hair, { roughness: .62, side: THREE.DoubleSide });

  // Each swept lock tapers into the scalp along its centerline.
  function lock(points, width, thickness, paint) {
    const guide = new THREE.CatmullRomCurve3(points.map((p) => new THREE.Vector3(...p)));
    patch(head, paint, 16, 23)((s, t) => {
      const p = guide.getPoint(t);
      const tangent = guide.getTangent(t);
      const angle = s * Math.PI * 2;
      const radius = Math.max(.035, Math.sin(Math.PI * t) ** .72);
      return [
        p.x - tangent.y * Math.sin(angle) * width * radius,
        p.y + tangent.x * Math.sin(angle) * width * radius,
        p.z + Math.cos(angle) * thickness * radius,
      ];
    });
  }

  if (design.bob) {
    lock([[.47, .90, .32], [.21, .93, .49], [-.19, .82, .61], [-.50, .61, .49], [-.59, .31, .30]], .124, .047, base);
    lock([[.53, .83, .35], [.65, .60, .32], [.69, .15, .27], [.64, -.34, .20], [.48, -.91, .03]], .126, .070, base);
    lock([[-.44, .83, .30], [-.65, .52, .31], [-.70, .07, .27], [-.63, -.43, .22], [-.60, -.91, .04]], .111, .079, base);
    lock([[.47, .91, .395], [.23, .941, .53], [-.18, .86, .62], [-.49, .63, .52]], .033, .018, highlight);
    lock([[.69, .51, .36], [.717, .2, .33], [.695, -.12, .28], [.62, -.6, .17]], .024, .019, highlight);
  } else {
    lock([[-.50, .72, .34], [-.35, .93, .405], [-.03, 1.08, .36], [.32, 1.075, .23], [.54, .91, .08]], .13, .061, base);
    lock([[-.44, .79, .46], [-.25, .97, .478], [.07, 1.063, .387], [.39, 1.03, .24]], .068, .037, highlight);
    lock([[-.30, .80, .48], [-.09, .914, .518], [.18, .998, .419], [.45, .97, .23]], .092, .033, base);
  }
}

function buildClothes(body, design) {
  const fabric = material(design.jacket);
  const fold = material(design.lapel);
  const shirt = material(design.shirt);
  const [start, end] = [torsoRows[0][0], torsoRows.at(-1)[0]];
  const chest = (x, y) => {
    const [width, tip, back] = sampleRows(torsoRows, y);
    const u = clamp(x / (width * design.shoulder), -.999, .999);
    return (tip + back) / 2 + (tip - back) / 2 * (1 - u * u) ** .37;
  };
  patch(body, fabric, 64, 32)((s, t) => {
    const y = lerp(start, end, t);
    const a = (s - .5) * Math.PI * 2;
    const [width, tip, back] = sampleRows(torsoRows, y);
    const cosine = Math.cos(a);
    return [design.shoulder * width * Math.sin(a), y,
      (tip + back) / 2 + (tip - back) / 2 * Math.sign(cosine) * Math.abs(cosine) ** .74];
  });
  oval(body, material(design.skin), 0, -.55, .013, design.bob ? .242 : .280, .43, .247);

  function panel(vertices, paint, lift) {
    const contour = vertices.map(([x, y]) => new THREE.Vector2(x, y));
    const surface = [];
    // Subdivide the lapels so they follow the curved jacket rather than cutting through it.
    function fill(a, b, c, depth) {
      if (!depth) {
        for (const { x, y } of [a, b, c]) surface.push(x, y, chest(x, y) + lift);
        return;
      }
      const ab = a.clone().lerp(b, .5);
      const bc = b.clone().lerp(c, .5);
      const ca = c.clone().lerp(a, .5);
      fill(a, ab, ca, depth - 1);
      fill(ab, b, bc, depth - 1);
      fill(ca, bc, c, depth - 1);
      fill(ab, bc, ca, depth - 1);
    }
    for (const triangle of THREE.ShapeUtils.triangulateShape(contour, [])) {
      fill(contour[triangle[0]], contour[triangle[1]], contour[triangle[2]], 3);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(surface, 3));
    geometry.computeVertexNormals();
    paint.side = THREE.DoubleSide;
    mesh(body, geometry, paint);
  }

  patch(body, shirt, 28, 24)((s, t) => {
    const u = s * 2 - 1;
    const x = u * lerp(.28, .34, t);
    const neckline = -.79 - (design.bob ? .16 : .088) * (1 - u * u);
    const y = lerp(-2.4, neckline, t);
    return [x, y, chest(x, y) + .031];
  });
  for (const side of signs) {
    panel([[.29, -.77], [.63, -.88], [.49, -1.24], [.66, -1.34], [.31, -1.61], [.06, -2.12], [.30, -1.25]]
      .map(([x, y]) => [x * side, y]), fold, .049);
  }
  if (design.bob) {
    const gold = material(design.metal, { metalness: .45, roughness: .32 });
    tube(body, gold, [[-.227, -.799, .276], [-.13, -.882, .316], [0, -.919, .333], [.13, -.882, .316], [.227, -.799, .276]], .009);
    oval(body, gold, 0, -.928, .343, .034, .042, .014);
  } else {
    tube(body, material(design.metal), [[.65, -1.49, .447], [.78, -1.491, .43], [.92, -1.50, .365]], .009);
  }
}

function eyeTexture(iris) {
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 256;
  const ink = canvas.getContext("2d");
  const white = ink.createRadialGradient(125, 123, 17, 124, 124, 156);
  white.addColorStop(0, "#fff4e7");
  white.addColorStop(1, "#a18a86");
  ink.fillStyle = white;
  ink.fillRect(0, 0, 256, 256);

  const irisLight = new THREE.Color(iris).lerp(new THREE.Color("#c3a682"), .37).getStyle();
  const ring = ink.createRadialGradient(127, 123, 15, 128, 128, 50);
  ring.addColorStop(0, "#222027");
  ring.addColorStop(.66, irisLight);
  ring.addColorStop(.88, iris);
  ring.addColorStop(1, "#2b2930");
  ink.fillStyle = ring;
  ink.beginPath();
  ink.arc(128, 128, 50, 0, Math.PI * 2);
  ink.fill();
  for (let i = 0; i < 70; i++) {
    const angle = i * Math.PI * 2 / 70;
    ink.strokeStyle = i % 5 === 0 ? "#ddbe855e" : "#29202d53";
    ink.beginPath();
    ink.moveTo(128 + Math.cos(angle) * 28, 128 + Math.sin(angle) * 28);
    ink.lineTo(128 + Math.cos(angle + .062) * (42 + 5 * Math.sin(i * 3)), 128 + Math.sin(angle + .062) * 44);
    ink.stroke();
  }
  ink.fillStyle = "#171920";
  ink.beginPath();
  ink.arc(128, 128, 24, 0, Math.PI * 2);
  ink.fill();
  ink.fillStyle = "#fff2dcd9";
  ink.beginPath();
  ink.ellipse(113, 110, 7, 5.5, -.2, 0, Math.PI * 2);
  ink.fill();
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function buildEyes(head, design) {
  const eyePaint = material("#ffffff", { map: eyeTexture(design.iris), roughness: .58 });
  const lidPaint = material(design.brow);
  const browPaint = material(design.brow);
  return signs.map((side) => {
    const eye = patch(head, eyePaint, 28, 15, { textured: true });
    const lash = patch(head, lidPaint, 24, 2);
    const brow = patch(head, browPaint, 24, 3);
    return ({ open, browLift, browTilt, gazeX, gazeY }) => {
      const width = design.bob ? .174 : .164;
      const center = side * .281;
      const height = (design.bob ? .082 : .073) * open;
      const eyeBase = (dx) => .169 + side * dx * (design.bob ? .14 : .07);
      const aperture = (x) => Math.max(0, 1 - x * x) ** .65;
      eye((s, t) => {
        const xn = s * 2 - 1;
        const dx = width * xn;
        const dy = height * aperture(xn) * (t * 2 - 1) * (t > .5 ? 1 : .71);
        const x = center + dx;
        const y = eyeBase(dx) + dy;
        const depth = front(design, x, y) + .008 + .064 * aperture(xn) * (1 - Math.abs(t * 2 - 1)) ** .64;
        const uv = [.5 + (dx - gazeX) / .37, .5 + (dy - gazeY) / .37];
        return [x, y, depth, uv];
      });
      lash((s, t) => {
        const xn = s * 2 - 1;
        const dx = width * xn;
        const x = center + dx;
        const y = eyeBase(dx) + height * aperture(xn) + (t - .5) * .022 * aperture(xn);
        return [x, y, front(design, x, y) + .015];
      });
      brow((s, t) => {
        const xn = s * 2 - 1;
        const x = center + xn * .184;
        const tail = side * xn;
        const y = .322 + browLift + .065 * (1 - xn ** 2) - .013 * tail + browTilt * tail
          + (t - .5) * (design.bob ? .046 : .069) * aperture(xn) * (1 - tail * .25);
        return [x, y, front(design, x, y) + .018 + .006 * Math.sin(t * Math.PI)];
      });
    };
  });
}

function buildMouth(head, design) {
  const lipTop = patch(head, material(design.lips), 32, 4);
  const lipBottom = patch(head, material(design.lipLight), 32, 4);
  const inside = patch(head, material("#452226"), 32, 6);
  const teeth = patch(head, material("#e8d5ba"), 24, 2);
  return ({ smile, mouthOpen }) => {
    const w = (design.bob ? .270 : .281) * (1 + smile * 1.42 - mouthOpen * .11);
    const opening = .009 + mouthOpen * .135;
    const point = (u, v, type) => {
      const dx = u * 2 - 1;
      const cushion = (1 - dx * dx) ** .75;
      const x = dx * w;
      const center = -.458 + smile * dx * dx;
      const upper = center + opening * .37 * cushion;
      const lower = center - opening * .85 * cushion;
      const peak = (.027 + .016 * Math.exp(-((Math.abs(dx) - .27) ** 2) / .035)) * cushion;
      let y = lerp(lower, upper, v);
      let z = .016;
      if (type === "top") {
        y = lerp(upper, upper + peak, v);
        z = lerp(.053, .009, v) + .017 * Math.sin(v * Math.PI) * cushion;
      } else if (type === "bottom") {
        y = lerp(lower - cushion * .046, lower, v);
        z = lerp(.01, .055, v) + .022 * Math.sin(v * Math.PI) * cushion;
      } else if (type === "teeth") {
        y = lerp(upper - Math.min(opening * .46, .043) * cushion, upper - .003, v);
        z = .035;
      }
      return [x, y, front(design, x, y) + z];
    };
    lipTop((s, t) => point(s, t, "top"));
    lipBottom((s, t) => point(s, t, "bottom"));
    inside((s, t) => point(s, t, "inside"));
    teeth((s, t) => point(.10 + s * .80, t, "teeth"));
  };
}

export function makeHuman(scene, character) {
  const design = designs[character];
  const body = new THREE.Group();
  const head = new THREE.Group();
  head.position.set(0, .56, .025);
  body.add(head);
  scene.add(body);
  buildClothes(body, design);
  buildFace(head, design);
  buildHair(head, design);
  const eyes = buildEyes(head, design);
  const mouth = buildMouth(head, design);
  const expression = { open: 1, browLift: 0, browTilt: 0, gazeX: 0, gazeY: 0, smile: .01, mouthOpen: 0 };
  const express = (target, ease) => {
    for (const key in expression) expression[key] = lerp(expression[key], target[key], ease);
    for (const eye of eyes) eye(expression);
    mouth(expression);
  };
  express(expression, 1);
  return { body, head, express };
}
