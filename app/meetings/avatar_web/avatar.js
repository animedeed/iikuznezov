import * as THREE from "/three.module.js";

const demo = new URLSearchParams(location.search).has("demo");
const status = document.querySelector("#status");
const controls = document.querySelector("#controls");
const names = {
  idle: "Ожидает", listening: "Слушает", thinking: "Обдумывает", speaking: "Говорит",
  joy: "радость", surprise: "удивление", concern: "озабоченность",
};
const poses = {
  idle: { body: 0, nod: .03, arm: 0, yaw: -.05 },
  listening: { body: .12, nod: .10, arm: .04, yaw: -.32 },
  thinking: { body: -.13, nod: -.18, arm: .48, yaw: .26 },
  speaking: { body: .035, nod: -.02, arm: .39, yaw: 0 },
};
const expressions = {
  neutral: { hue: 0x94f0cc, eyes: .77, brow: .07, lift: 0, smile: .06, tilt: 0 },
  joy: { hue: 0xa9f6b3, eyes: .46, brow: .035, lift: .11, smile: .21, tilt: -.07 },
  surprise: { hue: 0xffd788, eyes: 1.38, brow: 0, lift: .18, smile: -.07, tilt: .03 },
  concern: { hue: 0xffb489, eyes: .72, brow: -.37, lift: .06, smile: -.14, tilt: .12 },
};
let state = { phase: "idle", emotion: "neutral", levels: [] };
let receivedAt = 0;
let changedAt = 0;
let lastPoll = -Infinity;

function update(next) {
  if (!poses[next.phase] || !expressions[next.emotion]) return;
  if (state.phase !== next.phase || state.emotion !== next.emotion) changedAt = performance.now();
  state = next;
  receivedAt = performance.now();
  if (demo) {
    status.textContent = [names[state.phase], names[state.emotion]].filter(Boolean).join(" · ");
    for (const button of controls.querySelectorAll("button")) {
      button.setAttribute("aria-pressed", String(
        button.dataset.phase === state.phase && button.dataset.emotion === state.emotion,
      ));
    }
  }
}

if (demo) {
  document.body.classList.add("preview");
  controls.hidden = false;
  controls.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button) update({ phase: button.dataset.phase, emotion: button.dataset.emotion, levels: [] });
  });
  update(state);
}

async function poll() {
  try {
    const response = await fetch("/state", { cache: "no-store", signal: AbortSignal.timeout(700) });
    if (response.ok) {
      update(await response.json());
      lastPoll = performance.now();
    }
  } catch {
    if (performance.now() - lastPoll > 1000) update({ phase: "idle", emotion: "neutral", levels: [] });
  } finally {
    setTimeout(poll, 90);
  }
}

function roundBox(width, height, depth, radius) {
  const x = -width / 2, y = -height / 2;
  const shape = new THREE.Shape();
  shape.moveTo(x + radius, y);
  shape.lineTo(-x - radius, y);
  shape.quadraticCurveTo(-x, y, -x, y + radius);
  shape.lineTo(-x, -y - radius);
  shape.quadraticCurveTo(-x, -y, -x - radius, -y);
  shape.lineTo(x + radius, -y);
  shape.quadraticCurveTo(x, -y, x, -y - radius);
  shape.lineTo(x, y + radius);
  shape.quadraticCurveTo(x, y, x + radius, y);
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth, bevelEnabled: true, bevelSegments: 4, bevelSize: .055, bevelThickness: .05, curveSegments: 12,
  });
  geometry.translate(0, 0, -depth / 2);
  return geometry;
}

function addMesh(parent, geometry, material, x = 0, y = 0, z = 0) {
  const mesh = new THREE.Mesh(geometry, material);
  mesh.position.set(x, y, z);
  parent.add(mesh);
  return mesh;
}

function ball(parent, material, x, y, z, sx, sy, sz) {
  const mesh = addMesh(parent, new THREE.SphereGeometry(1, 24, 16), material, x, y, z);
  mesh.scale.set(sx, sy, sz);
  return mesh;
}

