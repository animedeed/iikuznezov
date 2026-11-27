import * as THREE from "/three.module.js";
import { makeHuman } from "/human.js";

const params = new URLSearchParams(location.search);
const demo = params.has("demo");
const showControls = demo && !params.has("camera");
const status = document.querySelector("#status");
const controls = document.querySelector("#controls");
const names = {
  idle: "Ожидает", listening: "Слушает", thinking: "Обдумывает", speaking: "Говорит",
  joy: "радость", surprise: "удивление", concern: "озабоченность",
};
const poses = {
  idle: { body: -.015, nod: .015, lean: 0, yaw: -.055, gazeX: 0, gazeY: 0 },
  listening: { body: .072, nod: .06, lean: -.025, yaw: -.13, gazeX: .006, gazeY: .003 },
  thinking: { body: -.06, nod: -.085, lean: .025, yaw: .10, gazeX: .022, gazeY: .023 },
  speaking: { body: .018, nod: -.008, lean: 0, yaw: 0, gazeX: 0, gazeY: 0 },
};
const expressions = {
  neutral: { eyes: .88, brow: 0, lift: 0, smile: .022, tilt: 0, mouth: 0 },
  joy: { eyes: .81, brow: .016, lift: .035, smile: .128, tilt: -.055, mouth: .17 },
  surprise: { eyes: 1.29, brow: 0, lift: .11, smile: -.012, tilt: .035, mouth: .42 },
  concern: { eyes: .81, brow: -.105, lift: .052, smile: -.075, tilt: .102, mouth: .045 },
};
const characters = new Set(["female", "male"]);
const previewCharacter = params.get("character");
const initialCharacter = demo && characters.has(previewCharacter) ? previewCharacter : "female";
let state = { character: initialCharacter, phase: "idle", emotion: "neutral", levels: [] };
let receivedAt = 0;
let changedAt = 0;
let lastPoll = -Infinity;

function update(next) {
  if (!characters.has(next.character) || !Object.hasOwn(poses, next.phase)
      || !Object.hasOwn(expressions, next.emotion)) return;
  if (state.phase !== next.phase || state.emotion !== next.emotion || state.character !== next.character) {
    changedAt = performance.now();
  }
  document.documentElement.dataset.character = next.character;
  state = next;
  receivedAt = performance.now();
  if (showControls) {
    status.textContent = [names[state.phase], names[state.emotion]].filter(Boolean).join(" · ");
    for (const button of controls.querySelectorAll("[data-phase]")) {
      button.setAttribute("aria-pressed", String(
        button.dataset.phase === state.phase && button.dataset.emotion === state.emotion,
      ));
    }
    for (const button of controls.querySelectorAll("[data-character]")) {
      button.setAttribute("aria-pressed", String(button.dataset.character === state.character));
    }
    controls.querySelector("a").href = `/?demo=1&camera=1&character=${state.character}`;
  }
}

if (showControls) {
  document.body.classList.add("preview");
  controls.hidden = false;
  controls.addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button?.dataset.character) update({ ...state, character: button.dataset.character });
    if (button?.dataset.phase) update({ ...state, phase: button.dataset.phase, emotion: button.dataset.emotion });
  });
}
update(state);

async function poll() {
  try {
    const response = await fetch("/state", { cache: "no-store", signal: AbortSignal.timeout(700) });
    if (response.ok) {
      update(await response.json());
      lastPoll = performance.now();
    }
  } catch {
    if (performance.now() - lastPoll > 1000) update({ ...state, phase: "idle", emotion: "neutral", levels: [] });
  } finally {
    setTimeout(poll, 90);
  }
}

function start() {
  const canvas = document.querySelector("#portrait");
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setClearColor(0, 0);
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.11;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(34, 1, .1, 60);
  scene.add(new THREE.HemisphereLight(0xfff1de, 0x76504e, 1.48));
  function spotlight(color, strength, x, y, z) {
    const lamp = new THREE.DirectionalLight(color, strength);
    lamp.position.set(x, y, z);
    scene.add(lamp);
  }
  spotlight(0xffe4cc, 2.7, -3, 5, 6);
  spotlight(0xbddcf0, 2.15, 3, 2.3, -3);
  spotlight(0xcbd4fa, .74, 4, 1.5, 4);

  const avatars = Object.fromEntries([...characters].map((name) => [name, makeHuman(scene, name)]));
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  const resize = () => {
    const { clientWidth: width, clientHeight: height } = canvas;
    camera.aspect = Math.max(width, 1) / Math.max(height, 1);
    camera.position.set(0, .32, Math.max(6.82, 4.77 / camera.aspect));
    camera.lookAt(0, .005, 0);
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
  let visibleCharacter;
  renderer.setAnimationLoop((at) => {
    const avatar = avatars[state.character];
    if (visibleCharacter !== state.character) {
      for (const [name, character] of Object.entries(avatars)) character.body.visible = name === state.character;
      openMouth = 0;
      visibleCharacter = state.character;
    }
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
    move(avatar.body.position, "y", Math.sin(t * 1.65) * .015 * motion + (state.emotion === "surprise" ? .045 * gesture : 0));
    move(avatar.body.rotation, "x", pose.body);
    move(avatar.body.rotation, "z", pose.lean + Math.sin(t * 1.46) * .012 * motion);
    move(avatar.head.rotation, "y", pose.yaw + Math.sin(t * 1.7) * .018 * motion);
    move(avatar.head.rotation, "x", pose.nod + (state.phase === "listening" ? .12 * gesture : .044 * openMouth));
    move(avatar.head.rotation, "z", face.tilt + Math.sin(t * 1.4) * .025 * motion);

    if (at >= nextBlink) { blinkRemaining = .15; nextBlink = at + 2700 + Math.random() * 2000; }
    blinkRemaining = Math.max(0, blinkRemaining - dt);
    const blink = blinkRemaining > 0 ? 1 - .96 * Math.sin(blinkRemaining / .15 * Math.PI) : 1;
    avatar.express({
      open: face.eyes * blink,
      browLift: face.lift,
      browTilt: face.brow,
      gazeX: pose.gazeX - pose.yaw * .07,
      gazeY: pose.gazeY,
      smile: face.smile,
      mouthOpen: Math.max(face.mouth, openMouth),
    }, ease);
    renderer.render(scene, camera);
  });
}

try {
  if (!demo) await poll();
  start();
} catch (error) {
  document.querySelector("#render-error").hidden = false;
  console.error(error);
}
