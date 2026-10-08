// Full-screen "Edit JSON" for template.json, config.json and evaluation.json (plan item 6).
// Line numbers, colouring, Format, errors as you type (JSON syntax at once, the engine's
// own rules from the server shortly after), Apply as one undo step, Download / Upload.
// Works offline: no external libraries.
import { api, download, el, toast } from "./api.js";
import { CodeEditor, lineForPath, parseJson } from "./json_code.js";

export const FILES = [
  { key: "template", file: "template.json", help: "Layout: page size, bubble blocks, zones, alignment, groups. Required." },
  { key: "config", file: "config.json", help: "Tuning and models. Empty = engine defaults." },
  { key: "evaluation", file: "evaluation.json", help: "Answer key and marking scheme. Empty = no scoring." },
];

// Text shown for each file when the dialog opens.
export function initialTexts(editor) {
  return {
    template: JSON.stringify(editor.doc, null, 2),
    config: prettyOrRaw(editor.configText),
    evaluation: prettyOrRaw(editor.evaluationText),
  };
}

function prettyOrRaw(text) {
  if (!text || !text.trim()) return "";
  const parsed = parseJson(text);
  return parsed.error ? text : JSON.stringify(parsed.value, null, 2);
}

// Local check of one file: {value, errors: [{line, message}], lines, empty}
export function checkText(key, text) {
  if (!text.trim()) {
    if (key === "template") return { value: undefined, errors: [{ line: 1, message: "template.json can't be empty" }], lines: new Map(), empty: true };
    return { value: null, errors: [], lines: new Map(), empty: true };
  }
  const parsed = parseJson(text);
  if (parsed.error) return { value: undefined, errors: [{ line: parsed.error.line, message: `Line ${parsed.error.line}, column ${parsed.error.col}: ${parsed.error.message}` }], lines: parsed.lines, empty: false };
  if (parsed.value === null || typeof parsed.value !== "object" || Array.isArray(parsed.value)) {
    return { value: undefined, errors: [{ line: 1, message: "The file must be one JSON object { … }" }], lines: parsed.lines, empty: false };
  }
  return { value: parsed.value, errors: [], lines: parsed.lines, empty: false };
}

// Plain-language wording for common schema messages.
export function explain(path, message) {
  let m = String(message || "");
  m = m.replace(/^Additional properties are not allowed \((.*) (was|were) unexpected\)$/, (_, names) => `Unknown option ${names}: check the spelling, or remove it`);
  m = m.replace(/^'(.+)' is a required property$/, "'$1' is missing");
  m = m.replace(/^(.+) is not one of (\[.*\])$/, "$1 is not allowed here; use one of $2");
  m = m.replace(/^(.+) is not of type '(\w+)'$/, (_, v, t) => `${v} should be ${{ string: "text", number: "a number", integer: "a whole number", boolean: "true or false", object: "an object { }", array: "a list [ ]" }[t] || t}`);
  const where = path && path !== "$root" ? `${path}: ` : "";
  return where + m;
}

class JsonDialog {
  constructor(editor) {
    this.editor = editor;
    this.texts = initialTexts(editor);
    this.original = { ...this.texts };
    this.checks = {};
    this.server = { template: [], config: [], evaluation: [] };
    this.serverState = "idle";
    this.tab = "template";
    this.seq = 0;
  }

