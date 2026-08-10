const flash = document.getElementById("flash");
let rememberedProxy = "";
let currentState = {};
let availableChannels = [];

function show(id) {
  document.getElementById(id).hidden = false;
}

function hide(id) {
  document.getElementById(id).hidden = true;
}

function message(text, kind = "info") {
  flash.hidden = false;
  flash.className = `flash ${kind}`;
  flash.textContent = text;
  flash.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function jsonRequest(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => item.msg).join("；")
      : body.detail;
    throw new Error(detail || "请求失败");
  }
  return body;
}

function postJson(url, payload, method = "POST") {
  return jsonRequest(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function setBusy(form, busy) {
  const button = form.querySelector("button[type='submit']");
  button.disabled = busy;
  button.textContent = busy ? "处理中…" : button.dataset.label;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;"
  })[character]);
}

document.querySelectorAll("form button[type='submit']").forEach((button) => {
  button.dataset.label = button.textContent;
});

function channelPayload() {
  const advertisementKeywords = document.getElementById("channel-ad-keywords").value
    .split(/[,\n，]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  return {
    channel_name: document.getElementById("channel-name").value.trim(),
    chat_id: Number(document.getElementById("chat-id").value),
    start_message_id: Number(document.getElementById("start-message-id").value || 0),
    start_mode: document.getElementById("channel-start-mode").value,
    start_date: document.getElementById("channel-start-date").value || null,
    enabled: true,
    grouping_mode: document.getElementById("channel-grouping-mode").value,
    marker_text: document.getElementById("channel-marker-text").value.trim() || "1",
    advertisement_policy: document.getElementById("channel-ad-policy").value,
    advertisement_keywords: advertisementKeywords,
    display_spacing_hours: Number(document.getElementById("channel-spacing-hours").value),
    timeline_mode: document.getElementById("channel-timeline-mode").value,
    display_order: Number(document.getElementById("channel-display-order").value),
    max_concurrent_downloads: Number(
      document.getElementById("channel-max-concurrent").value
    ),
  };
}

function editChannel(chatId) {
  const channel = (currentState.channels || []).find((item) => item.chat_id === chatId);
  if (!channel) return;
  document.getElementById("channel-name").value = channel.name;
  document.getElementById("chat-id").value = channel.chat_id;
  document.getElementById("start-message-id").value = channel.start_message_id;
  document.getElementById("channel-start-mode").value =
    channel.start_mode || "message_id";
  document.getElementById("channel-start-date").value = channel.start_date || "";
  updateStartVisibility();
  document.getElementById("channel-grouping-mode").value = channel.grouping_mode;
  document.getElementById("channel-marker-text").value = channel.marker_text || "1";
  document.getElementById("channel-ad-policy").value = channel.advertisement_policy;
  document.getElementById("channel-ad-keywords").value =
    (channel.advertisement_keywords || []).join(", ");
  document.getElementById("channel-spacing-hours").value = channel.display_spacing_hours;
  document.getElementById("channel-timeline-mode").value = channel.timeline_mode || "album";
  updateSpacingVisibility();
  document.getElementById("channel-display-order").value = channel.display_order || 0;
  document.getElementById("channel-max-concurrent").value =
    channel.max_concurrent_downloads || 5;
  document.getElementById("channel-step").scrollIntoView({ behavior: "smooth" });
}

function updateStartVisibility() {
  const mode = document.getElementById("channel-start-mode").value;
  document.getElementById("channel-start-date-field").hidden = mode !== "date";
  document.getElementById("channel-start-message-field").hidden =
    mode !== "message_id";
  document.getElementById("channel-start-date").required = mode === "date";
}

document.getElementById("channel-start-mode")
  .addEventListener("change", updateStartVisibility);
updateStartVisibility();

function resetChannelForm() {
  document.getElementById("available-channel").value = "";
  document.getElementById("channel-name").value = "";
  document.getElementById("chat-id").value = "";
  document.getElementById("channel-start-mode").value = "now";
  document.getElementById("channel-start-date").value = "";
  document.getElementById("start-message-id").value = "0";
  document.getElementById("channel-display-order").value = String(
    (currentState.channels || []).length * 10,
  );
  updateStartVisibility();
  document.getElementById("channel-name").focus();
}

function renderAvailableChannels() {
  const select = document.getElementById("available-channel");
  const managed = new Set(
    (currentState.channels || []).map((item) => Number(item.chat_id)),
  );
  select.innerHTML = `
    <option value="">请选择频道，或在下方手动填写</option>
    ${availableChannels.map((channel) => `
      <option value="${channel.chat_id}">
        ${managed.has(Number(channel.chat_id)) ? "✓ " : ""}${escapeHtml(channel.name)}
        ${channel.username ? ` · @${escapeHtml(channel.username)}` : ""}
      </option>
    `).join("")}
  `;
}

async function loadAvailableChannels() {
  const button = document.getElementById("refresh-channel-list");
  button.disabled = true;
  button.textContent = "读取中…";
  try {
    availableChannels = await jsonRequest("/api/settings/available-channels");
    renderAvailableChannels();
  } catch (error) {
    document.getElementById("available-channel").innerHTML =
      '<option value="">读取失败，可手动填写 Chat ID</option>';
    message(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "刷新列表";
  }
}

document.getElementById("available-channel").addEventListener("change", (event) => {
  const selected = availableChannels.find(
    (item) => Number(item.chat_id) === Number(event.target.value),
  );
  if (!selected) return;
  const existing = (currentState.channels || []).find(
    (item) => Number(item.chat_id) === Number(selected.chat_id),
  );
  if (existing) {
    editChannel(Number(existing.chat_id));
    document.getElementById("available-channel").value = String(existing.chat_id);
    return;
  }
  document.getElementById("channel-name").value = selected.name;
  document.getElementById("chat-id").value = selected.chat_id;
  document.getElementById("channel-start-mode").value = "now";
  updateStartVisibility();
});

document.getElementById("refresh-channel-list").onclick = loadAvailableChannels;
document.getElementById("new-channel").onclick = resetChannelForm;

function updateSpacingVisibility() {
  document.getElementById("channel-spacing-field").hidden =
    document.getElementById("channel-timeline-mode").value !== "spaced";
}

document.getElementById("channel-timeline-mode")
  .addEventListener("change", updateSpacingVisibility);
updateSpacingVisibility();

async function finalizeChannel(chatId) {
  if (!window.confirm("确定结束这个频道当前的人物批次并开始整理吗？")) return;
  try {
    const result = await jsonRequest(`/api/settings/channels/${chatId}/finalize`, {
      method: "POST",
    });
    message(result.message, "success");
  } catch (error) {
    message(error.message, "error");
  }
}

async function repairChannelOrder(chatId) {
  if (!window.confirm(
    "安全校正这个频道所有已下载人物的内部顺序？不会重新下载，也不会移动 Immich 媒体路径。"
  )) return;
  try {
    const result = await jsonRequest(
      `/api/settings/channels/${chatId}/repair-order`,
      { method: "POST" }
    );
    message(result.message, result.status === "deferred" ? "info" : "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  }
}

async function removeChannel(chatId) {
  if (!window.confirm("停止监听这个频道？已经下载的文件和记录不会删除。")) return;
  try {
    const result = await jsonRequest(`/api/settings/channels/${chatId}`, {
      method: "DELETE",
    });
    message(result.message, "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  }
}

function renderChannels(channels) {
  const list = document.getElementById("channels-list");
  if (!channels.length) {
    list.innerHTML = '<p class="empty">尚未添加频道。</p>';
    return;
  }
  const liveChannels = new Map(
    ((currentState.downloads || {}).channels || [])
      .map((item) => [Number(item.chat_id), item])
  );
  list.innerHTML = [...channels]
    .sort((left, right) => (left.display_order || 0) - (right.display_order || 0))
    .map((channel) => {
    const download = liveChannels.get(Number(channel.chat_id)) || {};
    return `
    <article class="managed-channel">
      <div class="managed-channel-main">
        <strong>${escapeHtml(channel.name)}</strong>
        <small>${channel.chat_id} · ${channel.enabled ? "监听中" : "已停用"} · 排列优先级 ${channel.display_order || 0}</small>
        <small>${channel.start_mode === "date" && channel.start_date
          ? `监听起点：${escapeHtml(channel.start_date)}`
          : channel.start_mode === "now"
            ? "监听起点：保存时刻之后"
            : `监听起点：消息 ${channel.start_message_id || 0}`}</small>
        <small>${channel.grouping_mode === "marker"
          ? `人物标记：${escapeHtml(channel.marker_text || "1")}`
          : "按 Telegram 相册分组"}</small>
        <div class="channel-download-strip">
          <span><b>${download.queued || 0}</b> 排队</span>
          <span><b>${download.downloading || 0}</b> 下载中</span>
          <span><b>${download.failed || 0}</b> 失败</span>
          <span><b>${channel.max_concurrent_downloads || 5}</b> 并发</span>
        </div>
      </div>
      <div class="channel-actions">
        <button type="button" data-action="edit" data-chat-id="${channel.chat_id}">修改</button>
        ${channel.grouping_mode === "marker"
          ? `<button type="button" data-action="finalize" data-chat-id="${channel.chat_id}">结束当前人物</button>`
          : ""}
        <button type="button" data-action="repair-order"
                data-chat-id="${channel.chat_id}">安全校正排序</button>
        <button type="button" class="danger-button" data-action="remove"
                data-chat-id="${channel.chat_id}">停止监听</button>
      </div>
    </article>
  `}).join("");
  list.querySelectorAll("button").forEach((button) => {
    const chatId = Number(button.dataset.chatId);
    if (button.dataset.action === "edit") button.onclick = () => editChannel(chatId);
    if (button.dataset.action === "finalize") button.onclick = () => finalizeChannel(chatId);
    if (button.dataset.action === "repair-order") {
      button.onclick = () => repairChannelOrder(chatId);
    }
    if (button.dataset.action === "remove") button.onclick = () => removeChannel(chatId);
  });
}

function renderBotTasks(tasks = []) {
  const list = document.getElementById("bot-task-list");
  if (!list) return;
  if (!tasks.length) {
    list.innerHTML = '<p class="empty">还没有机器人下载任务。</p>';
    return;
  }
  const labels = {
    queued: "等待继续",
    running: "下载中",
    paused: "已暂停",
    completed: "已完成",
    failed: "有失败",
  };
  list.innerHTML = `
    <p class="section-caption">最近机器人任务</p>
    ${tasks.map((task) => `
      <article class="managed-channel bot-task-card">
        <div class="managed-channel-main">
          <strong>${escapeHtml(task.task_id)}</strong>
          <div class="task-progress"><span style="width:${task.total ? Math.round(((task.success + task.skipped) / task.total) * 100) : 0}%"></span></div>
          <small>${labels[task.status] || escapeHtml(task.status)} ·
            ${task.success}/${task.total} 成功 · ${task.skipped} 跳过 ·
            ${task.failed} 失败</small>
          ${task.status === "running" ? `<small>并发 ${task.active_count}/${task.max_concurrent_downloads} ·
            速度 ${formatBotSpeed(task.speed_bps)} ·
            ${escapeHtml(task.current_file || "正在准备文件")}</small>` : ""}
          <small>${escapeHtml(task.description || "无简介")}</small>
          ${task.output_path
            ? `<code class="task-path">${escapeHtml(task.output_path)}</code>`
            : ""}
          ${task.error
            ? `<small class="task-error">${escapeHtml(task.error)}</small>`
            : ""}
        </div>
        <div class="managed-channel-actions">
          ${task.status === "running" || task.status === "queued"
            ? `<button type="button" data-bot-action="pause" data-task-id="${task.id}">暂停</button>`
            : ""}
          ${task.status === "paused" || task.status === "failed"
            ? `<button type="button" class="primary-button" data-bot-action="resume" data-task-id="${task.id}">继续</button>`
            : ""}
        </div>
      </article>
    `).join("")}
  `;
  list.querySelectorAll("[data-bot-action]").forEach((button) => {
    button.onclick = async () => {
      button.disabled = true;
      try {
        await postJson(
          `/api/downloads/bot/tasks/${button.dataset.taskId}/${button.dataset.botAction}`,
          {},
        );
        await loadBotTasks();
      } catch (error) {
        message(error.message, "error");
      } finally {
        button.disabled = false;
      }
    };
  });
}

function formatBotSpeed(value = 0) {
  const speed = Number(value || 0);
  return speed >= 1024 * 1024
    ? `${(speed / 1024 / 1024).toFixed(1)} MB/s`
    : `${Math.round(speed / 1024)} KB/s`;
}

async function loadBotTasks() {
  const state = await jsonRequest("/api/downloads/bot/status");
  renderBotTasks(state.tasks || []);
}

async function loadState() {
  currentState = await jsonRequest("/api/setup/state");
  rememberedProxy = currentState.proxy_url || "";
  document.getElementById("proxy-url").value = rememberedProxy;
  document.getElementById("saved-proxy-url").value = rememberedProxy;
  if (currentState.authorized) {
    hide("credentials-step");
    hide("code-step");
    hide("password-step");
    show("connection-step");
    show("bot-step");
    show("channel-step");
    show("channels-step");
    document.getElementById("connection-summary").textContent =
      currentState.status === "connection_error"
        ? `网络或代理异常：${currentState.connection_error || "无响应"}。每 60 秒自动重试，恢复后会自动继续监听。`
        : currentState.status === "running"
        ? `Telegram 已连接，正在监听 ${(currentState.channels || []).filter((item) => item.enabled).length} 个频道。`
        : "Telegram 账号已登录，可以添加频道或修改代理。";
    renderChannels(currentState.channels || []);
    if (!availableChannels.length) loadAvailableChannels().catch(() => {});
    else renderAvailableChannels();
    const bot = currentState.bot || {};
    document.getElementById("bot-enabled").checked = Boolean(bot.enabled);
    document.getElementById("bot-concurrency").value = String(
      bot.max_concurrent_downloads || 5,
    );
    document.getElementById("bot-summary").textContent = bot.error
      ? `机器人离线：${bot.error}。未完成的转发已退回队列，网络恢复后自动继续，也可手动继续。`
      : bot.running
      ? `机器人正在运行。原档：${bot.download_path}；独立整理库：${bot.library_path}`
      : bot.configured
        ? "Bot Token 已保存，当前未运行。"
      : "机器人尚未配置。";
    renderBotTasks(bot.tasks || []);
  } else {
    show("credentials-step");
    hide("connection-step");
    hide("channel-step");
    hide("channels-step");
    hide("bot-step");
  }
}

document.getElementById("credentials-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  rememberedProxy = document.getElementById("proxy-url").value.trim();
  setBusy(form, true);
  try {
    const result = await postJson("/api/setup/request-code", {
      api_id: Number(document.getElementById("api-id").value),
      api_hash: document.getElementById("api-hash").value.trim(),
      phone: document.getElementById("phone").value.trim(),
      proxy_url: rememberedProxy,
    });
    message(result.message, "success");
    if (result.status === "authorized") await loadState();
    else {
      show("code-step");
      document.getElementById("login-code").focus();
    }
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

document.getElementById("code-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const result = await postJson("/api/setup/confirm-code", {
      code: document.getElementById("login-code").value.trim(),
    });
    message(result.message, "success");
    if (result.status === "password_required") {
      show("password-step");
      document.getElementById("login-password").focus();
    } else await loadState();
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

document.getElementById("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const result = await postJson("/api/setup/confirm-password", {
      password: document.getElementById("login-password").value,
    });
    document.getElementById("login-password").value = "";
    message(result.message, "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

document.getElementById("proxy-settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const result = await postJson("/api/settings/proxy", {
      proxy_url: document.getElementById("saved-proxy-url").value.trim(),
    }, "PUT");
    message(result.message, "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

document.getElementById("bot-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  setBusy(form, true);
  try {
    const result = await postJson("/api/settings/bot", {
      enabled: document.getElementById("bot-enabled").checked,
      token: document.getElementById("bot-token").value.trim(),
      max_concurrent_downloads: Number(
        document.getElementById("bot-concurrency").value,
      ),
    }, "PUT");
    document.getElementById("bot-token").value = "";
    message(result.message, "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

setInterval(() => {
  if (!document.hidden && !document.getElementById("bot-step").hidden) {
    loadBotTasks().catch(() => {});
  }
}, 3000);

document.getElementById("channel-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = channelPayload();
  setBusy(form, true);
  try {
    const result = await postJson(
      `/api/settings/channels/${payload.chat_id}`,
      payload,
      "PUT",
    );
    message(result.message, "success");
    await loadState();
  } catch (error) {
    message(error.message, "error");
  } finally {
    setBusy(form, false);
  }
});

loadState().catch(() => message("无法读取当前设置状态", "error"));
