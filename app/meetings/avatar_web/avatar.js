import * as THREE from "three";
import { loadAvatar } from "/rocketbox.js";

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
  idle: { nod: .01, yaw: -.015, tilt: 0 },
  listening: { nod: .06, yaw: -.07, tilt: -.02 },
  thinking: { nod: -.035, yaw: .06, tilt: .024 },
  speaking: { nod: -.008, yaw: 0, tilt: 0 },
};
const emotionTilt = { neutral: 0, joy: -.025, surprise: .03, concern: .055 };
const characters = new Set(["female", "male"]);
const previewCharacter = params.get("character");
const initialCharacter = demo && characters.has(previewCharacter) ? previewCharacter : "female";
let state = { character: initialCharacter, phase: "idle", emotion: "neutral", levels: [] };
let receivedAt = 0;
let changedAt = 0;
let lastPoll = -Infinity;

function update(next) {
  if (!characters.has(next.character) || !Object.hasOwn(poses, next.phase)
      || !Object.hasOwn(emotionTilt, next.emotion)) return;
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
  const loading = document.querySelector("#loading");
  const error = document.querySelector("#render-error");
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setClearColor(0, 0);
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.NeutralToneMapping;
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(32, 1, .05, 12);
  scene.add(new THREE.HemisphereLight(0xfcf4e9, 0xa0aac2, 1.62));
  function light(color, intensity, x, y, z) {
    const lamp = new THREE.DirectionalLight(color, intensity);
    lamp.position.set(x, y, z);
    scene.add(lamp);
  }
  light(0xffecd9, 1.75, -2, 3.5, 3.6);
  light(0xd8e4fb, .59, 2.4, 2.0, 2.7);
  light(0xccdcff, 1.22, 2.5, 2.7, -2.8);

  const models = new Map();
  const reduced = matchMedia("(prefers-reduced-motion: reduce)");
  let selected;
  let loadGeneration = 0;
  let avatar;
  const resize = () => {
    const { clientWidth: width, clientHeight: height } = canvas;
    camera.aspect = Math.max(width, 1) / Math.max(height, 1);
    if (avatar) {
      const { x, y, z } = avatar.focus;
      camera.position.set(x, y + .028, z + Math.max(.83, .80 / camera.aspect));
      camera.lookAt(x, y, z);
    }
    camera.updateProjectionMatrix();
    renderer.setSize(width, height, false);
  };
  new ResizeObserver(resize).observe(canvas);
  resize();

  async function show(character) {
    selected = character;
    const generation = ++loadGeneration;
    if (avatar) scene.remove(avatar.scene);
    avatar = undefined;
    delete document.documentElement.dataset.activeCharacter;
    loading.hidden = false;
    error.hidden = true;
    if (!models.has(character)) models.set(character, loadAvatar(character));
    try {
      const next = await models.get(character);
      if (generation !== loadGeneration) return;
      avatar = next;
      scene.add(avatar.scene);
      resize();
      document.documentElement.dataset.renderer = "webgl2";
      document.documentElement.dataset.activeCharacter = character;
    } catch (reason) {
      models.delete(character);
      if (generation !== loadGeneration) return;
      error.textContent = "Не удалось загрузить 3D-модель. Обновите страницу и проверьте консоль клиента.";
      error.hidden = false;
      console.error(reason);
    } finally {
      if (generation === loadGeneration) loading.hidden = true;
    }
  }

  let oldAt = performance.now();
  let openMouth = 0;
  let nextBlink = oldAt + 2000;
  let blinkStarted = -Infinity;
  renderer.setAnimationLoop((at) => {
    if (selected !== state.character) {
      openMouth = 0;
      void show(state.character);
    }
    const dt = Math.min(.05, (at - oldAt) / 1000);
    oldAt = at;
    if (avatar) {
      const pose = poses[state.phase];
      const speech = state.phase === "speaking";
      const elapsed = Math.max(0, (at - receivedAt) / 1000);
      const raw = demo && speech ? .32 + .34 * Math.sin(at / 60) ** 2 : state.levels[Math.floor(elapsed * 30)] ?? 0;
      openMouth = THREE.MathUtils.lerp(openMouth, speech ? raw : 0, Math.min(1, 12 * dt));
      if (at >= nextBlink) {
        blinkStarted = at;
        nextBlink = at + 2700 + Math.random() * 2000;
      }
      const blinkTime = Math.min(1, Math.max(0, (at - blinkStarted) / 155));
      const motion = reduced.matches ? .2 : 1;
      const age = Math.min(1, Math.max(0, (at - changedAt) / 650));
      const nod = pose.nod + (state.phase === "listening" ? .12 * Math.sin(age * Math.PI) : .04 * openMouth);
      avatar.animate({
        phase: state.phase, emotion: state.emotion, mouth: openMouth, blink: Math.sin(blinkTime * Math.PI),
        nod: motion * nod, yaw: motion * pose.yaw, tilt: motion * (pose.tilt + emotionTilt[state.emotion]), motion,
      }, dt);
    }
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
