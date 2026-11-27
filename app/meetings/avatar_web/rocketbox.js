import * as THREE from "three";
import { GLTFLoader } from "/three/addons/loaders/GLTFLoader.js";

const models = {
  female: "/models/sophia_formal.glb",
  male: "/models/daniel_formal.glb",
};
const motions = { idle: "Idle", listening: "Idle", thinking: "Think", speaking: "Talk" };
const expressions = {
  neutral: {},
  joy: {
    mouthSmileLeft: .54, mouthSmileRight: .54, cheekSquintLeft: .24, cheekSquintRight: .24,
    eyeSquintLeft: .12, eyeSquintRight: .12,
  },
  surprise: {
    browInnerUp: .68, browOuterUpLeft: .63, browOuterUpRight: .63, eyeWideLeft: .58, eyeWideRight: .58,
    jawOpen: .22, mouthFunnel: .17,
  },
  concern: {
    browInnerUp: .42, browDownLeft: .28, browDownRight: .28, mouthFrownLeft: .31, mouthFrownRight: .31,
  },
};
const hiddenClothing = new Set(["Skin_shirt", "Skin_tee", "Top_shirt", "Top_tee", "Trousers", "Shoes"]);
const { clamp, lerp } = THREE.MathUtils;
const loader = new GLTFLoader();

export async function loadAvatar(character) {
  const { scene, animations } = await loader.loadAsync(models[character]);
  const head = scene.getObjectByName("mixamorigHead");
  const leftEye = scene.getObjectByName("mixamorigLeftEye");
  const rightEye = scene.getObjectByName("mixamorigRightEye");
  const faceMeshes = [];
  scene.traverse((node) => {
    if (hiddenClothing.has(node.name)) node.visible = false;
    if (node.morphTargetDictionary && node.morphTargetInfluences) faceMeshes.push(node);
  });

  if (!head || !leftEye || !rightEye || !faceMeshes.some((node) => "jawOpen" in node.morphTargetDictionary)) {
    throw new Error("Rocketbox model is missing the face rig");
  }

  const mixer = new THREE.AnimationMixer(scene);
  const actions = new Map();
  for (const name of new Set(Object.values(motions))) {
    const clip = THREE.AnimationClip.findByName(animations, name);
    if (!clip) throw new Error(`Rocketbox model is missing the ${name} clip`);
    actions.set(name, mixer.clipAction(clip));
  }
  let active = actions.get("Idle");
  active.play();
  mixer.update(0);

  const eyePoint = leftEye.getWorldPosition(new THREE.Vector3());
  eyePoint.lerp(rightEye.getWorldPosition(new THREE.Vector3()), .5);
  const focus = eyePoint.add(new THREE.Vector3(0, -.082, -.035));
  const pose = new THREE.Euler(0, 0, 0, "YXZ");
  const offset = new THREE.Quaternion();
  const parent = new THREE.Quaternion();
  const localOffset = new THREE.Quaternion();
  const desired = new THREE.Vector3();
  const expressionWeights = {};
  const driven = new Set([
    ...Object.values(expressions).flatMap(Object.keys),
    "jawOpen", "mouthFunnel", "eyeBlinkLeft", "eyeBlinkRight",
  ]);
  for (const key of driven) expressionWeights[key] = 0;

  function animate({ phase, emotion, mouth, blink, nod, yaw, tilt, motion }, dt) {
    const action = actions.get(motion > .5 ? motions[phase] : "Idle");
    if (action !== active) {
      action.reset().play().crossFadeFrom(active, .32, false);
      active = action;
    }
    mixer.update(dt);

    const ease = Math.min(1, dt * 12);
    desired.set(lerp(desired.x, nod, ease), lerp(desired.y, yaw, ease), lerp(desired.z, tilt, ease));
    pose.set(desired.x, desired.y, desired.z);
    offset.setFromEuler(pose);
    head.parent.getWorldQuaternion(parent);
    // The imported Biped head uses local axes different from the camera axes.
    head.quaternion.premultiply(localOffset.copy(parent).invert().multiply(offset).multiply(parent));

    const face = expressions[emotion];
    for (const key of driven) {
      let value = face[key] ?? 0;
      if (key === "jawOpen") value = Math.max(value, .66 * mouth);
      if (key === "mouthFunnel") value = Math.max(value, .13 * mouth);
      if (key.startsWith("eyeBlink")) value = blink;
      expressionWeights[key] = lerp(expressionWeights[key], clamp(value, 0, 1), ease);
    }
    for (const node of faceMeshes) {
      for (const [key, value] of Object.entries(expressionWeights)) {
        const index = node.morphTargetDictionary[key];
        if (index !== undefined) node.morphTargetInfluences[index] = value;
      }
    }
  }

  return { scene, focus, animate };
}
