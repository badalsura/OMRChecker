// Who did what: a sheet's history (Results side panel) and the administrator's
// "Check changes" screen, which lists every sheet people changed after scanning
// with its changed settings and values, to verify them one by one or in bulk.
import { api, chip, el, emit, modal, state, toast } from "./api.js";

const ACTIONS = {
  scanned: "Scanned",
  reviewed: "Reviewed (accepted as read)",
  corrected: "Changed values",
  regraded: "Regraded",
  aligned: "Aligned by hand",
  typed: "Typed the values",
  verified: "Verified",
  unverified: "Took back the verification",
  undone: "Undid",
  redone: "Redid",
};
const PAGE = 25;

const when = (at) => (at ? new Date(at * 1000).toLocaleString() : "");
const shown = (v) => (v === "" || v === null || v === undefined ? "∅" : String(v));

function isAdmin() {
  const user = state.auth && state.auth.user;
  return !user || user.role === "admin";
}

function settingsText(regrade) {
  if (!regrade) return "";
  const all = { ...(regrade.template_overrides || {}), ...(regrade.config_overrides || {}) };
  return Object.keys(all).length ? JSON.stringify(all) : "template settings";
}

function detailText(item) {
  const d = item.detail || {};
  if (item.action === "regraded") {
    const changed = (d.changed || []).length;
    return `${settingsText(d)}${changed ? ` · ${changed} value(s) changed` : ""}`;
  }
  if (item.action === "undone" || item.action === "redone") return `${d.step || "a change"}${d.by ? ` by ${d.by}` : ""}`;
  if (item.action === "aligned") return d.kind === "index" ? `index points ${(d.names || []).join(", ")}` : "page corners";
  if (item.action === "typed") return Object.entries(d.values || {}).map(([k, v]) => `${k}=${shown(v)}`).join(", ");
  if (d.fields) return d.fields.join(", ") + (d.source === "bulk" ? " (bulk)" : "");
  if (d.accepted && d.accepted.length) return `accepted ${d.accepted.join(", ")}`;
  if (d.job) return d.job;
  return "";
}

// ---------------------------------------------------------------- side panel
export function historyBox(scanId) {
  const list = el("div", { class: "small" }, "Loading…");
  const box = el("details", { class: "res-audit" }, el("summary", {}, "Who did what"), list);
  let loaded = false;
  box.addEventListener("toggle", async () => {
    if (!box.open || loaded) return;
    loaded = true;
    try {
      const data = await api(`/scans/${scanId}/activity`);
      const rows = data.items.slice();
      if (data.scanned && data.scanned.by && !rows.some((i) => i.action === "scanned")) {
        rows.push({ action: "scanned", user: data.scanned.by, at: data.scanned.at });
      }
      list.innerHTML = "";
      if (!rows.length) {
        list.append(el("p", { class: "muted" }, "Nothing recorded for this sheet yet (actions before this log existed are not listed)."));
        return;
      }
      list.append(
        el(
          "ul",
          {},
          rows.map((i) =>
            el("li", {}, el("strong", {}, ACTIONS[i.action] || i.action), ` by ${i.user || "unknown"}`, detailText(i) ? el("span", {}, ` · ${detailText(i)}`) : null, el("span", { class: "muted" }, ` · ${when(i.at)}`))
          )
        )
      );
    } catch (error) {
      list.textContent = error.message;
    }
  });
  return box;
}

// ---------------------------------------------------------------- admin screen
function valuesList(values) {
  if (!values || !values.length) return el("span", { class: "muted" }, "no value changes");
  // The latest change of each value
  const last = {};
  values.forEach((v) => (last[v.name] = v));
  return el(
    "ul",
    { class: "act-values" },
    Object.values(last).map((v) => el("li", {}, el("code", {}, v.name), ` ${shown(v.original ?? v.old)} → ${shown(v.new)}`, el("span", { class: "muted" }, ` · ${v.user}`)))
  );
}

function changeCell(item) {
  const parts = [];
  if (item.regrade) parts.push(el("div", {}, el("strong", {}, "Regraded "), `by ${item.regrade.by}: `, el("code", {}, settingsText(item.regrade))));
  if (item.manual_alignment) parts.push(el("div", {}, el("strong", {}, "Aligned by hand "), `by ${item.manual_alignment.by} (${item.manual_alignment.kind === "index" ? "index points" : "page corners"})`));
  if (item.manual_entry) parts.push(el("div", {}, el("strong", {}, "Values typed "), `by ${item.manual_entry.by}`));
  parts.push(valuesList(item.values));
  return el("td", {}, parts);
}