function makeAvatar(scene) {
  const body = new THREE.Group();
  scene.add(body);
  const head = new THREE.Group();
  head.position.y = .68;
  body.add(head);
  const enamel = new THREE.MeshPhysicalMaterial({
    color: 0xe3a956, metalness: .3, roughness: .29, clearcoat: .62, clearcoatRoughness: .18,
  });
  const cream = new THREE.MeshStandardMaterial({ color: 0xffe1a1, roughness: .35, metalness: .18 });
  const trim = new THREE.MeshStandardMaterial({ color: 0x3b564e, roughness: .43, metalness: .58 });
  const dark = new THREE.MeshPhysicalMaterial({ color: 0x0e2020, roughness: .24, metalness: .33, clearcoat: .8 });
  const light = new THREE.MeshBasicMaterial({ color: 0x94f0cc });
  const shade = new THREE.MeshBasicMaterial({ color: 0x081414 });

  addMesh(body, roundBox(1.52, 1.24, .64, .33), trim, 0, -.84);
  addMesh(body, roundBox(1.36, .99, .26, .29), enamel, 0, -.77, .39);
  addMesh(body, roundBox(.53, .33, .09, .10), dark, 0, -.77, .59);
  for (let index = 0; index < 3; index++) {
    addMesh(body, roundBox(.045, .095, .01, .014), index === 1 ? cream : light, (index - 1) * .13, -.77, .67);
  }
  addMesh(body, new THREE.CylinderGeometry(.34, .41, .39, 36), trim, 0, -.08);

  const arms = [-1, 1].map((side) => {
    const arm = new THREE.Group();
    arm.position.set(side * .78, -.49, 0);
    body.add(arm);
    ball(arm, trim, 0, 0, 0, .23, .23, .25);
    const outer = addMesh(arm, new THREE.CapsuleGeometry(.13, .39, 6, 18), enamel, side * .12, -.26, 0);
    outer.rotation.z = side * .39;
    ball(arm, cream, side * .20, -.53, .04, .17, .19, .17);
    return arm;
  });

  addMesh(head, roundBox(2.28, 1.54, .96, .46), enamel);
  addMesh(head, roundBox(2.00, 1.27, .20, .38), cream, 0, -.01, .51);
  addMesh(head, roundBox(1.84, 1.10, .15, .34), dark, 0, -.01, .65);
  const glass = new THREE.MeshBasicMaterial({ color: 0x97fff0, transparent: true, opacity: .18 });
  const highlight = ball(head, glass, -.44, .36, .787, .42, .025, .015);
  highlight.rotation.z = .09;

  for (const side of [-1, 1]) {
    const ear = addMesh(head, new THREE.CylinderGeometry(.27, .27, .18, 32), trim, side * 1.19, .02, 0);
    ear.rotation.z = Math.PI / 2;
    ball(head, cream, side * 1.30, .02, 0, .072, .18, .18);
    ball(head, light, side * 1.36, .02, 0, .033, .089, .089);
  }

  const antenna = new THREE.Group();
  antenna.position.set(.62, .72, -.08);
  head.add(antenna);
  addMesh(antenna, new THREE.CylinderGeometry(.045, .055, .39, 16), trim, 0, .16);
  ball(antenna, cream, 0, .38, 0, .123, .123, .123);
  ball(antenna, light, 0, .40, .07, .074, .074, .052);
  const eyes = [-1, 1].map((side) => ball(head, light, side * .37, .096, .79, .115, .17, .047));
  const brows = [-1, 1].map((side) => {
    const brow = addMesh(head, new THREE.CapsuleGeometry(.026, .17, 5, 12), light, side * .37, .33, .81);
    brow.rotation.z = Math.PI / 2;
    return brow;
  });
  const blush = new THREE.MeshBasicMaterial({ color: 0xffad82, transparent: true, opacity: 0 });
  for (const side of [-1, 1]) {
    ball(head, blush, side * .57, -.14, .81, .096, .041, .024);
  }
  const mouth = ball(head, shade, 0, -.267, .79, .24, .03, .046);
  const inner = ball(head, light, 0, -.277, .825, .18, .02, .027);
  const grin = Array.from({ length: 11 }, (_, i) =>
    ball(head, light, (i - 5) * .039, -.28, .82, .023, .023, .015),
  );

  return { body, head, arms, antenna, eyes, brows, mouth, inner, grin, light, blush };
}

