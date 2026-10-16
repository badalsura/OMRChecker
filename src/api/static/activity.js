// Who did what: a sheet's history (Results side panel) and the administrators'
// "Check changes" tab: hand flagged sheets out to people for review, then check
// every sheet people changed (with its settings and values), verify it or send
// it back to review.
import { api, chip, el, emit, state, toast } from "./api.js";

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
  assigned: "Assigned",
  returned: "Sent back to review",
};
const PAGE = 25;

const when = (at) => (at ? new Date(at * 1000).toLocaleString() : "");
const shown = (v) => (v === "" || v === null || v === undefined ? "∅" : String(v));

export function isAdmin() {
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
  if (item.action === "assigned") return `to ${d.to}`;
  if (item.action === "returned") return `${(d.fields || []).join(", ")}${d.note ? ` · “${d.note}”` : ""}`;
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

// ---------------------------------------------------------------- the tab
const $ = (id) => document.getElementById(id);
const tab = { offset: 0, items: [], total: 0, ticked: new Set(), started: false };

function filters() {
  const f = { template_id: $("chg-template").value, job_id: $("chg-job").value };
  for (const k of Object.keys(f)) if (!f[k]) delete f[k];
  return f;
}

async function fillJobs() {
  try {
    const data = await api("/jobs?limit=200");
    const select = $("chg-job");
    const current = select.value;
    select.innerHTML = "";
    select.append(el("option", { value: "" }, "All jobs"));
    for (const job of data.jobs) select.append(el("option", { value: job.id }, `${job.name || job.id.slice(0, 8)} (${job.processed_files || 0})`));
    if ([...select.options].some((o) => o.value === current)) select.value = current;
  } catch (e) {
    /* ignore */
  }
}

// People who can be handed work: accounts (sign-in on), else names already used
async function fillPeople() {
  const names = new Set();
  try {
    const data = await api("/auth/users");
    for (const u of data.users || data || []) if (u && u.active !== false && u.name) names.add(u.name);
  } catch (e) {
    /* sign-in off, or not an administrator */
  }
  try {
    const data = await api(`/assignments/summary?${new URLSearchParams(filters())}`);
    for (const u of data.users) names.add(u.user);
  } catch (e) {
    /* ignore */
  }
  const list = $("chg-people-list");
  list.innerHTML = "";
  for (const name of [...names].sort()) list.append(el("option", { value: name }));
  return [...names].sort();
}

async function renderSummary() {
  const box = $("chg-summary");
  let data;
  try {
    data = await api(`/assignments/summary?${new URLSearchParams(filters())}`);
  } catch (error) {
    box.textContent = error.message;
    return;
  }
  box.innerHTML = "";
  box.append(el("p", { class: "small" }, `Flagged sheets nobody has yet: ${data.unassigned_flagged}`));
  if (!data.users.length) {
    box.append(el("p", { class: "muted small" }, "No sheets are assigned for this template and job."));
    return;
  }
  box.append(
    el(
      "table",
      { class: "table act-table" },
      el("thead", {}, el("tr", {}, ["Person", "Assigned", "Still to review", "Nothing left to review", "Verified", ""].map((h) => el("th", {}, h)))),
      el(
        "tbody",
        {},
        data.users.map((u) =>
          el(
            "tr",
            {},
            el("td", {}, u.user),
            el("td", {}, String(u.assigned)),
            el("td", {}, String(u.to_review)),
            el("td", {}, String(u.reviewed)),
            el("td", {}, String(u.verified)),
            el(
              "td",
              {},
              el("button", { class: "small ghost", title: "Show this person's changed sheets below", onclick: () => { $("chg-user").value = u.user; tab.offset = 0; refresh(); } }, "Their changes"),
              " ",
              el(
                "button",
                {
                  class: "small ghost",
                  disabled: !isAdmin(),
                  title: "Take back every sheet assigned to this person (in this template and job)",
                  onclick: async () => {
                    if (!confirm(`Take back all sheets assigned to ${u.user}?`)) return;
                    try {
                      const r = await api("/assignments/clear", { method: "POST", json: { user: u.user, ...filters() } });
                      toast(`${r.unassigned} sheet(s) taken back`, "ok");
                      renderSummary();
                    } catch (error) {
                      toast(error.message, "error", 5000);
                    }
                  },
                },
                "Take back"
              )
            )
          )
        )
      )
    )
  );
}

async function handOut() {
  const users = $("chg-people").value.split(",").map((s) => s.trim()).filter(Boolean);
  if (!users.length) return toast("Name at least one person (comma between names)", "error");
  const body = { users, view: $("chg-which").value, only_unassigned: $("chg-only-new").checked, ...filters() };
  const limit = Number($("chg-limit").value);
  if (limit > 0) body.limit = limit;
  try {
    const data = await api("/assignments", { method: "POST", json: body });
    const parts = Object.entries(data.assigned).map(([name, n]) => `${name}: ${n}`);
    toast(data.total ? `Assigned ${data.total} sheet(s) · ${parts.join(", ")}` : "No sheets matched", data.total ? "ok" : "", 5000);
    renderSummary();
  } catch (error) {
    toast(error.message, "error", 6000);
  }
}

async function refresh() {
  const table = $("chg-table");
  const params = new URLSearchParams({ limit: PAGE, offset: tab.offset, ...filters() });
  if ($("chg-user").value.trim()) params.set("user", $("chg-user").value.trim());
  if ($("chg-all").checked) params.set("include_checked", "true");
  try {
    const data = await api(`/activity/sheets?${params}`);
    tab.items = data.items;
    tab.total = data.total;
  } catch (error) {
    table.textContent = error.message;
    return;
  }
  tab.ticked.clear();
  syncButtons();
  const end = Math.min(tab.offset + tab.items.length, tab.total);
  $("chg-count").textContent = tab.total ? `${tab.offset + 1}–${end} of ${tab.total} sheet(s)${$("chg-all").checked ? "" : " waiting for a check"}` : "";
  $("chg-prev").disabled = tab.offset === 0;
  $("chg-next").disabled = end >= tab.total;
  table.innerHTML = "";
  if (!tab.items.length) {
    table.append(el("p", { class: "muted" }, $("chg-all").checked ? "No sheet was changed after scanning." : "Every changed sheet is verified."));
    return;
  }
  const head = el("input", { type: "checkbox", title: "Tick every sheet on this page" });
  const boxes = [];
  head.addEventListener("change", () => {
    boxes.forEach(([box, id]) => {
      box.checked = head.checked;
      if (head.checked) tab.ticked.add(id);
      else tab.ticked.delete(id);
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
        tab.items.map((item) => {
          const box = el("input", { type: "checkbox" });
          boxes.push([box, item.scan_id]);
          box.addEventListener("change", () => {
            if (box.checked) tab.ticked.add(item.scan_id);
            else tab.ticked.delete(item.scan_id);
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
              item.assigned_to ? el("div", {}, `Assigned to: ${item.assigned_to.user}`) : null,
              el("div", {}, `Changed: ${item.users.join(", ") || "unknown"}`),
              el("div", { class: "muted" }, item.actions.map((a) => ACTIONS[a] || a).join(", "))
            ),
            changeCell(item),
            el(
              "td",
              {},
              el("button", { class: "small", title: "Open this sheet in Results", onclick: () => emit("results-scan", item.scan_id) }, "Open"),
              " ",
              el("button", { class: "small primary", disabled: !isAdmin(), onclick: () => verify([item.scan_id]) }, "Verify"),
              " ",
              el("button", { class: "small ghost", disabled: !isAdmin(), title: "Take back the verification and put its flagged and changed values back in the review queue", onclick: () => sendBack([item.scan_id]) }, "Send back")
            )
          );
        })
      )
    )
  );
}

function syncButtons() {
  const n = tab.ticked.size;
  $("chg-verify").textContent = n ? `Verify ticked (${n})` : "Verify ticked";
  $("chg-return").textContent = n ? `Send ticked back (${n})` : "Send ticked back";
  $("chg-verify").disabled = $("chg-return").disabled = !n || !isAdmin();
  $("chg-verify-page").disabled = !isAdmin();
}

async function verify(ids) {
  if (!ids.length) return;
  if (ids.length > 1 && !confirm(`Verify ${ids.length} sheet(s) with their current values? Flagged values on them are accepted as they are now.`)) return;
  try {
    const data = await api("/activity/verify", { method: "POST", json: { scan_ids: ids } });
    const failed = data.failed.length ? ` · ${data.failed.length} not verified: ${data.failed.map((f) => f.error).join("; ")}` : "";
    toast(`${data.verified.length} sheet(s) verified${failed}`, data.failed.length ? "error" : "ok", failed ? 7000 : 3000);
    emit("review-changed");
    emit("results-deleted");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  refresh();
  renderSummary();
}

async function sendBack(ids) {
  if (!ids.length) return;
  const to = prompt(`Send ${ids.length} sheet(s) back to review. Hand them to (a name, or leave empty to keep the current assignee):`, "");
  if (to === null) return;
  const note = prompt("A note for the reviewer (optional):", "") || "";
  try {
    const data = await api("/activity/return", { method: "POST", json: { scan_ids: ids, assign_to: to.trim() || null, note: note.trim() || null } });
    const failed = data.failed.length ? ` · ${data.failed.length} not sent: ${data.failed.map((f) => f.error).join("; ")}` : "";
    toast(`${data.returned.length} sheet(s) back in the review queue${failed}`, data.failed.length ? "error" : "ok", failed ? 7000 : 3000);
    emit("review-changed");
    emit("results-deleted");
  } catch (error) {
    toast(error.message, "error", 6000);
  }
  refresh();
  renderSummary();
}

export function initChanges() {
  const reload = () => {
    tab.offset = 0;
    refresh();
    renderSummary();
    fillPeople();
  };
  $("chg-template").addEventListener("change", reload);
  $("chg-job").addEventListener("change", reload);
  $("chg-all").addEventListener("change", reload);
  let timer = null;
  $("chg-user").addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(reload, 300);
  });
  $("chg-prev").addEventListener("click", () => {
    tab.offset = Math.max(0, tab.offset - PAGE);
    refresh();
  });
  $("chg-next").addEventListener("click", () => {
    tab.offset += PAGE;
    refresh();
  });
  $("chg-assign").addEventListener("click", handOut);
  $("chg-verify").addEventListener("click", () => verify([...tab.ticked]));
  $("chg-return").addEventListener("click", () => sendBack([...tab.ticked]));
  $("chg-verify-page").addEventListener("click", () => verify(tab.items.filter((i) => !i.verified).map((i) => i.scan_id)));
}

// The tab is for administrators when sign-in is on
export function syncChangesTab() {
  const button = document.querySelector('.tabs button[data-tab="changes"]');
  if (button) button.classList.toggle("hidden", !isAdmin());
}

export function onShowChanges() {
  if (!tab.started) {
    tab.started = true;
    fillJobs();
  }
  fillPeople();
  refresh();
  renderSummary();
}