export function showActivity(filters) {
  const st = { offset: 0, all: false, user: "", items: [], total: 0, ticked: new Set() };
  const table = el("div", {}, "Loading…");
  const summary = el("span", { class: "muted small" });
  const allBox = el("input", { type: "checkbox" });
  const userBox = el("input", { type: "search", placeholder: "changed by (user name)", class: "small" });
  const verifyTicked = el("button", { class: "primary", disabled: true }, "Verify ticked");
  const verifyPage = el("button", {}, "Verify all on this page");
  const prev = el("button", { class: "small ghost" }, "‹ Newer");
  const next = el("button", { class: "small ghost" }, "Older ›");
  if (!isAdmin()) {
    verifyTicked.disabled = verifyPage.disabled = true;
    verifyPage.title = verifyTicked.title = "Only an administrator can verify sheets in bulk";
  }
  const syncButtons = () => {
    verifyTicked.textContent = st.ticked.size ? `Verify ticked (${st.ticked.size})` : "Verify ticked";
    verifyTicked.disabled = !st.ticked.size || !isAdmin();
  };

  async function refresh() {
    const params = new URLSearchParams({ limit: PAGE, offset: st.offset });
    if (filters.template_id) params.set("template_id", filters.template_id);
    if (filters.job_id) params.set("job_id", filters.job_id);
    if (st.user) params.set("user", st.user);
    if (st.all) params.set("include_checked", "true");
    try {
      const data = await api(`/activity/sheets?${params}`);
      st.items = data.items;
      st.total = data.total;
    } catch (error) {
      table.textContent = error.message;
      return;
    }
    st.ticked.clear();
    syncButtons();
    const end = Math.min(st.offset + st.items.length, st.total);
    summary.textContent = st.total ? `${st.offset + 1}–${end} of ${st.total} sheet(s)${st.all ? "" : " waiting for a check"}` : "";
    prev.disabled = st.offset === 0;
    next.disabled = end >= st.total;
    table.innerHTML = "";
    if (!st.items.length) {
      table.append(el("p", { class: "muted" }, st.all ? "No sheet was changed after scanning." : "Every changed sheet is verified."));
      return;
    }
    const head = el("input", { type: "checkbox", title: "Tick every sheet on this page" });
    const boxes = [];
    head.addEventListener("change", () => {
      boxes.forEach(([box, id]) => {
        box.checked = head.checked;
        if (head.checked) st.ticked.add(id);
        else st.ticked.delete(id);
      });
      syncButtons();
    });
    table.append(
      el(
        "table",
        { class: "table act-table" },
        el("thead", {}, el("tr", {}, el("th", {}, head), el("th", {}, "Sheet"), el("th", {}, "Who"), el("th", {}, "Changed settings and values"), el("th", {}, ""))),
        el(
          "tbody",
          {},
          st.items.map((item) => {
            const box = el("input", { type: "checkbox" });
            boxes.push([box, item.scan_id]);
            box.addEventListener("change", () => {
              if (box.checked) st.ticked.add(item.scan_id);
              else st.ticked.delete(item.scan_id);
              syncButtons();
            });
            const verified = item.verified ? el("div", { class: "small" }, chip(`verified by ${item.verified.by}`, "ok")) : null;
            return el(
              "tr",
              {},
              el("td", {}, box),
              el("td", {}, el("div", { class: "mono" }, item.file_name || item.scan_id), el("div", {}, chip(item.status, item.status === "ok" ? "ok" : "flag")), verified, el("div", { class: "muted small" }, when(item.changed_at))),
              el(
                "td",
                { class: "small" },
                item.scanned && item.scanned.by ? el("div", {}, `Scanned: ${item.scanned.by}`) : null,
                el("div", {}, `Changed: ${item.users.join(", ") || "unknown"}`),
                el("div", { class: "muted" }, item.actions.map((a) => ACTIONS[a] || a).join(", "))
              ),
              changeCell(item),
              el(
                "td",
                {},
                el("button", { class: "small", title: "Open this sheet in Results", onclick: () => { dialog.close(); emit("results-scan", item.scan_id); } }, "Open"),
                " ",
                el("button", { class: "small primary", disabled: !isAdmin(), onclick: () => verify([item.scan_id]) }, "Verify")
              )
            );
          })
        )
      )
    );
  }

  async function verify(ids) {
    if (!ids.length) return;
    if (ids.length > 1 && !confirm(`Verify ${ids.length} sheet(s) with their current values? Flagged values on them are accepted as they are now.`)) return;
    try {
      const data = await api("/activity/verify", { method: "POST", json: { scan_ids: ids } });
      const failed = data.failed.length ? ` · ${data.failed.length} not verified: ${data.failed.map((f) => f.error).join("; ")}` : "";
      toast(`${data.verified.length} sheet(s) verified${failed}`, data.failed.length ? "error" : "ok", failed ? 7000 : 3000);
      emit("review-changed");
      emit("results-deleted"); // refresh the Results list
    } catch (error) {
      toast(error.message, "error", 6000);
    }
    refresh();
  }

  allBox.addEventListener("change", () => {
    st.all = allBox.checked;
    st.offset = 0;
    refresh();
  });
  let timer = null;
  userBox.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      st.user = userBox.value.trim();
      st.offset = 0;
      refresh();
    }, 300);
  });
  prev.addEventListener("click", () => {
    st.offset = Math.max(0, st.offset - PAGE);
    refresh();
  });
  next.addEventListener("click", () => {
    st.offset += PAGE;
    refresh();
  });
  verifyTicked.addEventListener("click", () => verify([...st.ticked]));
  verifyPage.addEventListener("click", () => verify(st.items.filter((i) => !i.verified).map((i) => i.scan_id)));

  const dialog = modal(
    "Check changes",
    el(
      "div",
      {},
      el(
        "p",
        { class: "muted small" },
        "Sheets people changed after scanning (values changed or accepted, regraded, aligned or typed by hand) that are not verified since. Check the settings and values, open a sheet to look at it, then verify. Uses the template and job picked in Results."
      ),
      el("div", { class: "row gap wrap" }, userBox, el("label", { class: "small" }, allBox, " include sheets already verified"), summary, prev, next),
      table
    ),
    [verifyPage, verifyTicked],
    { wide: true }
  );
  refresh();
}
