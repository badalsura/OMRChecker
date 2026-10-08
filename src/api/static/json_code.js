// Small offline JSON code editor: strict parser with line numbers for errors
// and for every key path, a syntax highlighter and a textarea-based editor
// with a line-number gutter. No external libraries (works in the portable build).
import { el } from "./api.js";

// ------------------------------------------------------------------ parser
// parseJson(text) -> {value, error: {line, col, message} | null, lines: Map(path -> line)}
// Paths use the server's notation: "fieldBlocks.MCQ.origin", "preProcessors.0.options".
export function parseJson(text) {
  let i = 0;
  let line = 1;
  let lineStart = 0;
  const lines = new Map();
  const fail = (message) => {
    const error = new Error(message);
    error.loc = { line, col: i - lineStart + 1, message };
    throw error;
  };
  const ws = () => {
    for (;;) {
      const c = text[i];
      if (c === "\n") {
        line++;
        i++;
        lineStart = i;
      } else if (c === " " || c === "\t" || c === "\r") i++;
      else break;
    }
  };
  const describe = (c) => (c === undefined ? "the end of the text" : `'${c}'`);
  const str = () => {
    i++; // opening quote
    let out = "";
    for (;;) {
      const c = text[i];
      if (c === undefined || c === "\n") fail("This text is missing its closing quote \"");
      if (c === '"') {
        i++;
        return out;
      }
      if (c === "\\") {
        const n = text[i + 1];
        const map = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (n in map) {
          out += map[n];
          i += 2;
        } else if (n === "u" && /^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) {
          out += String.fromCharCode(parseInt(text.slice(i + 2, i + 6), 16));
          i += 6;
        } else fail(`Unknown escape \\${n || ""} in text`);
      } else {
        out += c;
        i++;
      }
    }
  };
  const num = () => {
    const m = /^-?(0|[1-9]\d*)(\.\d+)?([eE][+-]?\d+)?/.exec(text.slice(i, i + 64));
    if (!m) fail(`Unexpected ${describe(text[i])}: expected a value`);
    i += m[0].length;
    return Number(m[0]);
  };
  const value = (path) => {
    ws();
    lines.set(path, line);
    const c = text[i];
    if (c === "{") return obj(path);
    if (c === "[") return arr(path);
    if (c === '"') return str();
    if (c === "-" || (c >= "0" && c <= "9")) return num();
    for (const [word, v] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(word, i)) {
        i += word.length;
        return v;
      }
    }
    if (c === "'") fail("Use double quotes \" for text, not single quotes '");
    if (c === "}" || c === "]") fail(`Unexpected ${describe(c)}: a value is missing (trailing comma?)`);
    return fail(`Unexpected ${describe(c)}: expected a value`);
  };
  const join = (path, key) => (path === "$root" ? String(key) : `${path}.${key}`);
  const obj = (path) => {
    i++;
    const out = {};
    ws();
    if (text[i] === "}") {
      i++;
      return out;
    }
    for (;;) {
      ws();
      if (text[i] !== '"') fail(text[i] === "}" ? "Remove the comma before }" : `Expected a "key" in double quotes, found ${describe(text[i])}`);
      const keyLine = line;
      const key = str();
      if (Object.prototype.hasOwnProperty.call(out, key)) fail(`The key "${key}" appears twice`);
      ws();
      if (text[i] !== ":") fail(`Expected ':' after "${key}"`);
      i++;
      const child = join(path, key);
      out[key] = value(child);
      lines.set(child, keyLine);
      ws();
      if (text[i] === ",") {
        i++;
        continue;
      }
      if (text[i] === "}") {
        i++;
        return out;
      }
      fail(`Expected ',' or '}' after the value of "${key}", found ${describe(text[i])}`);
    }
  };
  const arr = (path) => {
    i++;
    const out = [];
    ws();
    if (text[i] === "]") {
      i++;
      return out;
    }
    for (;;) {
      out.push(value(join(path, out.length)));
      ws();
      if (text[i] === ",") {
        i++;
        continue;
      }
      if (text[i] === "]") {
        i++;
        return out;
      }
      fail(`Expected ',' or ']' in the list, found ${describe(text[i])}`);
    }
  };
  try {
    const v = value("$root");
    ws();
    if (i < text.length) fail(`Unexpected ${describe(text[i])} after the end of the JSON`);
    return { value: v, error: null, lines };
  } catch (error) {
    if (!error.loc) throw error;
    return { value: undefined, error: error.loc, lines };
  }
}

