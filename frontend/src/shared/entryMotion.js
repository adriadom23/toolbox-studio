// Shared entry-motion presets. Each surface receives CSS custom properties
// instead of a fixed animation, so navigation feels varied without changing
// component identity or remounting stateful views.
const PRESETS = Object.freeze({
  tool: Object.freeze([
    { x: "-30px", y: "0px", scale: "0.988", duration: "350ms" },
    { x: "30px", y: "0px", scale: "0.988", duration: "350ms" },
    { x: "0px", y: "24px", scale: "0.982", duration: "330ms" },
    { x: "0px", y: "-20px", scale: "0.986", duration: "370ms" },
    { x: "-22px", y: "18px", scale: "0.984", duration: "360ms" },
    { x: "22px", y: "18px", scale: "0.984", duration: "360ms" },
  ]),
  shift: Object.freeze([
    { x: "-38px", y: "0px", scale: "0.992", duration: "390ms" },
    { x: "38px", y: "0px", scale: "0.992", duration: "390ms" },
    { x: "0px", y: "28px", scale: "0.988", duration: "370ms" },
    { x: "0px", y: "-24px", scale: "0.99", duration: "410ms" },
    { x: "-28px", y: "18px", scale: "0.989", duration: "400ms" },
    { x: "28px", y: "18px", scale: "0.989", duration: "400ms" },
  ]),
  popup: Object.freeze([
    { x: "-24px", y: "0px", scale: "0.955", duration: "320ms", backdrop: "230ms" },
    { x: "24px", y: "0px", scale: "0.955", duration: "320ms", backdrop: "230ms" },
    { x: "0px", y: "20px", scale: "0.95", duration: "300ms", backdrop: "210ms" },
    { x: "0px", y: "-18px", scale: "0.96", duration: "340ms", backdrop: "240ms" },
    { x: "-18px", y: "16px", scale: "0.952", duration: "330ms", backdrop: "220ms" },
    { x: "18px", y: "16px", scale: "0.952", duration: "330ms", backdrop: "220ms" },
  ]),
});

const lastPresetByGroup = new Map();

export function randomEntryMotion(group = "tool") {
  const presets = PRESETS[group] || PRESETS.tool;
  const previous = lastPresetByGroup.get(group);
  const available = presets.map((_, index) => index).filter((index) => index !== previous);
  const index = available[Math.floor(Math.random() * available.length)] ?? 0;
  const preset = presets[index];
  lastPresetByGroup.set(group, index);

  return {
    "--tb-entry-x": preset.x,
    "--tb-entry-y": preset.y,
    "--tb-entry-scale": preset.scale,
    "--tb-entry-duration": preset.duration,
    ...(preset.backdrop
      ? { "--tb-entry-backdrop-duration": preset.backdrop }
      : {}),
  };
}