  open() {
    this.code = new CodeEditor({ onInput: (text) => this.onInput(text) });
    this.tabsEl = el("div", { class: "fs-tabs", role: "tablist" });
    this.errorsEl = el("div", { class: "code-errors" });
    this.statusEl = el("span", { class: "muted small" });
    this.applyBtn = el("button", { class: "primary", title: "Apply to the editor (Ctrl+Enter); undo with Ctrl+Z in the editor", onclick: () => this.apply() }, "Apply");
    this.uploadInput = el("input", { type: "file", accept: ".json,application/json", hidden: true, onchange: (e) => this.upload(e.target.files[0]) });
    this.helpEl = el("span", { class: "muted small" });
    const panel = el(
      "div",
      { class: "fs-panel", role: "dialog", "aria-label": "Edit JSON" },
      el(
        "div",
        { class: "fs-head" },
        el("h2", {}, "Edit JSON"),
        this.tabsEl,
        el("span", { class: "spacer" }),
        el("button", { class: "small", title: "Re-indent this file (Ctrl+Shift+F)", onclick: () => this.format() }, "Format"),
        el("button", { class: "small", title: "Download this file", onclick: () => this.download() }, "Download"),
        el("button", { class: "small", title: "Replace this tab's text with a file from your computer (not applied until you click Apply)", onclick: () => this.uploadInput.click() }, "Upload…"),
        this.uploadInput,
        el("button", { class: "small ghost", title: "Close (Esc)", onclick: () => this.close() }, "✕")
      ),
      el("div", { class: "fs-body" }, this.code.root, this.errorsEl),
      el("div", { class: "fs-foot" }, this.helpEl, el("span", { class: "spacer" }), this.statusEl, el("button", { class: "ghost", onclick: () => this.close() }, "Cancel"), this.applyBtn)
    );
    this.backdrop = el("div", { class: "fs-backdrop" }, panel);
    // Listen on the dialog itself so the template editor's shortcuts never see these keys
    this.backdrop.addEventListener("keydown", (e) => this.onKey(e));
    document.body.append(this.backdrop);
    for (const f of FILES) this.checks[f.key] = checkText(f.key, this.texts[f.key]);
    this.showTab("template");
    this.validateSoon(0);
  }

