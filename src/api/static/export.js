// Export dialog: pick a format, build an export profile (columns, names, order,
// types), preview it and run the export on the server (CSV, XLSX, PDF, SQL).
import { api, chip, el, emit, fmtDuration, fmtTime, modal, on, safeStorage, toast, url } from "./api.js";
import { openBrowser } from "./folders.js";

const TYPES = ["text", "int", "decimal", "date", "bool"];
const META = ["file_name", "file_id", "page", "scan_id", "job_id", "template_id", "status", "score", "source_path", "verified_by", "error", "created_at"];
const DEFAULT_META = ["file_name", "page", "scan_id", "status", "score"];

export function initExport() {
  on("export-open", (filters) => openExport(filters || {}));
  on("exports-changed", refreshExports);
  document.getElementById("exports-refresh").addEventListener("click", refreshExports);
}

// ---- Jobs tab: every export, running or done ------------------------------
const ACTIVE = ["queued", "running", "paused"];
let exportsTimer = null;

export async function refreshExports() {
  clearTimeout(exportsTimer);
  const body = document.querySelector("#exports-table tbody");
  if (!body) return;
  let data;
  try {
    data = await api("/exports?limit=50");
  } catch (error) {
    return;
  }
  body.innerHTML = "";
  let active = false;
  for (const record of data.exports) {
    const running = ACTIVE.includes(record.state);
    active = active || running;
    const pct = record.total ? Math.min(100, Math.round((100 * (record.rows || 0)) / record.total)) : 0;
    const end = record.finished_at || (running ? Date.now() / 1000 : null);
    const start = record.started_at || record.created_at;
    const actions = [];
    if (record.state === "completed") {
      actions.push(el("a", { class: "button small primary", href: url(record.download_url), download: record.file_name }, "Download"));
    }
    if (record.state === "paused") {
      actions.push(el("button", { class: "small primary", title: "Carry on from where it stopped", onclick: () => exportAction(record, "resume") }, "Resume"));
    } else if (running) {
      actions.push(el("button", { class: "small", title: "Stop for now and carry on later with Resume (while the server keeps running)", onclick: () => exportAction(record, "pause") }, "Pause"));
    }
    if (["interrupted", "cancelled", "failed"].includes(record.state) && record.request) {
      actions.push(el("button", { class: "small primary", title: "Start the same export again: same sheets, format, columns and folder", onclick: () => exportAction(record, "rerun") }, "Run again"));
    }
    if (running) {
      actions.push(el("button", { class: "small danger", onclick: () => cancelExport(record) }, "Cancel"));
    } else {
      actions.push(el("button", { class: "small danger ghost", title: record.path ? "Remove from this list; the file in your folder stays" : "Delete this export and its file", onclick: () => deleteExport(record) }, "Delete"));
    }
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("div", {}, record.file_name), record.path ? el("div", { class: "mono small muted" }, record.path) : null, record.error && record.state !== "cancelled" ? el("div", { class: "small fs-warn" }, record.error) : null),
        el("td", { class: "small" }, `${record.format.toUpperCase()} · ${scopeText(record.filters || {})}`),
        el("td", {}, chip(record.state, record.state === "completed" ? "ok" : record.state === "failed" || record.state === "interrupted" ? "error" : record.state === "paused" ? "paused" : "")),
        el("td", {}, running ? el("div", { class: "progress" + (record.state === "paused" ? " paused" : "") }, el("div", { style: { width: `${pct}%` } })) : null, el("span", { class: "small muted" }, `${record.rows || 0} / ${record.total} rows`)),
        el("td", { class: "muted small" }, fmtTime(record.created_at)),
        el("td", { class: "muted small" }, end && start ? fmtDuration(end - start) : ""),
        el("td", { class: "muted small" }, record.user || ""),
        el("td", { class: "actions" }, ...actions)
      )
    );
  }
  if (!data.exports.length) body.append(el("tr", {}, el("td", { colspan: 8, class: "muted" }, "No exports yet: use Export… in the Results tab")));
  if (active && document.getElementById("tab-jobs").classList.contains("active")) {
    exportsTimer = setTimeout(refreshExports, 1500);
  }
}

async function cancelExport(record) {
  if (!confirm(`Stop the export ${record.file_name}? The part written so far is removed.`)) return;
  try {
    await api(`/exports/${record.id}/cancel`, { method: "POST" });
    toast("Export cancelled");
  } catch (error) {
    toast(error.message, "error");
  }
  refreshExports();
}

