// Jobs tab: start bulk jobs (chunked uploads or a server folder) and watch progress.
import { api, chip, chunk, el, emit, fmtDuration, state, toast, url } from "./api.js";

const UPLOAD_CHUNK = 25;
let pollTimer = null;

export function initJobs() {
  document.getElementById("job-start").addEventListener("click", startJob);
  document.getElementById("jobs-refresh").addEventListener("click", refreshJobs);
  document.getElementById("job-dir-mode").addEventListener("change", (e) => {
    const input = document.getElementById("job-files");
    if (e.target.checked) input.setAttribute("webkitdirectory", "");
    else input.removeAttribute("webkitdirectory");
  });
}

export function onShowJobs() {
  refreshJobs();
}

const ACCEPTED = /\.(png|jpe?g|tiff?|bmp|webp|pdf)$/i;

async function startJob() {
  const templateId = document.getElementById("job-template").value;
  if (!templateId) return toast("Pick a template", "error");
  const files = [...document.getElementById("job-files").files].filter((f) => ACCEPTED.test(f.name));
  const folder = document.getElementById("job-folder").value.trim();
  if (!files.length && !folder) return toast("Choose files or enter a server folder", "error");
  const status = document.getElementById("job-upload-status");
  const button = document.getElementById("job-start");
  const base = () => {
    const form = new FormData();
    form.append("template_id", templateId);
    form.append("save_images", document.getElementById("job-images").value);
    const name = document.getElementById("job-name").value.trim();
    if (name) form.append("name", name);
    const workers = document.getElementById("job-workers").value;
    if (workers) form.append("workers", workers);
    return form;
  };
  button.disabled = true;
  try {
    let job;
    if (folder && !files.length) {
      const form = base();
      form.append("folder", folder);
      job = await api("/jobs", { method: "POST", form });
    } else {
      const parts = chunk(files, UPLOAD_CHUNK);
      const form = base();
      form.append("start", "false");
      if (folder) form.append("folder", folder);
      for (const f of parts[0]) form.append("files", f, f.webkitRelativePath ? f.webkitRelativePath.replace(/\//g, "_") : f.name);
      job = await api("/jobs", { method: "POST", form });
      let sent = parts[0].length;
      status.textContent = `Uploading ${sent}/${files.length}…`;
      for (const part of parts.slice(1)) {
        const more = new FormData();
        for (const f of part) more.append("files", f, f.webkitRelativePath ? f.webkitRelativePath.replace(/\//g, "_") : f.name);
        await api(`/jobs/${job.id}/files`, { method: "POST", form: more });
        sent += part.length;
        status.textContent = `Uploading ${sent}/${files.length}…`;
      }
      job = await api(`/jobs/${job.id}/start`, { method: "POST" });
    }
    status.textContent = `Job ${job.id.slice(0, 8)} started with ${job.total_files} files`;
    document.getElementById("job-files").value = "";
    toast("Job started", "ok");
    refreshJobs();
  } catch (error) {
    toast(error.message, "error", 6000);
    status.textContent = "";
  } finally {
    button.disabled = false;
  }
}

export async function refreshJobs() {
  clearTimeout(pollTimer);
  let data;
  try {
    data = await api("/jobs?limit=50");
  } catch (error) {
    toast(error.message, "error");
    return;
  }
  const names = Object.fromEntries(state.templates.map((t) => [t.id, t.name]));
  const body = document.querySelector("#jobs-table tbody");
  body.innerHTML = "";
  let active = false;
  for (const job of data.jobs) {
    if (["queued", "running", "uploading"].includes(job.state)) active = true;
    const total = job.total_files || 0;
    const pct = total ? Math.round((100 * (job.processed_files || 0)) / total) : 0;
    const counts = job.counts || {};
    body.append(
      el(
        "tr",
        {},
        el("td", {}, el("div", {}, job.name || job.id.slice(0, 8)), el("div", { class: "muted small" }, job.source === "folder" ? job.folder || "folder" : "upload")),
        el("td", {}, names[job.template_id] || job.template_id),
        el("td", {}, chip(job.state, job.state)),
        el("td", {}, el("div", { class: "progress" }, el("div", { style: { width: `${pct}%` } })), el("div", { class: "muted small" }, `${job.processed_files || 0} / ${total} files · ${pct}%`)),
        el("td", {}, counts.ok || 0),
        el("td", {}, counts.needs_review || 0),
        el("td", {}, counts.error || 0),
        el("td", {}, job.throughput_per_s ?? ""),
        el("td", {}, job.state === "running" ? fmtDuration(job.eta_s) : ""),
        el(
          "td",
          { class: "actions" },
          el("a", { class: "button small", href: url(`/jobs/${job.id}/results.csv`) }, "CSV"),
          counts.needs_review ? el("button", { class: "small", onclick: () => emit("review-job", job.id) }, "Review") : null,
          ["queued", "running", "uploading"].includes(job.state)
            ? el(
                "button",
                {
                  class: "small danger",
                  onclick: async () => {
                    await api(`/jobs/${job.id}/cancel`, { method: "POST" });
                    refreshJobs();
                  },
                },
                "Cancel"
              )
            : null
        )
      )
    );
  }
  if (!data.jobs.length) body.append(el("tr", {}, el("td", { colspan: 10, class: "muted" }, "No jobs yet")));
  if (active && document.getElementById("tab-jobs").classList.contains("active")) {
    pollTimer = setTimeout(refreshJobs, 1500);
  }
  if (!active) emit("review-changed");
}