  onKey(e) {
    // Keep the template editor's own shortcuts (Delete, Ctrl+Z, B, Z…) away while this is open
    e.stopPropagation();
    if (e.key === "Escape") {
      e.preventDefault();
      this.close();
    } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      this.apply();
    } else if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "F" || e.key === "f")) {
      e.preventDefault();
      this.format();
    } else if ((e.ctrlKey || e.metaKey) && (e.key === "s" || e.key === "S")) {
      e.preventDefault();
      this.apply();
    }
  }

  changed() {
    return FILES.some((f) => this.texts[f.key] !== this.original[f.key]);
  }

  close() {
    if (this.changed() && !confirm("Close without applying your JSON changes?")) return;
    clearTimeout(this.timer);
    this.backdrop.remove();
  }

  showTab(key) {
    this.texts[this.tab] = this.code.value;
    this.tab = key;
    this.code.value = this.texts[key];
    this.helpEl.textContent = FILES.find((f) => f.key === key).help;
    this.render();
    this.code.area.focus();
  }

  onInput(text) {
    this.texts[this.tab] = text;
    this.checks[this.tab] = checkText(this.tab, text);
    this.server[this.tab] = [];
    this.render();
    this.validateSoon(700);
  }

  // All problems of one file: local syntax first, then the server's (engine) rules.
  problems(key) {
    const check = this.checks[key];
    if (check.errors.length) return check.errors;
    return (this.server[key] || []).map((e) => ({ line: lineForPath(check.lines, e.path), message: explain(e.path, e.message) }));
  }

  render() {
    this.tabsEl.innerHTML = "";
    for (const f of FILES) {
      const count = this.problems(f.key).length;
      const edited = this.texts[f.key] !== this.original[f.key];
      this.tabsEl.append(
        el(
          "button",
          { class: `small${f.key === this.tab ? " active" : ""}`, role: "tab", title: f.help, onclick: () => this.showTab(f.key) },
          f.file,
          edited ? " •" : "",
          count ? el("span", { class: "dot-err" }, `(${count})`) : null
        )
      );
    }
    const problems = this.problems(this.tab);
    this.code.setErrorLines(problems.map((p) => p.line));
    this.errorsEl.innerHTML = "";
    if (!problems.length) {
      const msg = this.serverState === "checking" ? "Checking with the engine's rules…" : this.serverState === "offline" ? "JSON is valid (the server's checks could not run)." : this.checks[this.tab].empty ? "Empty: the engine uses its defaults." : "No problems found.";
      this.errorsEl.append(el("div", { class: this.serverState === "ok" ? "ok" : "muted" }, msg));
    } else {
      this.errorsEl.append(
        el("strong", {}, `${problems.length} problem${problems.length > 1 ? "s" : ""}`),
        el(
          "ul",
          {},
          problems.map((p) => el("li", { title: "Go to the line", onclick: () => this.code.goToLine(p.line) }, el("code", {}, `line ${p.line}`), " ", p.message))
        )
      );
    }
    const blocked = FILES.some((f) => this.problems(f.key).length);
    this.applyBtn.disabled = blocked;
    this.statusEl.textContent = blocked ? "Fix the problems before applying" : "";
  }

  validateSoon(ms) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.validate(), ms);
  }

  async validate() {
    clearTimeout(this.timer);
    this.timer = null;
    if (FILES.some((f) => this.checks[f.key].errors.length)) {
      this.serverState = "idle";
      return this.render();
    }
    const seq = ++this.seq;
    this.serverState = "checking";
    this.render();
    const body = { clear: [] };
    for (const f of FILES) {
      if (this.checks[f.key].empty) body.clear.push(f.key);
      else body[f.key] = this.checks[f.key].value;
    }
    try {
      const result = await api(`/templates/${encodeURIComponent(this.editor.id)}/validate`, { method: "POST", json: body });
      if (seq !== this.seq) return;
      this.server = { template: result.template || [], config: result.config || [], evaluation: result.evaluation || [] };
      this.serverState = "ok";
    } catch (error) {
      if (seq !== this.seq) return;
      this.server = { template: [], config: [], evaluation: [] };
      this.serverState = "offline";
    }
    this.render();
  }

  format() {
    const check = checkText(this.tab, this.code.value);
    if (check.errors.length || check.empty) {
      if (check.errors.length) toast("Fix the JSON syntax first", "error");
      return;
    }
    const text = JSON.stringify(check.value, null, 2);
    this.code.value = text;
    this.onInput(text);
  }

  download() {
    const f = FILES.find((x) => x.key === this.tab);
    const text = this.code.value.trim() ? this.code.value : "{}";
    download(f.file, text.endsWith("\n") ? text : text + "\n", "application/json");
  }

  async upload(file) {
    this.uploadInput.value = "";
    if (!file) return;
    if (file.size > 10 * 1024 * 1024) return toast("That file is too large for a JSON file", "error");
    const text = await file.text();
    const check = checkText(this.tab, text);
    const pretty = !check.errors.length && !check.empty ? JSON.stringify(check.value, null, 2) : text;
    this.code.value = pretty;
    this.onInput(pretty);
    toast(`Loaded ${file.name} into ${FILES.find((x) => x.key === this.tab).file}: click Apply to use it`);
  }

  async apply() {
    this.texts[this.tab] = this.code.value;
    for (const f of FILES) this.checks[f.key] = checkText(f.key, this.texts[f.key]);
    if (this.serverState === "checking" || this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
      await this.validate();
    }
    const bad = FILES.find((f) => this.problems(f.key).length);
    if (bad) {
      this.showTab(bad.key);
      return toast(`${bad.file} has problems: it was not applied`, "error");
    }
    if (!this.changed()) return this.close();
    const files = {};
    if (this.texts.template !== this.original.template) files.template = this.checks.template.value;
    for (const key of ["config", "evaluation"]) {
      if (this.texts[key] !== this.original[key]) files[`${key}Text`] = this.checks[key].empty ? "" : JSON.stringify(this.checks[key].value, null, 2);
    }
    this.editor.applyFiles(files);
    this.original = { ...this.texts };
    this.close();
    toast("Applied: Save to keep it, Ctrl+Z to undo", "ok");
  }
}

export function openJsonEditor(editor) {
  const dialog = new JsonDialog(editor);
  dialog.open();
  return dialog;
}