async function exportAction(record, action) {
  try {
    await api(`/exports/${record.id}/${action}`, { method: "POST" });
    if (action === "rerun") toast("Export started again", "ok");
  } catch (error) {
    toast(error.message, "error");
  }
  refreshExports();
}

async function deleteExport(record) {
  try {
    await api(`/exports/${record.id}`, { method: "DELETE" });
  } catch (error) {
    toast(error.message, "error");
  }
  refreshExports();
}

function scopeText(filters) {
  const parts = [];
  if (filters.job_id) parts.push(`job ${filters.job_id.slice(0, 8)}`);
  if (filters.template_id) parts.push(`template ${filters.template_id}`);
  if (filters.view && filters.view !== "all") parts.push(filters.view.replace("_", " "));
  if (filters.name) parts.push(`field ${filters.name}`);
  if (filters.flag) parts.push(`flag ${filters.flag}`);
  if (filters.file) parts.push(`file contains "${filters.file}"`);
  return parts.length ? parts.join(" · ") : "all results";
}

async function openExport(filters) {
  const s = {
    format: safeStorage("get", "omr_export_format") || "xlsx",
    fields: [], // {field, header, type, format, outputFormat, include}
    meta: DEFAULT_META.slice(),
    opts: { includeConfidence: false, includeFlags: false, includeReviewStatus: true, includeCorrected: true, includeFieldCorrected: false, allowLossyCast: false },
    csv: { leadingZeros: "formula", delimiter: "," },
    xlsx: { highlight: true },
    pdf: { mode: "table" },
    sql: { table: "omr_results", url: "" },
    total: null,
  };

  const formatSelect = el("select", {}, [
    el("option", { value: "xlsx" }, "Excel (.xlsx)"),
    el("option", { value: "csv" }, "CSV"),
    el("option", { value: "pdf" }, "PDF"),
    el("option", { value: "sqlite" }, "SQLite file"),
    el("option", { value: "sql" }, "Database (SQLAlchemy URL)"),
  ]);
  formatSelect.value = s.format;
  const formatOptions = el("div", { class: "row gap wrap exp-format-opts" });
  const fieldTable = el("tbody");
  const metaBox = el("div", { class: "exp-meta" });
  const optionBox = el("div", { class: "exp-options" });
  const previewBox = el("div", { class: "exp-preview" });
  const statusBox = el("div", { class: "exp-status" });
  const profileSelect = el("select", {}, el("option", { value: "" }, "— saved profiles —"));
  const jsonBox = el("textarea", { rows: 8, spellcheck: "false" });

  const profile = () => ({
    fields: s.fields.map((f) => {
      const spec = { field: f.field };
      if (f.header && f.header !== f.field) spec.header = f.header;
      if (f.type !== "text") spec.type = f.type;
      if (f.type === "date") {
        spec.format = f.format || "%d%m%Y";
        if (f.outputFormat) spec.outputFormat = f.outputFormat;
      }
      if (!f.include) spec.include = false;
      return spec;
    }),
    includeOtherFields: false,
    meta: s.meta.slice(),
    ...s.opts,
    csv: { ...s.csv, bom: true },
    xlsx: { ...s.xlsx },
    pdf: { ...s.pdf },
    sql: { table: s.sql.table },
  });

  const loadProfile = (data) => {
    const byName = Object.fromEntries((data.fields || []).map((f) => [f.field, f]));
    const listed = (data.fields || []).map((f) => f.field);
    const known = s.fields.map((f) => f.field);
    const order = [...listed, ...known.filter((n) => !listed.includes(n))];
    s.fields = order.map((name) => {
      const spec = byName[name] || {};
      return { field: name, header: spec.header || spec.rename || name, type: spec.type || "text", format: spec.format || "", outputFormat: spec.outputFormat || "", include: byName[name] ? spec.include !== false : data.includeOtherFields !== false && !listed.length };
    });
    if (data.meta) s.meta = data.meta.slice();
    for (const key of Object.keys(s.opts)) if (key in data) s.opts[key] = !!data[key];
    Object.assign(s.csv, data.csv || {});
    Object.assign(s.xlsx, data.xlsx || {});
    Object.assign(s.pdf, data.pdf || {});
    if (data.sql && data.sql.table) s.sql.table = data.sql.table;
    renderAll();
  };

  const renderFormat = () => {
    formatOptions.innerHTML = "";
    const f = formatSelect.value;
    if (f === "csv") {
      const lz = el("select", {}, [el("option", { value: "formula" }, 'Excel-safe ="0123"'), el("option", { value: "apostrophe" }, "Text marker '0123"), el("option", { value: "none" }, "Plain 0123")]);
      lz.value = s.csv.leadingZeros;
      lz.addEventListener("change", () => (s.csv.leadingZeros = lz.value));
      const delim = el("select", {}, [el("option", { value: "," }, "comma"), el("option", { value: ";" }, "semicolon"), el("option", { value: "\t" }, "tab")]);
      delim.value = s.csv.delimiter;
      delim.addEventListener("change", () => (s.csv.delimiter = delim.value));
      formatOptions.append(el("label", { class: "field inline" }, "Leading zeros ", lz), el("label", { class: "field inline" }, "Delimiter ", delim));
    } else if (f === "xlsx") {
      const hl = el("input", { type: "checkbox", checked: s.xlsx.highlight !== false });
      hl.addEventListener("change", () => (s.xlsx.highlight = hl.checked));
      formatOptions.append(el("label", { class: "small" }, hl, " colour flagged (amber) and corrected (green) cells"), el("span", { class: "muted small" }, "Text fields are text cells, so leading zeros survive. Over 1,048,575 rows continue on extra sheets."));
    } else if (f === "pdf") {
      const mode = el("select", {}, [el("option", { value: "table" }, "Results table"), el("option", { value: "sheets" }, "One page per sheet (image + overlay + values)"), el("option", { value: "both" }, "Both")]);
      mode.value = s.pdf.mode;
      mode.addEventListener("change", () => {
        s.pdf.mode = mode.value;
        renderFormat();
      });
      formatOptions.append(el("label", { class: "field inline" }, "Layout ", mode));
      if (s.pdf.mode !== "table" && s.total !== null && s.total > 200) formatOptions.append(el("span", { class: "chip flag" }, `${s.total} sheets: one page each is slow and large; narrow the filter`));
    } else {
      const table = el("input", { value: s.sql.table, size: 16 });
      table.addEventListener("input", () => (s.sql.table = table.value.trim()));
      formatOptions.append(el("label", { class: "field inline" }, "Table ", table));
      if (f === "sql") {
        const dbUrl = el("input", { value: s.sql.url, size: 44, placeholder: "postgresql+psycopg://user:password@host/db" });
        dbUrl.addEventListener("input", () => (s.sql.url = dbUrl.value.trim()));
        formatOptions.append(el("label", { class: "field inline" }, "URL ", dbUrl));
      }
      formatOptions.append(el("span", { class: "muted small" }, "Rows are upserted on scan_id, so exporting again updates instead of duplicating."));
    }
  };

  const renderFields = () => {
    fieldTable.innerHTML = "";
    s.fields.forEach((f, index) => {
      const include = el("input", { type: "checkbox", checked: f.include });
      include.addEventListener("change", () => (f.include = include.checked));
      const header = el("input", { value: f.header, size: 14 });
      header.addEventListener("input", () => (f.header = header.value));
      const type = el("select", {}, TYPES.map((t) => el("option", { value: t }, t)));
      type.value = f.type;
      const fmt = el("input", { value: f.format, size: 9, placeholder: "%d%m%Y", title: "Date format as read (strptime)" });
      fmt.addEventListener("input", () => (f.format = fmt.value));
      fmt.classList.toggle("hidden", f.type !== "date");
      type.addEventListener("change", () => {
        f.type = type.value;
        fmt.classList.toggle("hidden", f.type !== "date");
      });
      const move = (delta) => {
        const target = index + delta;
        if (target < 0 || target >= s.fields.length) return;
        s.fields.splice(target, 0, s.fields.splice(index, 1)[0]);
        renderFields();
      };
      fieldTable.append(
        el(
          "tr",
          {},
          el("td", {}, include),
          el("td", { class: "mono" }, f.field),
          el("td", {}, header),
          el("td", {}, type, " ", fmt),
          el("td", { class: "actions" }, el("button", { class: "small ghost", title: "Up", onclick: () => move(-1) }, "↑"), el("button", { class: "small ghost", title: "Down", onclick: () => move(1) }, "↓"))
        )
      );
    });
  };

  const renderMeta = () => {
    metaBox.innerHTML = "";
    for (const name of META) {
      const box = el("input", { type: "checkbox", checked: s.meta.includes(name) });
      box.addEventListener("change", () => {
        s.meta = META.filter((m) => (m === name ? box.checked : s.meta.includes(m)));
      });
      metaBox.append(el("label", { class: "small" }, box, ` ${name}`));
    }
    optionBox.innerHTML = "";
    const labels = { includeReviewStatus: "review status", includeCorrected: "corrected (sheet)", includeFieldCorrected: "corrected (per field)", includeConfidence: "confidence", includeFlags: "flags", allowLossyCast: "allow lossy number casts (drops leading zeros)" };
    for (const [key, text] of Object.entries(labels)) {
      const box = el("input", { type: "checkbox", checked: s.opts[key] });
      box.addEventListener("change", () => (s.opts[key] = box.checked));
      optionBox.append(el("label", { class: "small" }, box, ` ${text}`));
    }
  };

  const renderAll = () => {
    renderFormat();
    renderFields();
    renderMeta();
  };

  // Save straight into a folder on the server (the scanning PC) instead of the export store
  const folderInput = el("input", { class: "mono", placeholder: "Leave empty to download from the browser", value: safeStorage("get", "omr_export_folder") || "", style: { minWidth: "22em" } });
  const folderRow = el(
    "div",
    { class: "row gap wrap" },
    el("label", { class: "field inline", title: "The file is written straight into this folder on the computer running the scanner, so large exports need no download" }, "Save to folder ", folderInput),
    el("button", { class: "small", onclick: () => openBrowser(folderInput.value, (path) => (folderInput.value = path)) }, "Browse…")
  );
  const request = (extra) => ({ format: formatSelect.value, filters, profile: profile(), sql_url: formatSelect.value === "sql" ? s.sql.url : undefined, folder: folderInput.value.trim() || undefined, ...extra });

  const preview = async () => {
    previewBox.innerHTML = "Loading…";
    try {
      const data = await api("/exports/preview", { method: "POST", json: request() });
      s.total = data.total;
      previewBox.innerHTML = "";
      for (const w of data.warnings) previewBox.append(el("div", { class: "res-warning" }, w));
      for (const e of data.errors) previewBox.append(el("div", { class: "res-warning error" }, e));
      const head = el("tr", {}, data.columns.map((c) => el("th", { title: `${c.field} · ${c.type}` }, c.key)));
      const body = data.rows.map((row) => el("tr", {}, row.map((v) => el("td", { class: "mono" }, v === null ? "" : String(v)))));
      const table = el("table", { class: "table" }, el("thead", {}, head), el("tbody", {}, body));
      previewBox.append(el("div", { class: "muted small" }, `${data.total} sheet(s) · first ${data.rows.length}:`), el("div", { class: "exp-table-wrap" }, table));
      renderFormat();
    } catch (error) {
      previewBox.innerHTML = "";
      previewBox.append(el("div", { class: "res-warning error" }, error.message));
    }
  };

  const run = async () => {
    safeStorage("set", "omr_export_format", formatSelect.value);
    safeStorage("set", "omr_export_folder", folderInput.value.trim());
    statusBox.innerHTML = "";
    let record;
    try {
      record = await api("/exports", { method: "POST", json: request() });
    } catch (error) {
      statusBox.append(el("div", { class: "res-warning error" }, error.message));
      return;
    }
    emit("exports-changed");
    const bar = el("div", { class: "progress" }, el("div", { style: { width: "0%" } }));
    const text = el("span", { class: "muted small" }, "Exporting…");
    const cancel = el("button", { class: "small danger", onclick: async () => { try { await api(`/exports/${record.id}/cancel`, { method: "POST" }); } catch (error) { toast(error.message, "error"); } } }, "Cancel export");
    const pause = el("button", { class: "small" }, "Pause");
    pause.addEventListener("click", async () => {
      const action = record.state === "paused" ? "resume" : "pause";
      try {
        record = await api(`/exports/${record.id}/${action}`, { method: "POST" });
        pause.textContent = record.state === "paused" ? "Resume" : "Pause";
      } catch (error) {
        toast(error.message, "error");
      }
    });
    statusBox.append(bar, text, " ", pause, " ", cancel, el("div", { class: "muted small" }, "You can close this window: the export keeps running on the server. Follow it, download it or cancel it in Jobs › Exports."));
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      // The window was closed: the Jobs tab follows the export from here
      if (!statusBox.isConnected) return;
      try {
        record = await api(`/exports/${record.id}`);
      } catch (error) {
        text.textContent = error.message;
        return;
      }
      const pct = record.total ? Math.min(100, Math.round((100 * (record.rows || 0)) / record.total)) : 0;
      bar.firstChild.style.width = `${pct}%`;
      text.textContent = `${record.state} · ${record.rows || 0} / ${record.total} rows`;
      pause.textContent = record.state === "paused" ? "Resume" : "Pause";
      if (!ACTIVE.includes(record.state)) break;
    }
    emit("exports-changed");
    statusBox.innerHTML = "";
    if (record.state !== "completed") {
      statusBox.append(el("div", { class: "res-warning error" }, record.error || record.state));
      return;
    }
    for (const w of record.warnings || []) statusBox.append(el("div", { class: "res-warning" }, w));
    if (record.path) {
      statusBox.append(el("div", { class: "res-warning preview" }, `Saved ${record.rows} rows to `, el("span", { class: "mono" }, record.path)));
      toast("Export saved", "ok");
      return;
    }
    statusBox.append(el("a", { class: "button primary", href: url(record.download_url), download: record.file_name }, `Download ${record.file_name} (${record.rows} rows)`));
    toast("Export ready", "ok");
  };

  const refreshProfiles = async () => {
    try {
      const data = await api("/export-profiles");
      profileSelect.innerHTML = "";
      profileSelect.append(el("option", { value: "" }, "— saved profiles —"));
      for (const p of data.profiles) profileSelect.append(el("option", { value: p.name }, p.name));
      profileSelect.profiles = data.profiles;
    } catch (e) {
      /* ignore */
    }
  };
  profileSelect.addEventListener("change", () => {
    const found = (profileSelect.profiles || []).find((p) => p.name === profileSelect.value);
    if (found) loadProfile(found.profile);
  });
  formatSelect.addEventListener("change", renderFormat);

  const saveProfile = async () => {
    const name = prompt("Profile name:", profileSelect.value || "");
    if (!name) return;
    try {
      await api(`/export-profiles/${encodeURIComponent(name)}`, { method: "PUT", json: profile() });
      await refreshProfiles();
      profileSelect.value = name;
      toast("Profile saved", "ok");
    } catch (error) {
      toast(error.message, "error", 6000);
    }
  };

  const jsonDetails = el(
    "details",
    {},
    el("summary", { class: "muted small" }, "Profile JSON"),
    jsonBox,
    el(
      "div",
      { class: "row gap" },
      el("button", { class: "small", onclick: () => (jsonBox.value = JSON.stringify(profile(), null, 1)) }, "Show current"),
      el(
        "button",
        {
          class: "small",
          onclick: () => {
            try {
              loadProfile(JSON.parse(jsonBox.value));
            } catch (e) {
              toast(`Invalid JSON: ${e.message}`, "error");
            }
          },
        },
        "Apply JSON"
      )
    )
  );
  jsonDetails.addEventListener("toggle", () => {
    if (jsonDetails.open) jsonBox.value = JSON.stringify(profile(), null, 1);
  });

  modal(
    "Export results",
    el(
      "div",
      { class: "exp" },
      el("div", { class: "row gap wrap" }, el("span", { class: "muted" }, "Sheets: "), chip(scopeText(filters), "ok"), el("span", { class: "spacer" }), profileSelect, el("button", { class: "small", onclick: saveProfile }, "Save profile…")),
      el("div", { class: "row gap wrap exp-format" }, el("label", { class: "field inline" }, "Format ", formatSelect), formatOptions),
      folderRow,
      el("div", { class: "exp-cols" }, el("div", {}, el("h3", {}, "Fields"), el("div", { class: "exp-table-wrap" }, el("table", { class: "table" }, el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Field"), el("th", {}, "Column name"), el("th", {}, "Type"), el("th", {}, ""))), fieldTable))), el("div", {}, el("h3", {}, "Sheet columns"), metaBox, el("h3", {}, "Per field"), optionBox, jsonDetails)),
      previewBox,
      statusBox
    ),
    [el("button", { onclick: preview }, "Preview"), el("button", { class: "primary", onclick: run }, "Export")],
    { wide: true }
  );

  // Field list in template order
  try {
    const data = await api("/exports/preview", { method: "POST", json: { format: "csv", filters, profile: {} } });
    s.total = data.total;
    s.fields = data.columns.filter((c) => c.source === "field").map((c) => ({ field: c.field, header: c.field, type: "text", format: "", outputFormat: "", include: true }));
  } catch (error) {
    toast(error.message, "error");
  }
  renderAll();
  refreshProfiles();
}
