// Profile files: take an export profile or a compare layout to another
// computer. One profile per file, or several in one file:
//   {"kind": "omr-export-profile", "version": 1, "name": "...", "profile": {...}}
//   {"kind": "omr-compare-profile", "version": 1, "name": "...", "template_id": "...", "settings": {...}}
//   {"kind": "omr-export-profiles", "version": 1, "profiles": [<one of the above>, ...]}
import { download, toast } from "./api.js";

const KINDS = { export: "omr-export-profile", compare: "omr-compare-profile" };
const slug = (name) => String(name || "profile").trim().replace(/[^\w.-]+/g, "_").replace(/^_+|_+$/g, "") || "profile";

// Save one profile (or several) as a file the user picks
export function saveProfileFile(kind, items, fileName) {
  const list = Array.isArray(items) ? items : [items];
  const one = (item) => ({ kind: KINDS[kind], version: 1, ...item });
  const data = list.length === 1 ? one(list[0]) : { kind: `${KINDS[kind]}s`, version: 1, profiles: list.map(one) };
  const name = fileName || `${slug(list.length === 1 ? list[0].name : `${kind} profiles`)}.${kind}-profile.json`;
  return download(name, JSON.stringify(data, null, 1), "application/json");
}

// Ask for profile files and return the profiles in them: [{name, ...item}].
// An export profile file saved by hand (a bare profile) is taken too, named
// after its file.
export function pickProfileFiles(kind) {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".json,application/json";
    input.multiple = true;
    input.addEventListener("change", async () => {
      const found = [];
      for (const file of input.files) {
        try {
          found.push(...readProfiles(kind, JSON.parse(await file.text()), file.name));
        } catch (error) {
          toast(`${file.name}: ${error.message}`, "error", 7000);
        }
      }
      resolve(found);
    });
    input.click();
  });
}

function readProfiles(kind, data, fileName) {
  if (!data || typeof data !== "object") throw new Error("not a profile file");
  const other = kind === "export" ? "compare" : "export";
  if (String(data.kind || "").startsWith(KINDS[other])) {
    throw new Error(kind === "export" ? "this is a compare layout: upload it in New comparison" : "this is an export profile: upload it in Export");
  }
  if (Array.isArray(data.profiles)) return data.profiles.flatMap((item) => readProfiles(kind, item, fileName));
  const fallback = fileName.replace(/(\.(export|compare)-profile)?\.json$/i, "");
  if (data.kind === KINDS.export) return [{ name: data.name || fallback, profile: data.profile || {} }];
  if (data.kind === KINDS.compare) return [{ name: data.name || fallback, template_id: data.template_id || null, settings: data.settings || {} }];
  if (kind === "export" && (data.fields || data.meta)) return [{ name: data.name || fallback, profile: data }];
  throw new Error(kind === "export" ? "not an export profile" : "not a compare layout");
}
