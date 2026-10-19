(() => {
  let activeModel = null;
  const USER_ID = "local-user";
  const API = "/api/v1";
  const STORAGE_KEY = "formwise-chat-v1";
  const $ = (id) => document.getElementById(id);
  const state = {
    sessionId: null,
    sessionTitle: "新建计算",
    messages: [],
    pendingConfirmation: false,
    currentParameters: {},
    busy: false,
    jobId: null,
    pollTimer: null,
    conversations: [],
    previewUrl: null,
  };
  let defaultContextPanelHTML = "";

  const escapeHTML = (value) => String(value).replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);

  // 轮询必须有界：任务卡住或服务不可用时，界面不能无限转圈。
  const POLL_INTERVAL_MS = 1800;
  const POLL_FAILURE_INTERVAL_MS = 3000;
  const POLL_MAX_MS = 15 * 60 * 1000;
  const POLL_MAX_FAILURES = 5;

  // 已知任务错误码 -> 面向用户的排查提示。后端仍会在 error.message 里给出完整原因。
  const JOB_ERROR_HINTS = {
    EXCEL_COM_ACTIVATION_FAILED: "Excel 无法启动（COM 激活失败）。请让管理员检查 Office 授权状态与 DCOM 配置——Worker 可能需要以管理员身份运行。",
    EXCEL_COM_ACCESS_DENIED: "当前账户没有权限启动 Excel。请让管理员检查 DCOM 的启动/激活权限与访问权限。",
    EXCEL_COM_SERVER_FAULT: "Excel 启动时异常退出。请手动打开一次 Excel 确认它可用，并检查加载项。",
    EXCEL_RPC_UNAVAILABLE: "无法连接 Excel 服务，请确认没有残留的 EXCEL.EXE 进程后重试。",
    WORKER_LEASE_EXPIRED: "任务长时间没有进展，已被回收。请确认 Excel Worker 正在运行。",
    INTERACTIVE_SESSION_REQUIRED: "Worker 必须在已登录的 Windows 桌面会话中运行。",
  };

  // 参数值的来源标签：让用户在确认前一眼看出哪些是自己这次说的。
  const PARAMETER_SOURCE_LABELS = {
    input: "本轮",
    session: "沿用上次",
    preference: "偏好",
    default: "默认值",
  };

  let pollJobId = null;
  let pollStartedAt = 0;
  let pollFailures = 0;

  function formatDuration(milliseconds) {
    const totalSeconds = Math.max(0, Math.round(milliseconds / 1000));
    if (totalSeconds < 60) return `${totalSeconds} 秒`;
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return seconds ? `${minutes} 分 ${seconds} 秒` : `${minutes} 分`;
  }

  function saveState() {
    try {
      const all = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      all.conversations = state.conversations;
      if (state.sessionId) {
        all.activeSessionId = state.sessionId;
        all.conversations[state.sessionId] = {
          title: state.sessionTitle,
          messages: state.messages,
          pendingConfirmation: state.pendingConfirmation,
          currentParameters: state.currentParameters,
          jobId: state.jobId,
          updatedAt: Date.now(),
        };
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify(all));
    } catch (_) { /* Private browsing/storage quota: chat remains usable in memory. */ }
  }

  function loadState() {
    try {
      const all = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
      state.conversations = all.conversations || {};
      const id = all.activeSessionId;
      if (id && state.conversations[id]) {
        const saved = state.conversations[id];
        state.sessionId = id;
        state.sessionTitle = saved.title || "计算对话";
        state.messages = Array.isArray(saved.messages) ? saved.messages : [];
        state.pendingConfirmation = Boolean(saved.pendingConfirmation);
        state.currentParameters = saved.currentParameters || {};
        state.jobId = saved.jobId || null;
        return true;
      }
    } catch (_) { /* Ignore invalid cached state. */ }
    return false;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: {
        "X-User-ID": USER_ID,
        ...(options.body ? { "Content-Type": "application/json; charset=utf-8" } : {}),
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    if (!response.ok) {
      let message = `请求失败 (${response.status})`;
      try {
        const error = contentType.includes("json") ? await response.json() : { detail: await response.text() };
        message = error.detail || error.message || message;
      } catch (_) { /* Use fallback message. */ }
      throw new Error(message);
    }
    if (contentType.includes("application/json")) return response.json();
    return response;
  }

  async function ensureSession() {
    if (state.sessionId) return;
    if (!activeModel) await refreshModelDefinition();
    const session = await api(`${API}/sessions`, {
      method: "POST",
      body: JSON.stringify({}),
    });
    state.sessionId = session.session_id;
    state.sessionTitle = "新建计算";
    state.messages = [];
    state.pendingConfirmation = false;
    state.jobId = null;
    saveState();
  }

  async function refreshModelDefinition(modelId = null) {
    const models = await api(`${API}/models`);
    if (!Array.isArray(models) || !models.length) throw new Error("当前没有可用计算模型");
    // 右侧面板必须跟着**会话实际绑定的模型**走。否则路由切到独立模型后，
    // 面板还显示串联模型的两步流程和四个参数，用户会以为根本没切换成功。
    const wanted = modelId || (activeModel && activeModel.model_id) || null;
    activeModel = models.find((model) => model.model_id === wanted) || models[0];
    renderModelParameters();
    renderModelExamples();
  }

  const SUGGESTION_ICON_CLASSES = ["flame", "sliders", "grid"];
  const SUGGESTION_ICON_GLYPHS = ["◉", "⌁", "▦"];

  // 引导示例完全来自模型定义（/api/v1/models 的 examples / hints），
  // 前端不硬编码任何业务文案：换模型或加参数不需要改这里。
  function renderModelExamples() {
    const grid = $("suggestionGrid");
    const chips = $("exampleChips");
    const caption = $("exampleCaption");
    if (!grid || !chips) return;
    const examples = (activeModel && activeModel.examples) || [];
    const hints = (activeModel && activeModel.hints) || [];
    grid.innerHTML = examples.map((example, index) => {
      const iconIndex = index % SUGGESTION_ICON_CLASSES.length;
      return `<button class="suggestion-card" type="button" data-prompt="${escapeHTML(example.prompt)}"><span class="suggestion-icon ${SUGGESTION_ICON_CLASSES[iconIndex]}">${SUGGESTION_ICON_GLYPHS[iconIndex]}</span><span><strong>${escapeHTML(example.title)}</strong><small>${escapeHTML(example.description || "")}</small></span><span class="suggestion-arrow">↗</span></button>`;
    }).join("");
    chips.innerHTML = hints.map((hint) => `<span>${escapeHTML(hint)}</span>`).join("");
    grid.classList.toggle("hidden", examples.length === 0);
    chips.classList.toggle("hidden", hints.length === 0);
    if (caption) caption.classList.toggle("hidden", hints.length === 0);
    const input = $("messageInput");
    if (input) {
      input.placeholder = hints.length
        ? `描述你的计算条件… 例如：${hints.join("，")}`
        : "描述你的计算条件…";
    }
  }

  function renderModelParameters() {
    if (!activeModel) return;
    const container = $("activeParameters");
    if (!container) return;
    const entries = Object.entries(activeModel.parameters || {});
    container.innerHTML = entries.map(([key, spec]) => {
      const value = state.currentParameters[key];
      const hasValue = value !== undefined && value !== null;
      // 控制参数（例如“是否继续空气预热器计算”）是布尔开关，不写入单元格，
      // 显示成「是/否」而不是「模板默认」，否则用户会以为它对应某个 Excel 格。
      const isControl = Boolean(spec.control);
      const label = (input) => (typeof input === "boolean" ? (input ? "是" : "否") : input);
      const shownValue = hasValue
        ? escapeHTML(label(value))
        : (isControl ? "按默认" : (spec.required ? "待输入" : "模板默认"));
      const valueClass = hasValue ? "has-value" : (spec.required ? "missing-value" : "template-default");
      return `<div data-input-parameter="${escapeHTML(key)}"><span class="parameter-dot"></span><span class="input-label" title="${escapeHTML(spec.description || spec.display_name || key)}">${escapeHTML(spec.display_name || key)}</span><span class="input-value ${valueClass}" data-input-value="${escapeHTML(key)}">${shownValue}</span><code>${escapeHTML(spec.unit || "")}</code></div>`;
    }).join("");
  }

  function showToast(message) {
    const toast = $("toast");
    toast.textContent = message;
    toast.classList.add("show");
    window.clearTimeout(showToast.timer);
    showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 3000);
  }

  function setBusy(busy, typing = false) {
    state.busy = busy;
    $("sendButton").disabled = busy;
    $("messageInput").disabled = busy;
    $("typingRow").classList.toggle("hidden", !typing);
  }

  function addMessage(role, text, extra = {}) {
    state.messages.push({ role, text, ...extra, createdAt: Date.now() });
    if (role === "user" && state.sessionTitle === "新建计算") {
      state.sessionTitle = text.length > 22 ? `${text.slice(0, 22)}…` : text;
    }
    saveState();
    render();
  }

  function renderMessage(message) {
    if (message.role === "user") {
      return `<div class="message-row user"><div class="message-content">${escapeHTML(message.text)}</div></div>`;
    }
    let content = `<div class="message-content"><div class="message-meta">Formwise · ${message.createdAt ? new Date(message.createdAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "刚刚"}</div><p class="message-text">${escapeHTML(message.text).replace(/\n/g, "<br>")}</p>`;
    if (message.confirmation && !message.confirmation.confirmed) {
      const rows = message.confirmation.parameters.map((item) => {
        // 标出每个值的来源：不标的话，用户会对一个自己本轮根本没提过的旧值点「确认」。
        const label = PARAMETER_SOURCE_LABELS[item.source] || "";
        const badge = label
          ? `<span class="parameter-source source-${escapeHTML(item.source || "unknown")}">${label}</span>`
          : "";
        return `<div class="parameter-row"><span class="parameter-name">${escapeHTML(item.description || item.key)}${badge}</span><span class="parameter-value">${escapeHTML(item.value)}</span><span class="parameter-unit">${escapeHTML(item.unit || "")}</span></div>`;
      }).join("");
      content += `<div class="confirmation-card"><div class="card-head"><strong>请核对计算参数</strong><span class="review-badge">需要确认</span></div>${rows}<div class="confirm-actions"><button class="primary-button" data-action="confirm">确认并开始计算</button><button class="secondary-button" data-action="edit">继续修改</button></div></div>`;
    }
    if (message.job) content += renderJob(message.job);
    content += "</div>";
    return `<div class="message-row assistant"><div class="assistant-mark">✳</div>${content}</div>`;
  }

  function renderJob(job) {
    const statusMap = {
      QUEUED: ["排队中", ""], RUNNING: ["计算中", "running"],
      SUCCEEDED: ["已完成", "success"], FAILED: ["执行失败", "failed"],
      CANCELLED: ["已取消", ""],
    };
    const [label, style] = statusMap[job.status] || [job.status || "处理中", "running"];
    const files = (job.artifacts || []).map((artifact) => {
      const isPdf = artifact.kind === "pdf";
      const canPreview = isPdf || Boolean(artifact.preview_path);
      const tag = isPdf ? "PDF" : "DOC";
      const name = escapeHTML(artifact.file_name);
      const download = `<a class="artifact-download" href="${escapeHTML(artifact.download_path)}" download aria-label="下载 ${name}" title="下载文件">↓</a>`;
      if (canPreview) {
        const previewName = escapeHTML(artifact.preview_name || artifact.file_name);
        return `<div class="artifact-link pdf-artifact"><button class="artifact-preview-button" data-preview-url="${escapeHTML(artifact.preview_path || artifact.download_path)}" data-preview-name="${previewName}" data-download-url="${escapeHTML(artifact.download_path)}" data-download-name="${name}" data-download-kind="${escapeHTML(artifact.kind)}" title="在右侧预览"><span class="artifact-type">${tag}</span><span class="artifact-name">${name}</span><span class="preview-hint">预览</span></button>${download}</div>`;
      }
      return `<div class="artifact-link"><span class="artifact-type">${tag}</span><span class="artifact-name">${name}</span>${download}</div>`;
    }).join("");
    const hint = job.error ? JOB_ERROR_HINTS[job.error.code] : "";
    const error = job.error
      ? `<p class="message-text error-text">${escapeHTML(job.error.message || "任务执行失败")}</p>`
        + (hint ? `<p class="message-text error-hint">${escapeHTML(hint)}</p>` : "")
      : "";
    const pending = job.status === "QUEUED" || job.status === "RUNNING";
    const elapsed = Number(job.elapsedMs) || 0;
    const elapsedText = pending && elapsed >= 1000 ? `已等待 ${formatDuration(elapsed)}` : "";
    const progress = pending ? `<div class="progress-track"><div class="progress-bar"></div></div>` : "";
    const stale = pending && elapsedText
      ? `<div class="job-stale"><span>${job.stale ? "已停止自动刷新" : elapsedText}</span>${job.stale ? `<button class="secondary-button" data-refresh-job="${escapeHTML(job.job_id || "")}">刷新状态</button>` : ""}</div>`
      : "";
    return `<div class="job-card"><div class="job-heading"><strong>Excel 计算任务</strong><span class="job-status ${style}">${label}</span></div><div class="job-id">${escapeHTML(job.job_id || "")}</div>${progress}${stale}${error}${files ? `<div class="artifact-list">${files}</div>` : ""}</div>`;
  }

  function updateWorkflow() {
    const currentMessage = [...state.messages].reverse().find((item) => item.job);
    const job = currentMessage?.job;
    const flowContainer = $("workflowSteps");
    const configuredSteps = activeModel?.workflow?.steps || [];
    const artifactDefinitions = activeModel?.artifacts || {};
    const workflow = [
      { id: "parameters", label: "参数提取与确认", kind: "parameters" },
      ...configuredSteps.map((step) => ({
        id: step.id,
        label: artifactDefinitions[step.artifact]?.display_name || step.artifact,
        kind: "artifact",
        artifact: step.artifact,
        type: step.type,
        // 带 when 条件的步骤是可跳过的（例如“不用做空气预热器”）
        optional: Boolean(step.when),
      })),
      { id: "report_generation", label: "结果与计算报告", kind: "report" },
    ];

    if (flowContainer) {
      flowContainer.innerHTML = workflow.map((step, index) => {
        const hint = step.kind === "parameters" ? "自然语言 · AI" :
          step.kind === "report" ? "PDF · Word 报告" :
            (step.optional ? `可选 · ${step.type.replaceAll("_", " ")}` : `${step.type.replaceAll("_", " ")}`);
        const connector = index < workflow.length - 1 ? '<div class="flow-connector"></div>' : "";
        return `<div class="flow-step" data-workflow-step="${escapeHTML(step.id)}" data-step-kind="${step.kind}" data-step-artifact="${escapeHTML(step.artifact || "")}"><span class="flow-number">${index + 1}</span><div><strong>${escapeHTML(step.label)}</strong><small data-step-caption>${escapeHTML(hint)}</small></div><span class="flow-check"></span></div>${connector}`;
      }).join("");
    }

    let activeIndex = -1;
    let failedIndex = -1;
    if (job) {
      if (job.status === "SUCCEEDED") {
        activeIndex = workflow.length;
      } else if (job.status === "QUEUED") {
        activeIndex = workflow.length > 1 ? 1 : 0;
      } else {
        const stage = job.stage || "";
        if (stage === "report_generation") {
          activeIndex = workflow.length - 1;
        } else {
          const matched = configuredSteps.findIndex((step) => stage.startsWith(`${step.id}_`));
          activeIndex = matched >= 0 ? matched + 1 : (workflow.length > 1 ? 1 : 0);
        }
      }
      if (job.status === "FAILED") failedIndex = Math.min(activeIndex, workflow.length - 1);
    } else if (state.pendingConfirmation || state.messages.some((message) => message.role === "user")) {
      activeIndex = 0;
    }

    workflow.forEach((step, index) => {
      const element = flowContainer?.querySelector(`[data-workflow-step="${CSS.escape(step.id)}"]`);
      if (!element) return;
      const number = element.querySelector(".flow-number");
      const check = element.querySelector(".flow-check");
      const caption = element.querySelector("[data-step-caption]");
      element.classList.remove("is-active", "is-done", "is-failed");
      if (!job) {
        if (index === 0 && activeIndex === 0 && caption) {
          caption.textContent = state.pendingConfirmation ? "等待你确认参数" : "正在提取参数";
        }
        number.textContent = String(index + 1);
        check.textContent = "";
        return;
      }
      if (index < activeIndex || job.status === "SUCCEEDED") {
        element.classList.add("is-done");
        number.textContent = "✓";
        check.textContent = "";
        if (caption) caption.textContent = step.kind === "parameters" ? "参数已确认" :
          step.kind === "report" ? "PDF 与 Word 已就绪" : "计算与 PDF 完成";
      } else if (index === failedIndex && job.status === "FAILED") {
        element.classList.add("is-failed");
        number.textContent = "!";
        check.textContent = "失败";
        if (caption) caption.textContent = job.error?.code || "执行失败";
      } else if (index === activeIndex && job.status !== "SUCCEEDED") {
        element.classList.add("is-active");
        number.textContent = String(index + 1);
        check.textContent = job.status === "QUEUED" ? "排队" : "进行中";
        if (caption) {
          if (step.kind === "parameters") caption.textContent = "参数已确认";
          else if (step.kind === "report") caption.textContent = "正在生成说明与 Word 报告";
          else if (job.status === "QUEUED") caption.textContent = "排队等待 Worker";
          else if (job.stage.includes("pdf_export")) caption.textContent = "正在导出多页 PDF";
          else caption.textContent = `正在执行 ${step.label}`;
        }
      } else {
        number.textContent = String(index + 1);
        check.textContent = "";
      }
    });
    renderModelParameters();
  }

  function render() {
    const hasMessages = state.messages.length > 0;
    $("welcome").classList.toggle("hidden", hasMessages);
    const list = $("messageList");
    list.innerHTML = state.messages.map(renderMessage).join("");
    updateWorkflow();
    $("recentList").innerHTML = Object.entries(state.conversations)
      .sort((a, b) => (b[1].updatedAt || 0) - (a[1].updatedAt || 0))
      .slice(0, 8)
      .map(([id, item]) => `<button class="recent-item ${id === state.sessionId ? "selected" : ""}" data-session="${escapeHTML(id)}"><span>◷</span><span>${escapeHTML(item.title || "计算对话")}</span></button>`)
      .join("") || `<div class="recent-empty">还没有对话记录</div>`;
    list.querySelectorAll('[data-action="confirm"]').forEach((button) => button.addEventListener("click", confirmCurrent));
    list.querySelectorAll('[data-action="edit"]').forEach((button) => button.addEventListener("click", () => {
      $("messageInput").focus();
      showToast("继续输入要修改的计算参数");
    }));
    list.querySelectorAll("[data-preview-url]").forEach((button) => button.addEventListener("click", () => {
      openPdfPreview(
        button.dataset.previewUrl,
        button.dataset.previewName,
        button.dataset.downloadUrl,
        button.dataset.downloadName,
        button.dataset.downloadKind,
      );
    }));
    $("recentList").querySelectorAll("[data-session]").forEach((button) => button.addEventListener("click", () => switchSession(button.dataset.session)));
    list.querySelectorAll("[data-refresh-job]").forEach((button) => button.addEventListener("click", () => {
      // 手动刷新：重置计时与失败计数，重新开始一轮有界轮询。
      pollStartedAt = Date.now();
      pollFailures = 0;
      pollJob(button.dataset.refreshJob);
    }));
    requestAnimationFrame(() => { $("conversation").scrollTop = $("conversation").scrollHeight; });
  }

  function closePdfPreview() {
    if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
    state.previewUrl = null;
    $("contextPanel").classList.remove("is-preview");
    $("chatLayout").classList.remove("preview-open");
    $("contextPanel").innerHTML = defaultContextPanelHTML;
  }

  async function openPdfPreview(path, previewName, downloadUrl, downloadName, downloadKind) {
    try {
      const response = await fetch(path, { headers: { "X-User-ID": USER_ID } });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.detail || `预览失败 (${response.status})`);
      }
      const blob = await response.blob();
      if (state.previewUrl) URL.revokeObjectURL(state.previewUrl);
      state.previewUrl = URL.createObjectURL(new Blob([blob], { type: "application/pdf" }));
      const panel = $("contextPanel");
      panel.classList.add("is-preview");
      $("chatLayout").classList.add("preview-open");
      const downloadLabel = downloadKind === "word" ? "下载 Word" : "下载 PDF";
      panel.innerHTML = `<div class="pdf-preview-header"><div class="pdf-preview-title"><span class="artifact-type">PDF</span><div><strong>${escapeHTML(previewName)}</strong><small>报告预览 · 原始文件仍可单独下载</small></div></div><div class="pdf-preview-actions"><a class="preview-download" href="${escapeHTML(downloadUrl)}" download="${escapeHTML(downloadName)}">${downloadLabel} ↓</a><button class="icon-button preview-close" aria-label="关闭预览" title="关闭预览">×</button></div></div><iframe class="pdf-frame" src="${state.previewUrl}#toolbar=1&navpanes=0&view=FitH" title="${escapeHTML(previewName)} 预览"></iframe>`;
      panel.querySelector(".preview-close").addEventListener("click", closePdfPreview);
    } catch (error) {
      showToast(`无法预览 PDF：${error.message}`);
    }
  }

  function makeAssistantText(draft) {
    if (draft.needs_confirmation) {
      // 只有本轮真的抽到新值时才说「根据你的描述整理」；否则用户会对一个
      // 自己根本没提过的旧值直接点确认。运行几本工作簿也由模型定义决定，
      // 不能写死「两本」。
      const sources = draft.parameter_sources || {};
      const hasInput = Object.values(sources).some((source) => source === "input");
      if (hasInput) {
        return "我已从你的描述里提取到新的参数值。请核对下面标着「本轮」的数值和单位；标着「沿用上次」的是这个会话之前已确认的值，不需要请点「继续修改」。确认后我会在后台运行 Excel 并生成 PDF 和 Word 报告。";
      }
      return "本轮我没有从你的描述里识别出新的参数值。下面是这个会话当前生效的参数——标着「沿用上次」的**不是**你这次说的，请确认无误再继续，或点「继续修改」。";
    }
    return draft.clarification || `还需要补充这些必填参数：${(draft.missing_required || []).join("、")}。`;
  }

  async function sendMessage(text) {
    const message = text.trim();
    if (!message || state.busy) return;
    $("messageInput").value = "";
    resizeInput();
    addMessage("user", message);
    setBusy(true, true);
    try {
      await refreshModelDefinition();
      await ensureSession();
      const draft = await api(`${API}/sessions/${encodeURIComponent(state.sessionId)}/messages`, {
        method: "POST",
        body: JSON.stringify({ message }),
      });
      state.currentParameters = draft.parameters || {};
      // 这句话可能刚刚把会话路由到另一个模型：面板立刻跟上
      if (draft.model_id && (!activeModel || activeModel.model_id !== draft.model_id)) {
        await refreshModelDefinition(draft.model_id);
      }
      state.pendingConfirmation = Boolean(draft.needs_confirmation);
      const assistant = { role: "assistant", text: makeAssistantText(draft), createdAt: Date.now() };
      if (draft.needs_confirmation) assistant.confirmation = { parameters: draft.confirmation_summary || [], confirmed: false };
      addMessage("assistant", assistant.text, { confirmation: assistant.confirmation });
      if (!draft.needs_confirmation) state.pendingConfirmation = false;
    } catch (error) {
      state.pendingConfirmation = false;
      addMessage("assistant", `暂时无法处理这条消息：${error.message}\n请检查 API 服务和 DeepSeek 配置后重试。`);
    } finally {
      setBusy(false, false);
      $("messageInput").focus();
    }
  }

  async function confirmCurrent() {
    if (!state.sessionId || !state.pendingConfirmation || state.busy) return;
    setBusy(true, true);
    try {
      const job = await api(`${API}/sessions/${encodeURIComponent(state.sessionId)}/confirm`, { method: "POST" });
      state.pendingConfirmation = false;
      state.jobId = job.job_id;
      const confirmation = [...state.messages].reverse().find((item) => item.confirmation && !item.confirmation.confirmed);
      if (confirmation) confirmation.confirmation.confirmed = true;
      addMessage("assistant", "已确认参数，计算任务已提交。我会等待 Windows Excel Worker 完成两本工作簿的计算。", { job });
      pollJob(job.job_id);
    } catch (error) {
      addMessage("assistant", `提交计算失败：${error.message}`);
    } finally {
      setBusy(false, false);
    }
  }

  function jobMessage(jobId) {
    return [...state.messages].reverse().find((item) => item.job && item.job.job_id === jobId);
  }

  function markJobStale(jobId) {
    const message = jobMessage(jobId);
    if (message) {
      message.job.stale = true;
      message.job.elapsedMs = Date.now() - pollStartedAt;
    }
    saveState();
    render();
  }

  async function pollJob(jobId) {
    window.clearTimeout(state.pollTimer);
    if (pollJobId !== jobId) {
      pollJobId = jobId;
      pollStartedAt = Date.now();
      pollFailures = 0;
    }
    const elapsed = Date.now() - pollStartedAt;
    try {
      const job = await api(`${API}/jobs/${encodeURIComponent(jobId)}`);
      pollFailures = 0;
      const message = jobMessage(jobId);
      if (message) {
        message.job = job;
        message.job.elapsedMs = elapsed;
        message.job.stale = false;
      }
      saveState();
      render();
      if (job.status === "QUEUED" || job.status === "RUNNING") {
        if (elapsed >= POLL_MAX_MS) {
          // 到达上限就停手，把「还在跑」交给用户决定，而不是无限转圈。
          markJobStale(jobId);
          showToast(`任务仍在执行（已等待 ${formatDuration(elapsed)}），已停止自动刷新，可点击「刷新状态」`);
          return;
        }
        state.pollTimer = window.setTimeout(() => pollJob(jobId), POLL_INTERVAL_MS);
      } else if (job.status === "SUCCEEDED") {
        showToast("计算完成，PDF/Word 报告预览及文件下载已就绪");
      } else if (job.status === "FAILED") {
        showToast(job.error?.message || "计算任务执行失败");
      }
    } catch (error) {
      // 服务不可用时也不能无限重试：连续失败到上限就停下并告诉用户。
      pollFailures += 1;
      if (pollFailures >= POLL_MAX_FAILURES) {
        markJobStale(jobId);
        showToast(`连续 ${pollFailures} 次无法获取任务状态，已停止自动刷新：${error.message}`);
        return;
      }
      state.pollTimer = window.setTimeout(() => pollJob(jobId), POLL_FAILURE_INTERVAL_MS);
    }
  }

  async function newChat() {
    window.clearTimeout(state.pollTimer);
    state.sessionId = null;
    state.sessionTitle = "新建计算";
    state.messages = [];
    state.pendingConfirmation = false;
    state.currentParameters = {};
    state.jobId = null;
    closePdfPreview();
    saveState();
    render();
    try { await ensureSession(); } catch (error) { showToast(`创建会话失败：${error.message}`); }
    $("messageInput").focus();
    $("sidebar").classList.remove("open");
  }

  function switchSession(id) {
    const saved = state.conversations[id];
    if (!saved) return;
    window.clearTimeout(state.pollTimer);
    closePdfPreview();
    state.sessionId = id;
    state.sessionTitle = saved.title || "计算对话";
    state.messages = saved.messages || [];
    state.pendingConfirmation = Boolean(saved.pendingConfirmation);
    state.currentParameters = saved.currentParameters || {};
    state.jobId = saved.jobId || null;
    render();
    if (state.jobId) {
      const lastJob = [...state.messages].reverse().find((item) => item.job?.job_id === state.jobId);
      if (lastJob?.job?.status === "QUEUED" || lastJob?.job?.status === "RUNNING") pollJob(state.jobId);
    }
    $("sidebar").classList.remove("open");
  }

  function resizeInput() {
    const input = $("messageInput");
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 140)}px`;
  }

  function bindEvents() {
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      sendMessage($("messageInput").value);
    });
    $("messageInput").addEventListener("input", resizeInput);
    $("messageInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        $("composer").requestSubmit();
      }
    });
    // 示例卡片是渲染出来的，用事件委托绑定，重渲染后依然有效。
    const welcome = $("welcome");
    if (welcome) {
      welcome.addEventListener("click", (event) => {
        const button = event.target.closest("[data-prompt]");
        if (!button) return;
        $("messageInput").value = button.dataset.prompt;
        resizeInput();
        $("messageInput").focus();
      });
    }
    $("newChat").addEventListener("click", newChat);
    $("openSidebar").addEventListener("click", () => $("sidebar").classList.add("open"));
    $("closeSidebar").addEventListener("click", () => $("sidebar").classList.remove("open"));
    $("helpButton").addEventListener("click", () => $("helpModal").classList.remove("hidden"));
    $("closeHelp").addEventListener("click", () => $("helpModal").classList.add("hidden"));
    $("gotIt").addEventListener("click", () => $("helpModal").classList.add("hidden"));
    $("helpModal").addEventListener("click", (event) => {
      if (event.target === $("helpModal")) $("helpModal").classList.add("hidden");
    });
  }

  async function init() {
    defaultContextPanelHTML = $("contextPanel").innerHTML;
    bindEvents();
    loadState();
    try {
      await refreshModelDefinition();
    } catch (error) {
      showToast(`读取模型配置失败：${error.message}`);
    }
    render();
    if (!state.sessionId) {
      try { await ensureSession(); } catch (error) { showToast(`连接 API 失败：${error.message}`); }
    }
    if (state.jobId) {
      const lastJob = [...state.messages].reverse().find((item) => item.job?.job_id === state.jobId);
      if (lastJob?.job?.status === "QUEUED" || lastJob?.job?.status === "RUNNING") pollJob(state.jobId);
    }
  }

  init();
})();
