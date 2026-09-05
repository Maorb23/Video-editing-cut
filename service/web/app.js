(() => {
  "use strict";

  const POLL_MS = 2500;
  const STORAGE_KEY = "video-editing-web-session-v1";
  const TELEMETRY_KEY = "video-editing-web-events-v1";
  const states = {
    analyzing: ["Analyzing your video.", 20],
    planning: ["Creating a proposed plan.", 40],
    awaiting_approval: ["Your plan is ready for review.", 60],
    approved: ["Your plan is approved. Start rendering when you are ready.", 70],
    rendering: ["Rendering your video.", 85],
    completed: ["Your accepted video is ready.", 100],
    failed: ["This edit could not be completed.", 100],
  };
  const el = (id) => document.getElementById(id);
  const panels = ["start-panel", "job-panel", "plan-panel", "render-panel", "result-panel", "failure-panel", "feedback-panel"];
  let editId = null;
  let activePlan = null;
  let timer = null;
  let selectedIteration = null;
  let latestEdit = null;
  let refreshSequence = 0;
  let followLatest = true;

  function record(event, details = {}) {
    const events = JSON.parse(localStorage.getItem(TELEMETRY_KEY) || "[]");
    events.push({ event, edit_id: editId, at: new Date().toISOString(), ...details });
    localStorage.setItem(TELEMETRY_KEY, JSON.stringify(events.slice(-200)));
  }
  function saveSession() { localStorage.setItem(STORAGE_KEY, JSON.stringify({ editId })); }
  function show(...ids) { panels.forEach((id) => el(id).classList.toggle("hidden", !ids.includes(id))); }
  function errorText(error) { return error?.error?.message || "The request could not be completed. Please try again."; }
  function showCreateError(message) { el("create-error").textContent = message; el("create-error").classList.remove("hidden"); }
  async function request(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw body;
    return body;
  }
  function stopPolling() { if (timer) window.clearTimeout(timer); timer = null; }
  function schedulePoll() { stopPolling(); timer = window.setTimeout(refresh, POLL_MS); }

  async function loadPlan() {
    const currentEdit = editId;
    const selection = selectedIteration;
    const plan = await request(`/v1/edits/${editId}/iterations/${selection}/plan`);
    if (currentEdit !== editId || selection !== selectedIteration) return;
    activePlan = plan;
    el("plan-summary").textContent = plan.summary;
    el("plan-document").textContent = JSON.stringify(plan.edit_plan, null, 2);
    const warnings = el("plan-warnings");
    warnings.replaceChildren();
    if (plan.warnings.length) {
      const list = document.createElement("ul");
      plan.warnings.forEach((warning) => { const item = document.createElement("li"); item.textContent = warning; list.append(item); });
      warnings.append(list); warnings.classList.remove("hidden");
    } else warnings.classList.add("hidden");
    const log = plan.decision_log || {};
    function items(id, values, describe) {
      const container = el(id); container.replaceChildren();
      if (!values?.length) { container.textContent = "None reported."; return; }
      const list = document.createElement("ul");
      values.forEach((value) => { const item = document.createElement("li"); item.textContent = describe(value); list.append(item); });
      container.append(list);
    }
    const confidence = (value) => Number.isFinite(value) ? ` (${Math.round(value * 100)}% confidence)` : "";
    items("observations", log.observations, (o) => `${o.description}${confidence(o.confidence)}${o.evidence?.length ? ` Evidence: ${o.evidence.join(", ")}` : ""}`);
    items("decisions", log.decisions, (d) => `${d.request}: ${d.operation}. ${d.reason}${confidence(d.confidence)}`);
    items("unsupported", log.unsupported, (v) => v);
    items("assumptions", log.assumptions, (v) => v);
    el("iteration-status").textContent = `Iteration ${String(plan.iteration).padStart(3, "0")} · Plan: ${plan.plan_status} · Preview: ${plan.preview_status}${plan.error ? ` · ${plan.error.message}` : ""}`;
    const preview = el("preview-video");
    preview.classList.toggle("hidden", !plan.preview_url);
    if (plan.preview_url && preview.getAttribute("src") !== plan.preview_url) {
      preview.pause();
      preview.src = plan.preview_url;
      preview.load();
    }
    if (!plan.preview_url && preview.getAttribute("src")) {
      preview.pause();
      preview.removeAttribute("src");
      preview.load();
    }
    if (plan.poster_url) preview.poster = plan.poster_url; else preview.removeAttribute("poster");
    el("inspection-link").classList.toggle("hidden", !plan.inspection_url);
    if (plan.inspection_url) el("inspection-link").href = plan.inspection_url;
    el("iteration-download").classList.toggle("hidden", !plan.video_url);
    if (plan.video_url) el("iteration-download").href = plan.video_url;
    el("approve-plan").disabled = plan.preview_status !== "succeeded" || !["awaiting_approval", "approved"].includes(plan.plan_status) || ["planning", "analyzing", "rendering"].includes(latestEdit?.state);
    el("request-changes").disabled = !["awaiting_approval", "approved", "completed", "failed"].includes(latestEdit?.state);
  }

  async function loadIterations(edit) {
    const history = await request(`/v1/edits/${editId}/iterations`);
    const available = history.iterations.filter((item) => item.plan_id);
    if (!available.length) return false;
    if (followLatest || selectedIteration === null) selectedIteration = available[available.length - 1].iteration;
    const selector = el("iteration-selector"); selector.replaceChildren();
    available.forEach((item) => {
      const option = document.createElement("option"); option.value = item.iteration;
      option.textContent = `Iteration ${String(item.iteration).padStart(3, "0")}${item.iteration === edit.iteration ? " (latest)" : ""}`;
      selector.append(option);
    });
    selector.value = selectedIteration;
    await loadPlan();
    return true;
  }
  async function loadResult() {
    const result = await request(`/v1/edits/${editId}/result`);
    el("result-video").src = result.video_url;
    el("download-video").href = result.video_url;
  }
  async function refresh() {
    if (!editId) return;
    const sequence = ++refreshSequence;
    try {
      const edit = await request(`/v1/edits/${editId}`);
      if (sequence !== refreshSequence) return;
      latestEdit = edit;
      const [message, percent] = states[edit.state] || ["Working on your edit.", 15];
      el("edit-id").textContent = `Edit ${editId}`;
      el("status-message").textContent = edit.progress?.message || message;
      el("progress-bar").style.width = `${percent}%`;
      const hasPlan = await loadIterations(edit);
      if (sequence !== refreshSequence) return;
      const visible = ["job-panel", "feedback-panel", ...(hasPlan ? ["plan-panel"] : [])];
      if (edit.state === "approved") visible.push("render-panel");
      if (edit.state === "completed") { await loadResult(); visible.push("result-panel"); }
      if (edit.state === "failed") { el("failure-message").textContent = edit.error?.message || message; visible.push("failure-panel"); }
      show(...visible);
      if (["analyzing", "planning", "rendering"].includes(edit.state) || edit.jobs?.some((job) => ["queued", "running"].includes(job.status))) schedulePoll(); else stopPolling();
    } catch (error) { const message = errorText(error); el("status-message").textContent = message; el("failure-message").textContent = message; show("job-panel", "failure-panel", "feedback-panel"); stopPolling(); }
  }
  function start(edit) { editId = edit.id; selectedIteration = null; followLatest = true; saveSession(); record("edit_created"); refresh(); }

  el("create-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = el("video-file").files[0];
    const instruction = el("instruction").value.trim();
    if (!file || !instruction) return;
    const button = el("create-edit"); button.disabled = true;
    el("create-error").classList.add("hidden");
    try {
      record("upload_started", { bytes: file.size });
      const form = new FormData(); form.append("file", file);
      const video = await request("/v1/videos", { method: "POST", body: form });
      record("upload_completed", { video_id: video.id });
      const edit = await request("/v1/edits", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ video_id: video.id, instruction }) });
      start(edit);
    } catch (error) { const message = errorText(error); showCreateError(message); record("create_failed", { message }); }
    finally { button.disabled = false; }
  });
  el("approve-plan").addEventListener("click", async () => {
    if (!activePlan) return;
    el("approve-plan").disabled = true;
    try { await request(`/v1/edits/${editId}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_id: activePlan.plan_id }) }); record("plan_approved", { plan_id: activePlan.plan_id }); await request(`/v1/edits/${editId}/render`, { method: "POST" }); await refresh(); }
    catch (error) { el("status-message").textContent = errorText(error); }
  });
  el("iteration-selector").addEventListener("change", async () => {
    followLatest = false;
    selectedIteration = Number(el("iteration-selector").value);
    try { await loadPlan(); } catch (error) { el("status-message").textContent = errorText(error); }
  });
  el("revision-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const instruction = el("revision-instruction").value.trim();
    if (!instruction) return;
    el("request-changes").disabled = true;
    el("revision-error").textContent = "";
    try {
      const edit = await request(`/v1/edits/${editId}/revise`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction }) });
      selectedIteration = null; activePlan = null; followLatest = true;
      el("revision-instruction").value = "";
      record("revision_requested", { iteration: edit.iteration });
      await refresh();
    } catch (error) { el("revision-error").textContent = errorText(error); el("request-changes").disabled = false; }
  });
  el("render-video").addEventListener("click", async () => {
    try { await request(`/v1/edits/${editId}/render`, { method: "POST" }); record("render_requested"); await refresh(); }
    catch (error) { el("status-message").textContent = errorText(error); }
  });
  el("new-edit").addEventListener("click", () => { stopPolling(); refreshSequence++; localStorage.removeItem(STORAGE_KEY); editId = null; activePlan = null; selectedIteration = null; show("start-panel"); });
  el("feedback-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const outcome = new FormData(event.currentTarget).get("outcome");
    const comment = el("feedback-comment").value.trim();
    record("feedback", { outcome, comment });
    el("feedback-status").textContent = "Feedback saved on this device. Use Export feedback to share it with the team.";
    event.currentTarget.reset();
  });
  el("export-feedback").addEventListener("click", () => {
    const payload = localStorage.getItem(TELEMETRY_KEY) || "[]";
    const url = URL.createObjectURL(new Blob([payload], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = "video-editor-feedback.json"; link.click(); URL.revokeObjectURL(url);
  });
  const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
  if (saved?.editId) { editId = saved.editId; show("job-panel"); refresh(); }
})();