// Best line for a server error path (walks up to the nearest known parent).
export function lineForPath(lines, path) {
  let p = path || "$root";
  for (;;) {
    if (lines.has(p)) return lines.get(p);
    const cut = p.lastIndexOf(".");
    if (cut < 0) return lines.get("$root") || 1;
    p = p.slice(0, cut);
  }
}

// ------------------------------------------------------------------ highlighter
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const TOKEN = /("(?:[^"\\\n]|\\.)*"?)(\s*:)?|(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|\b(true|false|null)\b|([{}[\],:])/g;

export function highlight(text) {
  let out = "";
  let last = 0;
  TOKEN.lastIndex = 0;
  let m;
  while ((m = TOKEN.exec(text))) {
    out += esc(text.slice(last, m.index));
    if (m[1] !== undefined) {
      out += `<span class="${m[2] ? "tok-key" : "tok-str"}">${esc(m[1])}</span>`;
      if (m[2]) out += `<span class="tok-punc">${esc(m[2])}</span>`;
    } else if (m[3] !== undefined) out += `<span class="tok-num">${m[3]}</span>`;
    else if (m[4] !== undefined) out += `<span class="tok-lit">${m[4]}</span>`;
    else out += `<span class="tok-punc">${esc(m[5])}</span>`;
    last = TOKEN.lastIndex;
  }
  return out + esc(text.slice(last)) + "\n";
}

// ------------------------------------------------------------------ editor
export class CodeEditor {
  constructor({ onInput } = {}) {
    this.onInput = onInput;
    this.gutter = el("div", { class: "code-gutter" });
    this.pre = el("pre", { "aria-hidden": "true" });
    this.area = el("textarea", { spellcheck: "false", autocomplete: "off", autocapitalize: "off", wrap: "off" });
    this.root = el("div", { class: "code-wrap" }, this.gutter, el("div", { class: "code-area" }, this.pre, this.area));
    this.errorLines = new Set();
    this.pending = false;
    this.area.addEventListener("input", () => {
      this.schedule();
      if (this.onInput) this.onInput(this.area.value);
    });
    this.area.addEventListener("scroll", () => this.syncScroll());
    this.area.addEventListener("keydown", (e) => this.onKey(e));
  }

  get value() {
    return this.area.value;
  }

  set value(text) {
    this.area.value = text;
    this.render();
  }

  setErrorLines(lines) {
    this.errorLines = new Set(lines);
    this.renderGutter();
  }

  schedule() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => {
      this.pending = false;
      this.render();
    });
  }

  render() {
    this.pre.innerHTML = highlight(this.area.value);
    this.renderGutter();
    this.syncScroll();
  }

  renderGutter() {
    const count = this.area.value.split("\n").length;
    let html = "";
    for (let n = 1; n <= count; n++) html += (this.errorLines.has(n) ? `<span class="err">${n}</span>` : n) + "\n";
    this.gutter.innerHTML = html;
  }

  syncScroll() {
    this.pre.scrollTop = this.area.scrollTop;
    this.pre.scrollLeft = this.area.scrollLeft;
    this.gutter.scrollTop = this.area.scrollTop;
  }

  goToLine(line) {
    const parts = this.area.value.split("\n");
    let pos = 0;
    for (let n = 1; n < line && n <= parts.length; n++) pos += parts[n - 1].length + 1;
    this.area.focus();
    this.area.setSelectionRange(pos, pos + (parts[line - 1] || "").length);
    const lineHeight = parseFloat(getComputedStyle(this.area).lineHeight) || 19;
    this.area.scrollTop = Math.max(0, (line - 5) * lineHeight);
    this.syncScroll();
  }

  onKey(e) {
    if (e.key === "Tab" && !e.ctrlKey && !e.altKey) {
      e.preventDefault();
      const { selectionStart: s, selectionEnd: t, value } = this.area;
      this.area.value = value.slice(0, s) + "  " + value.slice(t);
      this.area.selectionStart = this.area.selectionEnd = s + 2;
      this.area.dispatchEvent(new Event("input"));
    } else if (e.key === "Enter" && !e.ctrlKey && !e.metaKey) {
      // keep the indentation of the current line
      e.preventDefault();
      const { selectionStart: s, selectionEnd: t, value } = this.area;
      const lineStart = value.lastIndexOf("\n", s - 1) + 1;
      let indent = /^\s*/.exec(value.slice(lineStart, s))[0];
      if (/[{[]\s*$/.test(value.slice(lineStart, s))) indent += "  ";
      this.area.value = value.slice(0, s) + "\n" + indent + value.slice(t);
      this.area.selectionStart = this.area.selectionEnd = s + 1 + indent.length;
      this.area.dispatchEvent(new Event("input"));
    }
  }
}
