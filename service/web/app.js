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
  const panels = ["auth-panel", "profile-panel", "projects-panel", "start-panel", "job-panel", "plan-panel", "render-panel", "result-panel", "failure-panel", "feedback-panel"];
  let editId = null;
  let activePlan = null;
  let timer = null;
  let selectedIteration = null;
  let latestEdit = null;
  let refreshSequence = 0;
  let followLatest = true;
  let currentUser = null;
  const landingMode = new URLSearchParams(location.search).get("landing") === "1";

  function describeFile() {
    const file = el("video-file").files[0];
    el("upload-zone").classList.toggle("selected", Boolean(file));
    el("upload-status").textContent = file ? `${file.name} · ${(file.size / 1048576).toFixed(1)} MB · Ready to upload` : "Your original video stays untouched.";
    updateWorkflow(file ? 1 : 0);
  }
  el("video-file").addEventListener("change", describeFile);
  ["dragenter", "dragover"].forEach((name) => el("upload-zone").addEventListener(name, (event) => { event.preventDefault(); if (!el("create-edit").disabled) el("upload-zone").classList.add("dragging"); }));
  el("upload-zone").addEventListener("dragleave", (event) => { if (!el("upload-zone").contains(event.relatedTarget)) el("upload-zone").classList.remove("dragging"); });
  el("upload-zone").addEventListener("drop", (event) => {
    event.preventDefault(); el("upload-zone").classList.remove("dragging");
    if (el("create-edit").disabled) return;
    const files = event.dataTransfer.files;
    if (files.length !== 1 || !/\.(mp4|mov|webm|mkv)$/i.test(files[0].name)) { showCreateError("Choose one MP4, MOV, WebM, or MKV video to start your edit."); return; }
    el("video-file").files = files; el("create-error").classList.add("hidden"); describeFile();
  });

  function record(event, details = {}) {
    const events = JSON.parse(localStorage.getItem(TELEMETRY_KEY) || "[]");
    events.push({ event, edit_id: editId, at: new Date().toISOString(), ...details });
    localStorage.setItem(TELEMETRY_KEY, JSON.stringify(events.slice(-200)));
  }
  function saveSession() { localStorage.setItem(STORAGE_KEY, JSON.stringify({ editId })); }
  function show(...ids) {
    panels.forEach((id) => el(id).classList.toggle("hidden", !ids.includes(id)));
    const section = ids.includes("profile-panel") ? "account" : ids.includes("projects-panel") ? "projects" : "editor";
    ["editor", "projects", "account"].forEach((name) => {
      const button = el(`side-${name}`);
      button.classList.toggle("active", name === section);
      if (name === section) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current");
    });
    el("workflow").classList.toggle("hidden", section !== "editor");
    if (ids.includes("start-panel")) updateWorkflow(el("video-file").files.length ? 1 : 0);
  }
  function updateWorkflow(index) {
    el("workflow").querySelectorAll("li").forEach((item, step) => {
      item.classList.toggle("done", step < index);
      item.classList.toggle("current", step === index);
      if (step === index) item.setAttribute("aria-current", "step"); else item.removeAttribute("aria-current");
    });
  }
  function busy(id, active, label) {
    const button = el(id);
    if (!button.dataset.label) button.dataset.label = button.textContent;
    button.disabled = active;
    button.setAttribute("aria-busy", String(active));
    button.textContent = active ? label : button.dataset.label;
  }
  function actionError(error) { el("action-error").textContent = errorText(error); el("action-error").classList.remove("hidden"); }
  function errorText(error) { return error?.error?.message || error?.detail || "The request could not be completed. Please try again."; }
  function authErrorText(error) { return error?.status === 404 ? "Authentication is not available on the running server. Restart the API and try again." : errorText(error); }
  function showCreateError(message) { el("create-error").textContent = message; el("create-error").classList.remove("hidden"); }
  function showAuthError(id, message) { el(id).textContent = message; el(id).classList.remove("hidden"); }
  function signedIn(user) {
    currentUser = user;
    el("user-email").textContent = user.email;
    el("profile-email").textContent = user.email;
    el("profile-id").textContent = user.id;
    el("app-shell").classList.toggle("hidden", landingMode);
    el("landing-panel").classList.toggle("hidden", !landingMode);
    document.querySelectorAll(".public-nav").forEach((node) => node.classList.add("hidden"));
    document.querySelectorAll(".private-nav").forEach((node) => node.classList.remove("hidden"));
    ["user-email", "logout", "export-feedback"].forEach((id) => el(id).classList.remove("hidden"));
    el("verification-banner").classList.toggle("hidden", Boolean(user.email_verified));
    el("profile-verification").textContent = user.email_verified ? "Verified" : "Unverified";
    el("profile-verification").classList.toggle("verified", Boolean(user.email_verified));
    if (!landingMode) show("start-panel"); else show();
    loadCredits().catch(() => { el("nav-credit-balance").textContent = "Credits unavailable"; });
  }
  async function request(path, options = {}) {
    const response = await fetch(path, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw { ...body, status: response.status };
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
    function empty(container) {
      const message = document.createElement("p");
      message.className = "empty-state";
      message.textContent = "None reported.";
      container.append(message);
    }
    function confidenceBadge(value) {
      if (!Number.isFinite(value)) return null;
      const badge = document.createElement("span");
      badge.className = "confidence";
      badge.textContent = `${Math.round(value * 100)}% confidence`;
      return badge;
    }
    function renderObservations(values) {
      const container = el("observations"); container.replaceChildren();
      if (!values?.length) { empty(container); return; }
      values.forEach((observation) => {
        const item = document.createElement("article"); item.className = "rationale-item";
        const description = document.createElement("p"); description.textContent = observation.description; item.append(description);
        const metadata = document.createElement("div"); metadata.className = "rationale-meta";
        const badge = confidenceBadge(observation.confidence); if (badge) metadata.append(badge);
        (observation.evidence || []).forEach((value) => { const evidence = document.createElement("code"); evidence.textContent = value; metadata.append(evidence); });
        item.append(metadata); container.append(item);
      });
    }
    function renderDecisions(values) {
      const container = el("decisions"); container.replaceChildren();
      if (!values?.length) { empty(container); return; }
      values.forEach((decision) => {
        const item = document.createElement("article"); item.className = "rationale-item";
        const heading = document.createElement("strong"); heading.textContent = decision.request; item.append(heading);
        const reason = document.createElement("p"); reason.textContent = decision.reason; item.append(reason);
        const metadata = document.createElement("div"); metadata.className = "rationale-meta";
        const operation = document.createElement("span"); operation.className = "operation"; operation.textContent = decision.operation; metadata.append(operation);
        const badge = confidenceBadge(decision.confidence); if (badge) metadata.append(badge);
        item.append(metadata); container.append(item);
      });
    }
    function renderSimple(id, values) {
      const container = el(id); container.replaceChildren();
      if (!values?.length) { empty(container); return; }
      const list = document.createElement("ul");
      values.forEach((value) => { const item = document.createElement("li"); item.textContent = value; list.append(item); });
      container.append(list);
    }
    renderObservations(log.observations);
    const silenceContainer = el("detected-silences"); silenceContainer.replaceChildren();
    const silence = plan.edit_plan?.analysis?.silence;
    const candidates = silence?.intervals || [];
    const rate = plan.edit_plan?.profile?.frame_rate;
    const fps = rate ? rate.numerator / rate.denominator : 1;
    function silenceTime(frame) {
      const seconds = frame / fps;
      return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${(seconds % 60).toFixed(2).padStart(5, "0")}`;
    }
    if (!candidates.length) empty(silenceContainer);
    candidates.forEach((pause) => {
      const item = document.createElement("article"); item.className = "rationale-item";
      const title = document.createElement("strong");
      title.textContent = `${silenceTime(pause.start_frame)}–${silenceTime(pause.end_frame)} — ${((pause.end_frame - pause.start_frame) / fps).toFixed(2)} s`;
      const details = document.createElement("p");
      const threshold = pause.threshold_db ?? silence.settings?.threshold_db;
      details.textContent = `Threshold: ${threshold ?? "unavailable"} dBFS · Calibration confidence: ${Number.isFinite(pause.confidence) ? Math.round(pause.confidence * 100) + "%" : "unavailable"} · Suggested action: ${pause.suggestion?.action || "unavailable"}`;
      const context = document.createElement("p");
      context.textContent = Object.entries(pause.context || {}).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join(" · ") || "Speech and visual context: unavailable";
      const evidence = document.createElement("code"); evidence.textContent = pause.evidence_id || pause.id;
      item.append(title, details, context, evidence); silenceContainer.append(item);
    });
    renderDecisions(log.decisions);
    renderSimple("unsupported", log.unsupported);
    renderSimple("assumptions", log.assumptions);
    const planState = plan.plan_status === "awaiting_approval" ? "Ready for review" : plan.plan_status === "approved" ? "Approved" : plan.plan_status;
    el("iteration-status").textContent = `Iteration ${String(plan.iteration).padStart(3, "0")} · ${planState}${plan.error ? ` · ${plan.error.message}` : ""}`;
    const preview = el("preview-video");
    const showLegacyPreview = Boolean(plan.preview_url && plan.plan_status !== "awaiting_approval");
    preview.classList.toggle("hidden", !showLegacyPreview);
    if (showLegacyPreview && preview.getAttribute("src") !== plan.preview_url) {
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
    el("inspection-link").classList.toggle("hidden", !showLegacyPreview || !plan.inspection_url);
    if (plan.inspection_url) el("inspection-link").href = plan.inspection_url;
    el("iteration-download").classList.toggle("hidden", !plan.video_url);
    if (plan.video_url) el("iteration-download").href = plan.video_url;
    el("approve-plan").disabled = plan.plan_status !== "awaiting_approval" || ["planning", "analyzing", "rendering"].includes(latestEdit?.state);
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
      el("progress-bar").style.width = `${edit.progress?.percent ?? percent}%`;
      el("job-progress").setAttribute("aria-valuenow", String(edit.progress?.percent ?? percent));
      el("job-progress").setAttribute("aria-valuetext", edit.progress?.message || message);
      el("job-panel").dataset.state = edit.state;
      el("job-stage").textContent = { analyzing: "Preparing your edit", planning: "Building your plan", awaiting_approval: "Ready for your review", approved: "Plan approved", rendering: "Bringing your edit to life", completed: "Edit complete", failed: "Edit paused" }[edit.state] || "Working on your edit";
      updateWorkflow({ analyzing: 2, planning: 2, awaiting_approval: 2, approved: 3, rendering: 4, completed: 5, failed: 2 }[edit.state] ?? 2);
      const hasPlan = await loadIterations(edit);
      if (sequence !== refreshSequence) return;
      const visible = ["job-panel", ...(hasPlan ? ["plan-panel"] : [])];
      if (edit.state === "approved") visible.push("render-panel");
      if (edit.state === "completed") { await loadResult(); visible.push("result-panel", "feedback-panel"); }
      if (edit.state === "failed") { el("failure-message").textContent = edit.error?.message || message; visible.push("failure-panel"); }
      show(...visible);
      if (["analyzing", "planning", "rendering"].includes(edit.state) || edit.jobs?.some((job) => ["queued", "running"].includes(job.status))) schedulePoll(); else stopPolling();
    } catch (error) { if (sequence !== refreshSequence) return; const message = errorText(error); el("status-message").textContent = message; el("failure-message").textContent = message; show("job-panel", "failure-panel", "feedback-panel"); stopPolling(); }
  }
  function start(edit) { editId = edit.id; selectedIteration = null; followLatest = true; saveSession(); record("edit_created"); show("job-panel"); refresh(); }

  const promptExamples = ["Zoom into the player from 00:04 to 00:07", "Crop to the left side during this section", "Reframe around the speaker", "Slow this moment down to 0.6x", "Cut the first 2 seconds", "Zoom out after the shot", "Pan from the left side to the right", "Remove the section from 00:12 to 00:16"];
  promptExamples.forEach((text) => { const chip = document.createElement("button"); chip.type = "button"; chip.textContent = text; chip.addEventListener("click", () => { const input = el("instruction"); input.value = input.value.trim() ? `${input.value.trim()}\n${text}` : text; input.focus(); }); el("prompt-chips").append(chip); });

  const avatars = { camera: "◉", director: "♟", clapperboard: "▰", "film-reel": "✥", "video-frame": "▣", timeline: "▤", lens: "◎", play: "▶" };
  function renderAvatars(selected) { const grid = el("avatar-grid"); grid.replaceChildren(); Object.entries(avatars).forEach(([key, icon]) => { const button = document.createElement("button"); button.type = "button"; button.title = key.replace("-", " "); button.setAttribute("aria-label", `Choose ${button.title} avatar`); button.textContent = icon; button.classList.toggle("selected", key === selected); button.setAttribute("aria-pressed", String(key === selected)); button.addEventListener("click", async () => { button.disabled = true; try { await request("/v1/account/avatar", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ avatar_key: key }) }); currentUser.avatar_key = key; renderAvatars(key); el("current-avatar").textContent = icon; } catch (error) { actionError(error); } finally { button.disabled = false; } }); grid.append(button); }); el("current-avatar").textContent = avatars[selected] || avatars.camera; }
  function renderCredits(credits) { const unmetered = credits.mode === "unmetered"; el("credit-mode").textContent = unmetered ? "Admin · Unmetered editing" : "Credits for your next edit"; el("credit-balance").textContent = unmetered ? "API cost only" : `${credits.balance} credits`; el("nav-credit-balance").textContent = unmetered ? "Admin · API cost only" : `${credits.balance} credits`; el("top-up").classList.toggle("hidden", unmetered); const history = el("credit-history"); history.replaceChildren(); if (!credits.entries.length) { history.textContent = unmetered ? "Admin edits do not consume application credits." : "No credit activity yet."; return; } credits.entries.forEach((entry) => { const row = document.createElement("div"); row.className = "history-row"; const detail = document.createElement("div"); const reason = document.createElement("strong"); reason.textContent = entry.reason.replace("_", " "); const date = document.createElement("small"); date.textContent = new Date(entry.created_at).toLocaleDateString(); detail.append(reason, date); const amount = document.createElement("b"); amount.className = entry.amount > 0 ? "positive" : "negative"; amount.textContent = `${entry.amount > 0 ? "+" : ""}${entry.amount}`; row.append(detail, amount); history.append(row); }); }
  async function loadCredits() { const credits = await request("/v1/billing/credits"); renderCredits(credits); return credits; }
  async function openAccount() { stopPolling(); refreshSequence++; el("action-error").classList.add("hidden"); el("landing-panel").classList.add("hidden"); el("app-shell").classList.remove("hidden"); show("profile-panel"); const data = await request("/v1/account"); currentUser = data.user; el("profile-email").textContent = data.user.email; el("profile-id").textContent = data.user.id; el("profile-verification").textContent = data.user.email_verified ? "Verified" : "Unverified"; el("profile-verification").classList.toggle("verified", data.user.email_verified); renderAvatars(data.user.avatar_key); renderCredits(data.credits); }
  function statusText(value) { return String(value || "unknown").replaceAll("_", " "); }
  async function openProjects() { stopPolling(); refreshSequence++; el("action-error").classList.add("hidden"); el("landing-panel").classList.add("hidden"); el("app-shell").classList.remove("hidden"); show("projects-panel"); el("projects-loading").classList.remove("hidden"); el("projects-list").replaceChildren(); el("projects-empty").classList.add("hidden"); try { const data = await request("/v1/projects"); el("projects-empty").classList.toggle("hidden", data.projects.length > 0); data.projects.forEach((project) => { const card = document.createElement("article"); card.className = "project-card"; const head = document.createElement("div"); head.className = "project-head"; const title = document.createElement("div"); const heading = document.createElement("h2"); heading.textContent = project.filename; const date = document.createElement("p"); date.textContent = `${new Date(project.created_at).toLocaleDateString()} · ${project.credits_used} credits used`; title.append(heading, date); const reopen = document.createElement("button"); reopen.className = "button button-secondary"; reopen.textContent = "Reopen"; reopen.addEventListener("click", () => { editId = project.id; selectedIteration = null; followLatest = true; saveSession(); refresh(); }); head.append(title, reopen); const iterations = document.createElement("div"); iterations.className = "iteration-list"; project.iterations.forEach((item) => { const row = document.createElement("div"); row.className = "iteration-row"; const number = document.createElement("strong"); number.textContent = `#${item.iteration}`; const prompt = document.createElement("p"); prompt.textContent = item.instruction; const state = document.createElement("span"); state.className = "state"; state.textContent = item.has_video ? "Final" : item.preview_status === "succeeded" ? "Preview" : statusText(item.status); row.append(number, prompt, state); iterations.append(row); }); card.append(head, iterations); el("projects-list").append(card); }); } catch (error) { const message = document.createElement("p"); message.className = "form-error"; message.setAttribute("role", "alert"); message.textContent = errorText(error); const retry = document.createElement("button"); retry.className = "button button-secondary"; retry.textContent = "Try loading projects again"; retry.addEventListener("click", openProjects); el("projects-list").replaceChildren(message, retry); } finally { el("projects-loading").classList.add("hidden"); } }

  el("create-edit-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const file = el("video-file").files[0];
    const instruction = el("instruction").value.trim();
    if (!file || !instruction) return;
    busy("create-edit", true, "Uploading your video…");
    el("upload-status").textContent = "Uploading securely. Keep this page open while your video is sent.";
    el("create-error").classList.add("hidden");
    try {
      record("upload_started", { bytes: file.size });
      const form = new FormData(); form.append("file", file);
      const video = await request("/v1/videos", { method: "POST", body: form });
      record("upload_completed", { video_id: video.id });
      busy("create-edit", true, "Starting your edit…");
      el("upload-status").textContent = "Upload complete. Preparing your request…";
      const edit = await request("/v1/edits", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ video_id: video.id, instruction }) });
      start(edit);
    } catch (error) { const message = errorText(error); showCreateError(message); el("upload-status").textContent = "Your selection and instruction are saved here. Check the error, then try again."; record("create_failed", { message }); }
    finally { busy("create-edit", false); }
  });
  el("approve-plan").addEventListener("click", async () => {
    if (!activePlan) return;
    busy("approve-plan", true, "Approving and starting render…");
    updateWorkflow(3);
    el("action-error").classList.add("hidden");
    try { await request(`/v1/edits/${editId}/approve`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plan_id: activePlan.plan_id }) }); record("plan_approved", { plan_id: activePlan.plan_id }); await request(`/v1/edits/${editId}/render`, { method: "POST" }); await refresh(); }
    catch (error) { actionError(error); await refresh(); }
    finally { busy("approve-plan", false); el("approve-plan").disabled = activePlan?.plan_status !== "awaiting_approval" || ["planning", "analyzing", "rendering"].includes(latestEdit?.state); }
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
    busy("render-video", true, "Starting render…");
    try { await request(`/v1/edits/${editId}/render`, { method: "POST" }); record("render_requested"); await refresh(); }
    catch (error) { actionError(error); }
    finally { busy("render-video", false); }
  });
  el("new-edit").addEventListener("click", () => { stopPolling(); refreshSequence++; localStorage.removeItem(STORAGE_KEY); editId = null; activePlan = null; selectedIteration = null; el("action-error").classList.add("hidden"); show("start-panel"); });
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
  el("open-profile").addEventListener("click", () => { if (currentUser) openAccount().catch(actionError); });
  el("close-profile").addEventListener("click", () => {
    if (editId) refresh(); else show("start-panel");
  });
  async function authenticate(path, email, password, errorId, buttonId) {
    const error = el(errorId);
    const button = el(buttonId);
    error.classList.add("hidden");
    button.disabled = true;
    button.textContent = path.endsWith("register") ? "Creating account…" : "Signing in…";
    try {
      const payload = { email, password }; if (path.endsWith("register")) payload.captcha_token = window.turnstile?.getResponse?.() || null;
      const user = await request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      signedIn(user);
      const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null");
      openEditor();
      if (saved?.editId) { editId = saved.editId; show("job-panel"); refresh(); }
    } finally {
      button.disabled = false;
      button.textContent = path.endsWith("register") ? "Create account" : "Log in";
    }
  }
  el("login-form").addEventListener("submit", async (event) => { event.preventDefault(); try { await authenticate("/v1/auth/login", el("login-email").value, el("login-password").value, "login-error", "login-submit"); } catch (error) { showAuthError("login-error", authErrorText(error)); } });
  el("register-form").addEventListener("submit", async (event) => { event.preventDefault(); try { await authenticate("/v1/auth/register", el("register-email").value, el("register-password").value, "register-error", "register-submit"); } catch (error) { showAuthError("register-error", authErrorText(error)); } });
  function openEditor() { stopPolling(); refreshSequence++; editId = null; activePlan = null; selectedIteration = null; localStorage.removeItem(STORAGE_KEY); el("action-error").classList.add("hidden"); el("landing-panel").classList.add("hidden"); el("app-shell").classList.remove("hidden"); show("start-panel"); }
  ["nav-editor", "side-editor", "projects-new", "empty-new"].forEach((id) => el(id).addEventListener("click", openEditor));
  ["nav-projects", "side-projects"].forEach((id) => el(id).addEventListener("click", openProjects));
  ["side-account", "nav-balance"].forEach((id) => el(id).addEventListener("click", () => openAccount().catch(actionError)));
  document.querySelectorAll("[data-scroll]").forEach((button) => button.addEventListener("click", () => el(button.dataset.scroll).scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" })));
  document.querySelectorAll("[data-auth]").forEach((button) => button.addEventListener("click", () => { if (currentUser) { openEditor(); return; } el("landing-panel").classList.add("hidden"); show("auth-panel"); el("register-email").focus(); }));
  el("nav-login").addEventListener("click", () => { el("landing-panel").classList.add("hidden"); show("auth-panel"); el("login-email").focus(); });
  el("nav-signup").addEventListener("click", () => { el("landing-panel").classList.add("hidden"); show("auth-panel"); el("register-email").focus(); });
  function closeNavigation() { el("main-nav").classList.remove("open"); el("mobile-menu").setAttribute("aria-expanded", "false"); }
  el("mobile-menu").addEventListener("click", () => { const open = el("main-nav").classList.toggle("open"); el("mobile-menu").setAttribute("aria-expanded", String(open)); });
  el("main-nav").addEventListener("click", (event) => { if (event.target.closest("button")) closeNavigation(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && el("main-nav").classList.contains("open")) { closeNavigation(); el("mobile-menu").focus(); } });
  document.querySelectorAll("[data-demo]").forEach((button) => { button.setAttribute("aria-pressed", "false"); button.addEventListener("click", () => { document.querySelectorAll("[data-demo]").forEach((item) => { item.classList.toggle("active", item === button); item.setAttribute("aria-pressed", String(item === button)); }); el("demo-prompt-text").textContent = button.dataset.demo; el("example-selection").textContent = `Try this instruction: “${button.dataset.demo}”`; el("example-selection").classList.remove("hidden"); }); });
  el("toggle-examples").addEventListener("click", () => { const hidden = el("prompt-chips").classList.toggle("hidden"); el("toggle-examples").setAttribute("aria-expanded", String(!hidden)); });
  el("resend-verification").addEventListener("click", async () => { busy("resend-verification", true, "Sending…"); try { const result = await request("/v1/auth/resend-verification", { method: "POST" }); el("verification-banner").querySelector("p").textContent = result.message; } catch (error) { actionError(error); } finally { busy("resend-verification", false); } });
  el("forgot-password").addEventListener("click", async () => { const email = el("login-email").value.trim(); if (!email) { showAuthError("login-error", "Enter your email first."); return; } busy("forgot-password", true, "Sending…"); try { const result = await request("/v1/auth/forgot-password", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email }) }); showAuthError("login-error", result.message); } catch (error) { showAuthError("login-error", errorText(error)); } finally { busy("forgot-password", false); } });
  let selectedPackage = null;
  async function openTopUp() {
    selectedPackage = null;
    el("mock-success").disabled = true; el("mock-failure").disabled = true;
    el("order-summary").textContent = "Choose a package.";
    el("topup-status").textContent = "Loading credit packages…";
    el("package-list").replaceChildren(); el("topup-dialog").showModal();
    try {
      const data = await request("/v1/billing/packages");
      data.packages.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button"; button.setAttribute("aria-pressed", "false");
        const name = document.createElement("b"); name.textContent = item.name;
        const value = document.createElement("span"); value.textContent = `${item.credits} credits · $${(item.price_minor / 100).toFixed(2)}`;
        button.append(name, value);
        button.addEventListener("click", () => {
          selectedPackage = item;
          el("package-list").querySelectorAll("button").forEach((node) => { node.classList.toggle("selected", node === button); node.setAttribute("aria-pressed", String(node === button)); });
          el("order-summary").textContent = `${item.name}: ${item.credits} credits for $${(item.price_minor / 100).toFixed(2)}`;
          el("mock-success").disabled = false; el("mock-failure").disabled = false;
        });
        el("package-list").append(button);
      });
      el("topup-status").textContent = data.packages.length ? "" : "No packages are available right now. Close this window and try again later.";
    } catch (error) { el("topup-status").textContent = errorText(error) + " Close this window and try again."; }
  }
  async function submitTopUp(simulate) {
    if (!selectedPackage || el("mock-success").disabled) return;
    const packageKey = selectedPackage.key;
    busy("mock-success", true, "Processing…"); el("mock-failure").disabled = true;
    el("package-list").querySelectorAll("button").forEach((button) => { button.disabled = true; });
    try {
      const data = await request("/v1/billing/mock-top-ups", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ package_key: packageKey, simulate }) });
      renderCredits(data.credits);
      el("topup-status").textContent = data.order.status === "succeeded" ? "Credits added successfully." : "The simulated payment failed. No credits were added. You can try again.";
    } catch (error) { el("topup-status").textContent = errorText(error); }
    finally { busy("mock-success", false); el("mock-failure").disabled = false; el("package-list").querySelectorAll("button").forEach((button) => { button.disabled = false; }); }
  }
  el("top-up").addEventListener("click", () => openTopUp().catch(actionError)); el("close-topup").addEventListener("click", () => el("topup-dialog").close()); el("mock-success").addEventListener("click", () => submitTopUp("success")); el("mock-failure").addEventListener("click", () => submitTopUp("failure"));
  el("retry-status").addEventListener("click", () => { busy("retry-status", true, "Checking…"); refresh().finally(() => busy("retry-status", false)); });
  el("failure-new").addEventListener("click", openEditor);
  const query = new URLSearchParams(location.search); if (query.get("verify")) request("/v1/auth/verify-email", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ token: query.get("verify") }) }).then(() => history.replaceState({}, "", "/")).catch(() => {});
  request("/v1/public-config").then((config) => { if (!config.turnstile_site_key) return; const script = document.createElement("script"); script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit"; script.async = true; script.onload = () => window.turnstile.render("#turnstile-slot", { sitekey: config.turnstile_site_key }); document.head.append(script); }).catch(() => {});
  el("logout").addEventListener("click", async () => { await fetch("/v1/auth/logout", { method: "POST" }); stopPolling(); localStorage.removeItem(STORAGE_KEY); editId = null; currentUser = null; el("app-shell").classList.add("hidden"); el("landing-panel").classList.remove("hidden"); document.querySelectorAll(".public-nav").forEach((node) => node.classList.remove("hidden")); document.querySelectorAll(".private-nav").forEach((node) => node.classList.add("hidden")); ["user-email", "logout", "export-feedback"].forEach((id) => el(id).classList.add("hidden")); show(); });
  request("/v1/auth/me").then(signedIn).then(() => { const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "null"); if (saved?.editId) { editId = saved.editId; show("job-panel"); refresh(); } }).catch(() => { el("landing-panel").classList.remove("hidden"); show(); });
})();
