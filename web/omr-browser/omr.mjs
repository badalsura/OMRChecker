// ES module entry point: `import OMR, { loadTemplate } from "./omr.mjs"`.
// omr.js is a plain script that registers globalThis.OMR (and module.exports in Node).
import "./omr.js";

const OMR = globalThis.OMR;
export default OMR;
export const {
  VERSION,
  CONFIG_DEFAULTS,
  DEFAULT_LIBS,
  loadTemplate,
  checkTemplate,
  createWorkerEngine,
  decodeImage,
  enableBarcodes,
  barcodesAvailable,
  loadBubbleModel,
  registerZoneReader,
  renderResult,
} = OMR;
