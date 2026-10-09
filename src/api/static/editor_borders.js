// Template editor: printed borders. "Pick printed border" shows the
// rectangles printed on the reference sheet; clicking one makes it the
// selected block's border, with the gap to the bubbles (borderPadding)
// measured instead of typed. A box around that one becomes the outer frame
// (outerBorderPadding). At scan time a border may move the bubbles at most
// 0.4 of a bubble pitch (src/rectify.py), so it can never slide a block.
import { api, toast } from "./api.js";

export async function startBorderPick(ed) {
  if (!ed.selected || ed.selected.kind !== "block") return toast("Select a block first");
  try {
    if (!ed.printedBoxes) ed.printedBoxes = (await api(`/templates/${ed.id}/generator/printed-boxes`)).boxes;
  } catch (error) {
    return toast(error.message, "error", 5000);
  }
  if (!ed.printedBoxes.length) return toast("No printed rectangles found on the reference sheet", "error", 5000);
  ed.borderBlock = ed.selected.name;
  if (ed.mode !== "pick-border") ed.setMode("pick-border");
  toast("Click the printed box that belongs to this block");
  ed.draw();
}

const contains = (outer, inner, tol = 2) =>
  outer[0] <= inner[0] + tol && outer[1] <= inner[1] + tol && outer[0] + outer[2] >= inner[0] + inner[2] - tol && outer[1] + outer[3] >= inner[1] + inner[3] - tol;

const area = (b) => b[2] * b[3];

// Gap from the bubbles' bounding box to a box, as [x, y]
function gap(o, box) {
  const gx = (o.x - box[0] + (box[0] + box[2]) - (o.x + o.w)) / 2;
  const gy = (o.y - box[1] + (box[1] + box[3]) - (o.y + o.h)) / 2;
  return [Math.max(0, Math.round(gx * 10) / 10), Math.max(0, Math.round(gy * 10) / 10)];
}

export function pickBorder(ed, point) {
  const name = ed.borderBlock;
  ed.setMode(null);
  if (!name || !ed.doc.fieldBlocks[name]) return;
  const o = ed.blockInfo(name);
  const bubbles = [o.x, o.y, o.w, o.h];
  const hits = (ed.printedBoxes || []).filter((b) => contains(b, [point[0], point[1], 0, 0], 0)).sort((a, b) => area(a) - area(b));
  const inner = hits.find((b) => contains(b, bubbles));
  if (!inner) return toast("That box does not enclose the block's bubbles; click a box around them", "error", 5000);
  const outer = (ed.printedBoxes || []).filter((b) => b !== inner && contains(b, inner) && area(b) > area(inner) * 1.05).sort((a, b) => area(a) - area(b))[0];
  ed.edit(() => {
    const raw = ed.doc.fieldBlocks[name];
    raw.rectifyOnBorder = true;
    raw.borderPadding = gap(o, inner);
    if (outer) raw.outerBorderPadding = gap(o, outer);
    else delete raw.outerBorderPadding;
  });
  const text = `Border set: gap ${gap(o, inner).join(" × ")} px${outer ? `, outer frame gap ${gap(o, outer).join(" × ")} px` : ""}`;
  toast(text, "ok", 5000);
  ed.draw();
}

export function drawBorders(ed) {
  if (ed.mode !== "pick-border" || !ed.printedBoxes) return;
  const { scale, ox, oy } = ed.view;
  const ctx = ed.ctx;
  ctx.save();
  ctx.strokeStyle = "#e08a00";
  ctx.lineWidth = 2;
  ctx.setLineDash([6, 4]);
  for (const [x, y, w, h] of ed.printedBoxes) ctx.strokeRect(ox + x * scale, oy + y * scale, w * scale, h * scale);
  ctx.restore();
}
