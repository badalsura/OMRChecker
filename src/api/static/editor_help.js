// Template editor help: every option gets an info icon and a short help line
// that shows under the control while it is focused (or after clicking the icon).
import { el } from "./api.js";

// Keys are "<panel>:<label>" (panel = block, zone, page, check, validation…)
// or a plain label / topic key used everywhere.
export const HELP = {
  // block
  "block:Name": "Name of the block in the template. Only used inside the editor and in messages; the output columns are the field labels.",
  "block:Field type": "Preset bubble values and layout, e.g. QTYPE_INT for digits 0-9 in a column. \"Custom values\" lets you type your own.",
  "block:Bubble values (comma separated)": "What each bubble means, in order, e.g. A,B,C,D or 0,1,2,…,9.",
  "block:Direction (how values are laid out)": "Horizontal: a field is a row of bubbles (MCQ). Vertical: a field is a column of bubbles (roll number digits).",
  "block:Field labels (e.g. q1..20 or roll1..6, comma separated)": "Names of the fields in this block, one per row or column. Renaming them here also renames them in grouped fields, validation rules and checks.",
  "block:Origin x": "Left edge of the first bubble, in template pixels.",
  "block:Origin y": "Top edge of the first bubble, in template pixels.",
  "block:Bubbles gap": "Distance between the starts of two neighbouring bubbles of one field.",
  "block:Labels gap": "Distance between the starts of two neighbouring fields (rows or columns).",
  "block:Bubble width": "Bubble size for this block only. Empty: use the page's bubble size.",
  "block:Bubble height": "Bubble size for this block only. Empty: use the page's bubble size.",
  "block:Empty value": "Value written when no bubble of a field is marked. Empty: use the page's empty value.",
  "block:Fit to printed border (rectifyOnBorder)": "After page alignment, find the box printed around this block and fit the bubbles onto it. Use for blocks with a printed border that sit slightly off.",
  "block:Border gap (px from bubbles to the box)": "Distance from the outer bubbles to the printed box. Empty: measured on each sheet.",
  // zone
  "zone:Name": "Output column name of this zone. Renaming it also renames it in validation rules, checks and fallbacks.",
  "zone:Type": "barcode / qrcode: decode a symbol. ocr: printed text (Tesseract). icr: handwritten characters in boxes (needs a model).",
  "zone:x": "Left edge of the zone, in template pixels.",
  "zone:y": "Top edge of the zone, in template pixels.",
  "zone:Width": "Zone width in template pixels. Leave some margin around the text or barcode.",
  "zone:Height": "Zone height in template pixels.",
  "zone:Accepted formats (none ticked = all)": "Only accept these barcode symbologies. Ticking the right one avoids misreads of other printed codes.",
  "zone:Allowed characters": "Characters the OCR may return, e.g. 0123456789 for a serial number. Empty: any character.",
  "zone:Character boxes": "Number of equal-width boxes, one handwritten character each.",
  "zone:Language": "Tesseract language model used for this zone. Only languages installed on this server are listed.",
  "zone:Layout (page segmentation mode)": "What layout Tesseract expects inside the zone. One line suits a single printed field; use \"Find text anywhere\" when the position of the text varies.",
  "zone:Pattern the value must match": "Values that don't match are flagged for review. Pick a preset; \"Custom\" takes a regular expression.",
  "zone:Min confidence": "Reads below this confidence (0–1) go to review. Default 0.6.",
  "zone:Empty value": "Value written when the zone reads nothing.",
  "zone:Colour removal for this zone": "Read this zone from a differently processed image, e.g. plain grey for a barcode on a red-dropout sheet.",
  "zone:If the barcode fails, read this zone": "When the barcode gives nothing, use this OCR/ICR zone (e.g. the printed digits under the barcode). Creates a check named after the barcode.",
  "zone:Clean-up before comparing": "How the barcode and fallback values are cleaned before they are compared.",
  "zone:Send to review when the fallback is used": "Sheets whose value came from the fallback zone also go to the review queue.",
  "zone:Read only when needed": "Skip this zone unless a check needs it (e.g. as a barcode fallback). Saves time on every sheet.",
  "zone:Decoders, in order": "Barcode decoders tried in order until one reads. Untick to skip a decoder.",
  "zone:Use pyzbar (ZBar) when installed": "Adds the ZBar decoder. Off by default; only used when it is installed.",
  "zone:Code 39 check digit": "Require and strip the Code 39 mod-43 check digit.",
  "zone:Code 39 full ASCII": "Decode Code 39 full-ASCII pairs (e.g. +A as a). Auto decides per read.",
  "zone:ITF check digit": "Require the GS1 check digit on Interleaved 2 of 5 codes.",
  "zone:ITF minimum length": "Shortest ITF code accepted; short partial reads are rejected.",
  // page
  "page:Page width": "Width the sheet is resized to before reading, in template pixels.",
  "page:Page height": "Height the sheet is resized to before reading, in template pixels.",
  "page:Bubble width": "Default bubble size for every block.",
  "page:Bubble height": "Default bubble size for every block.",
  "page:Empty value": "Value written for a field with no bubble marked (default: nothing).",
  "page:Bubble threshold": "Adaptive finds the marked/unmarked split on each sheet and row. Fixed uses one intensity line for every sheet.",
  "page:Dark below (0–255)": "Pixels darker than this count as pencil.",
  "page:Min filled share (0–1)": "A bubble is marked when at least this share of it is dark.",
  // grouping
  outputAsOne: "Join all columns of this block into one output column, e.g. rollno = roll1..roll10. Each column gives exactly one character.",
  groupName: "Name of the joined output column, e.g. rollno.",
  groupEmpty: "What an unmarked column becomes. Keep a placeholder so later digits don't shift, or skip it and join the next column.",
  groupMulti: "Character put in place of a column with two or more marked bubbles.",
  groupIssue: "Character put in place of a column with low confidence, a doubtful mark or a failed check. The value is still exported, and the field is flagged for review.",
  // validation
  "validation:Required": "The value must not be blank.",
  "validation:Length": "Exact length, or a minimum and maximum, in characters.",
  "validation:Allow gaps": "Blank columns inside the value are allowed (e.g. 12 45). Untick to flag them.",
  "validation:Allow empty ends": "Blank columns at the start or end are allowed (e.g. a 4-digit roll number in 6 columns).",
  "validation:Leading zeros": "Keep: 0123 is fine. Forbid: a value starting with 0 fails.",
  "validation:Pattern": "The whole value must match. Presets cover most cases; Custom takes a regular expression.",
  "validation:Allowed values": "The value must be one of these. Leave empty to allow anything.",
  "validation:Range": "Numeric minimum and maximum. The value stays text; leading zeros are kept.",
  "validation:On fail": "What happens to a value that fails: send it to review, blank it, both, or only mark it in the results.",
  // checks
  "check:Check name": "Name of this cross-field check, shown in the review queue and results.",
  "check:Fields to compare": "Every field, group and zone that reads the same value, e.g. a barcode and the printed serial number.",
  "check:Which field wins when they differ": "Drag to order. The first field that has a value is used.",
  "check:Clean-up before comparing": "Applied to every value before they are compared.",
  "check:When values disagree": "Use the preferred field, send the sheet to review, or blank the value and send it to review.",
  "check:When the preferred field is missing": "Use the next field that has a value, or also send the sheet to review.",
  "check:Also send to review when the fallback is used": "Review sheets whose value did not come from the first field.",
  "check:Ignore fields that failed their own validation": "A field whose own validation failed is treated as missing.",
  "check:Ignore fields already flagged for review": "A field that is already in the review queue is treated as missing.",
  "check:Output column name": "Column that gets the check's value. Empty: the check's name. May be one of the compared fields, which is then replaced.",
  // review thresholds
  "review:Min confidence": "Fields whose weakest bubble is less confident than this go to review. Higher = more reviews.",
  "review:Weak mark below": "A marked bubble with less of its inside filled than this is a weak mark.",
  "review:Possible missed mark above": "An unmarked bubble with more of its inside filled than this may be a missed mark.",
  "review:Confidence margin": "Intensity distance from the threshold that counts as fully confident. Larger = stricter.",
  "review:Min marked bubbles per sheet": "Flag the whole sheet when fewer bubbles are marked (catches pens removed by colour dropout). 0 = off.",
  "review:Flags that send a field to review": "Which problems put a field in the review queue. Unticked flags are still shown in the results.",
  // alignment
  "align:Alignment method": "How each sheet is lined up with the template before reading. Timing tracks or corner markers are the most precise.",
  "align:Search radius": "How far (template px) a mark may sit from where the template expects it.",
  "align:Minimum matched marks": "Fewer matched timing marks than this rejects the sheet.",
  "align:Max leftover error": "Mean distance (template px) between the fitted and found marks above which the sheet is rejected.",
  "align:Bend to fit the marks (nonRigid)": "Add a curve correction on top of the perspective fit, for curled paper.",
  "align:Detect sheets fed upside down": "Try 90/180/270° turns when the tracks don't match.",
  "align:Marker image": "Image of the corner marker, in the template folder.",
  "align:Marker width share": "Sheet width divided by marker width.",
  "align:Min match score": "Lowest template-matching score (0–1) accepted for a marker.",
  "align:Reference image": "Image of a blank sheet in the template folder; scans are aligned to it.",
  "align:Max features": "Feature points compared between the scan and the reference.",
  "align:Good matches share": "Share of the best feature matches kept (0–1).",
  "align:Only shift and rotate (2d)": "Fit a rotation and shift only, no perspective.",
  "align:Motion model": "How much the ECC alignment may change the sheet: translation only up to a full perspective.",
  "align:Edge kernel": "Size of the smoothing used to find the page edge.",
  // models, pdf, language
  "ml:Bubble model": "Learned model that double-checks every bubble. Classical thresholding only when none.",
  "ml:Handwriting model": "Model that reads handwritten characters in ICR zones. Without one, ICR zones go to review.",
  "pdf:PDF render DPI": "Resolution PDF pages are rendered at. Auto uses the PDF's own image resolution; 200–300 suits most scans.",
  "pdf:PDF pages": "Which pages of each PDF to read: 1, 2-4, 3- (to the end) or all.",
  "barcode:Decoders, in order": "Barcode decoders tried in order for every barcode zone.",
  "barcode:Use pyzbar (ZBar) when installed": "Adds the ZBar decoder for every zone. Only used when it is installed.",
  "barcode:Review reads by fallback decoders": "Send barcodes read by a decoder other than the first (zxing) to review.",
  "output:Custom column order": "Choose and order the CSV columns. Off: every column, sorted by name.",
};

export function helpFor(key, context) {
  if (!key) return null;
  return HELP[`${context}:${key}`] || HELP[key] || null;
}

// Add an info icon and a focus help line to a ".field" label.
// `text` is a HELP key, a context-free key, or the help text itself.
export function help(label, text, context) {
  const body = helpFor(text, context) || (text && text.length > 40 ? text : null);
  if (!body || !label) return label;
  const icon = el("button", { type: "button", class: "info-icon", title: body, "aria-label": "Help", tabindex: "-1" }, "i");
  icon.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    label.classList.toggle("show-help");
  });
  // Put the icon right after the caption text
  const caption = [...label.childNodes].find((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim());
  if (caption) {
    const cap = el("span", { class: "cap" }, caption.textContent, icon);
    label.replaceChild(cap, caption);
  } else {
    label.append(icon);
  }
  label.classList.add("has-help");
  label.append(el("div", { class: "help-line" }, body));
  return label;
}