function start() {
  const canvas = document.querySelector("#portrait");
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setClearColor(0, 0);
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.25;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(33, 1, .1, 60);
  scene.add(new THREE.HemisphereLight(0xd2f9ff, 0x2b1a09, 2.1));
  function spotlight(color, strength, x, y, z) {
    const lamp = new THREE.DirectionalLight(color, strength);
    lamp.position.set(x, y, z);
    scene.add(lamp);
  }
  spotlight(0xffd6a5, 3.7, -3, 6, 5);
  spotlight(0xbafff1, 3.1, 3, 2, -3);
  spotlight(0xbad9fe, .9, 5, 1, 3);

  const avatar = makeAvatar(scene);
  const halo = addMesh(scene, new THREE.TorusGeometry(1.79, .014, 8, 144),
    new THREE.MeshBasicMaterial({ color: 0x8cbcac, transparent: true, opacity: .34 }), 0, .36, -.86);
  halo.scale.y = .83;
  const base = new THREE.MeshStandardMaterial({ color: 0x31443f, metalness: .32, roughness: .51 });
  addMesh(scene, new THREE.CylinderGeometry(1.22, 1.34, .13, 72), base, 0, -1.55, 0);
  const edge = addMesh(scene, new THREE.TorusGeometry(1.25, .013, 8, 96),
    new THREE.MeshBasicMaterial({ color: 0x77afa1 }), 0, -1.53, 0);
  edge.rotation.x = Math.PI / 2;

  const targetColor = new THREE.Color();
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const resize = () => {
    const { clientWidth: width, clientHeight: height } = canvas;
    camera.aspect = Math.max(width, 1) / Math.max(height, 1);
    camera.position.set(0, .68, Math.max(7.35, 5.25 / camera.aspect));
    camera.lookAt(0, .03, 0);
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  };
  new ResizeObserver(resize).observe(canvas);
  resize();
  document.documentElement.dataset.renderer = "webgl2";
  let oldAt = performance.now();
  let openMouth = 0;
  let blinkRemaining = 0;
  let nextBlink = oldAt + 2200;
  renderer.setAnimationLoop((at) => {
    const dt = Math.min(.05, (at - oldAt) / 1000);
    oldAt = at;
    const ease = Math.min(1, 10 * dt);
    const t = at / 1000;
    const pose = poses[state.phase];
    const face = expressions[state.emotion];
    const speech = state.phase === "speaking";
    const elapsed = Math.max(0, (at - receivedAt) / 1000);
    const rawLevel = demo && speech ? .35 + .30 * Math.sin(t * 17) ** 2 : state.levels[Math.floor(elapsed * 30)] ?? 0;
    openMouth = THREE.MathUtils.lerp(openMouth, speech ? rawLevel : 0, ease);
    const move = (object, axis, value) => {
      object[axis] = THREE.MathUtils.lerp(object[axis], value, ease);
    };
    const motion = reduced.matches ? .2 : 1;
    const age = Math.max(0, (at - changedAt) / 1000);
    const gesture = Math.sin(Math.min(age, .8) / .8 * Math.PI) * motion;
    move(avatar.body.position, "y", Math.sin(t * 1.7) * .025 * motion + (state.emotion === "surprise" ? .10 * gesture : 0));
    move(avatar.body.rotation, "x", pose.body);
    move(avatar.head.rotation, "y", pose.yaw + Math.sin(t * 1.7) * .034 * motion);
    move(avatar.head.rotation, "x", pose.nod + (state.phase === "listening" ? .16 * gesture : .09 * openMouth));
    move(avatar.head.rotation, "z", face.tilt + Math.sin(t * 1.4) * .035 * motion);
    move(avatar.antenna.rotation, "z", .28 + .10 * gesture + Math.sin(t * 3) * .055 * motion);
    move(avatar.arms[0].rotation, "z", -.11 + .28 * openMouth + (state.emotion === "joy" ? .32 * gesture : 0));
    move(avatar.arms[1].rotation, "z", .11 + pose.arm + .25 * gesture);
    move(avatar.arms[1].rotation, "x", pose.arm + .17 * openMouth);
    avatar.light.color.lerp(targetColor.set(face.hue), ease);
    move(avatar.blush, "opacity", state.emotion === "joy" ? .66 : 0);

    if (at >= nextBlink) { blinkRemaining = .17; nextBlink = at + 2600 + Math.random() * 1900; }
    blinkRemaining = Math.max(0, blinkRemaining - dt);
    const blink = blinkRemaining > 0 ? 1 - .93 * Math.sin(blinkRemaining / .17 * Math.PI) : 1;
    for (const [i, eye] of avatar.eyes.entries()) {
      move(eye.scale, "y", .17 * face.eyes * blink);
      move(eye.position, "y", .10 + face.lift * .24 + (state.phase === "thinking" ? .04 : 0));
      move(avatar.brows[i].position, "y", .30 + face.lift);
      move(avatar.brows[i].rotation, "z", Math.PI / 2 + (i === 0 ? -1 : 1) * face.brow);
    }
    const roundMouth = speech || state.emotion === "surprise";
    avatar.mouth.visible = avatar.inner.visible = roundMouth;
    const opening = .047 + openMouth * .19 + (!speech && roundMouth ? .045 : 0);
    move(avatar.mouth.scale, "y", opening);
    move(avatar.inner.scale, "y", opening * .53);
    for (const dot of avatar.grin) {
      dot.visible = !roundMouth;
      const x = dot.position.x / .20;
      move(dot.position, "y", -.294 + face.smile * x * x);
    }
    renderer.render(scene, camera);
  });
}

try {
  start();
  if (!demo) poll();
} catch (error) {
  document.querySelector("#render-error").hidden = false;
  console.error(error);
}
