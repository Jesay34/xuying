const flash = document.getElementById("immich-flash");

function notify(text, kind = "info") {
  flash.hidden = false;
  flash.className = `flash ${kind}`;
  flash.textContent = text;
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "请求失败");
  return body;
}

async function loadSettings() {
  const state = await request("/api/immich/settings");
  document.getElementById("immich-url").value = state.base_url;
  document.getElementById("immich-path").value = state.external_library_path;
  document.getElementById("album-prefix").value = state.album_prefix;
  document.getElementById("auto-scan").checked = state.auto_scan;
  document.getElementById("auto-album").checked = state.auto_album;
  document.getElementById("forwarded-path").value = state.forwarded_external_library_path;
  document.getElementById("forwarded-album-prefix").value = state.forwarded_album_prefix;
  document.getElementById("forwarded-auto-scan").checked = state.forwarded_auto_scan;
  document.getElementById("forwarded-auto-album").checked = state.forwarded_auto_album;
  document.getElementById("forwarded-auto-archive").checked = state.forwarded_auto_archive;
  if (state.configured) await loadLibraries(state.library_id, state.forwarded_library_id);
}

async function loadLibraries(selected = "", forwardedSelected = "") {
  const libraries = await request("/api/immich/libraries");
  const options = libraries.map((library) => {
    const count = Number(library.statistics?.total ?? library.assetCount ?? library.assets?.total ?? 0);
    return `<option value="${library.id}">${library.name} · ${count.toLocaleString("zh-CN")} 项</option>`;
  }).join("");
  const main = document.getElementById("immich-library");
  main.innerHTML = '<option value="">请选择频道人物外部库</option>' + options;
  main.value = selected;
  const forwarded = document.getElementById("forwarded-library");
  forwarded.innerHTML = '<option value="">请选择机器人下载外部库</option>' + options;
  forwarded.value = forwardedSelected;
}

const stateLabels = {
  idle: "等待下一次整理",
  queued: "已排队",
  scanning: "正在扫描",
  syncing: "正在等待入库并同步相册",
  completed: "同步完成",
  waiting: "等待 Immich 入库",
  retry_pending: "等待自动重试",
};

function renderTargetStatus(elementId, status) {
  const box = document.getElementById(elementId);
  if (!box) return;
  const missing = Number(status?.missing_assets || 0);
  const attempt = Number(status?.attempt || 0);
  const unsupported = Number(status?.unsupported_assets || 0);
  const members = Number(status?.album_member_assets || 0);
  const timeline = Number(status?.album_timeline_assets || 0);
  const collapsed = Number(status?.collapsed_live_photo_assets || 0);
  const details = [
    missing ? `尚待入库 ${missing} 项` : "",
    unsupported ? `${unsupported} 项扩展名 Immich 无法入库` : "",
    members ? `相册成员 ${members} 项` : "",
    collapsed ? `时间线 ${timeline} 项（Live Photo 合并 ${collapsed} 项）` : "",
    attempt ? `第 ${attempt} 次校正` : "",
  ].filter(Boolean).join(" · ");
  box.innerHTML = `<b>${stateLabels[status?.state] || status?.state || "未知状态"}</b>` +
    `<span>${status?.message || ""}${details ? ` · ${details}` : ""}</span>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]
  ));
}

function cell(value, kind = "") {
  const n = Number(value || 0);
  return `<td class="${n === 0 ? "zero" : kind}">${n.toLocaleString("zh-CN")}</td>`;
}

function renderAlbumReport(report) {
  const body = document.querySelector("#album-report-table tbody");
  const summary = document.getElementById("album-report-summary");
  if (!body || !summary) return;
  const albums = Array.isArray(report?.albums) ? report.albums : [];
  if (!albums.length) {
    body.innerHTML = `<tr><td colspan="7">还没有明细，等第一次人物相册同步完成后自动出现。</td></tr>`;
    summary.innerHTML = "<b>暂无数据</b><span>同步一次频道人物库即可生成。</span>";
    return;
  }
  const sum = (key) => albums.reduce((acc, a) => acc + Number(a[key] || 0), 0);
  const problems = albums.filter((a) => Number(a.missing || 0) || Number(a.unsupported || 0));
  body.innerHTML = albums.map((a) => {
    const bad = Number(a.missing || 0) || Number(a.unsupported || 0);
    return `<tr class="${bad ? "row-bad" : ""}">` +
      `<td>${escapeHtml(a.album)}</td>` +
      cell(a.hardlinked) + cell(a.members) + cell(a.timeline) +
      cell(a.collapsed, "warn") + cell(a.missing, "bad") + cell(a.unsupported, "bad") +
      `</tr>`;
  }).join("");
  const stamp = report?.generated_at ? `统计于 ${escapeHtml(report.generated_at)}` : "";
  summary.innerHTML =
    `<b>${albums.length} 个人物相册${problems.length ? ` · ${problems.length} 个需要关注` : " · 全部一致"}</b>` +
    `<span>硬链接 ${sum("hardlinked").toLocaleString("zh-CN")} 项 · ` +
    `相册成员 ${sum("members").toLocaleString("zh-CN")} 项 · ` +
    `时间线 ${sum("timeline").toLocaleString("zh-CN")} 项 · ` +
    `Live Photo 合并 ${sum("collapsed").toLocaleString("zh-CN")} 项 · ` +
    `待入库 ${sum("missing").toLocaleString("zh-CN")} 项${stamp ? ` · ${stamp}` : ""}</span>`;
}

async function loadAlbumReport() {
  try {
    renderAlbumReport(await request(`/api/immich/album-report?_=${Date.now()}`));
  } catch (error) {
    const summary = document.getElementById("album-report-summary");
    if (summary) summary.innerHTML = `<b>读取明细失败</b><span>${escapeHtml(error.message)}</span>`;
  }
}

async function loadRefreshStatus() {
  try {
    const status = await request(`/api/immich/refresh-status?_=${Date.now()}`);
    renderTargetStatus("main-sync-status", status.main || status);
    renderTargetStatus("forwarded-sync-status", status.forwarded || {});
  } catch (error) {
    renderTargetStatus("main-sync-status", {state: "retry_pending", message: error.message});
  }
}

async function queuePipeline(target) {
  const button = document.getElementById(target === "main" ? "sync-main-library" : "sync-forwarded-library");
  button.disabled = true;
  try {
    const result = await request(`/api/immich/sync/${target}`, {method: "POST"});
    notify(`${result.message}，后续无需停留在本页面。`, "success");
    await loadRefreshStatus();
    await loadAlbumReport();
  } catch (error) {
    notify(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

document.getElementById("immich-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button[type='submit']");
  button.disabled = true;
  try {
    const state = await request("/api/immich/settings", {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        enabled: true,
        base_url: document.getElementById("immich-url").value.trim(),
        api_key: document.getElementById("immich-key").value.trim(),
        library_id: document.getElementById("immich-library").value,
        external_library_path: document.getElementById("immich-path").value.trim(),
        auto_scan: document.getElementById("auto-scan").checked,
        auto_album: document.getElementById("auto-album").checked,
        album_prefix: document.getElementById("album-prefix").value.trim(),
        forwarded_library_id: document.getElementById("forwarded-library").value,
        forwarded_external_library_path: document.getElementById("forwarded-path").value.trim(),
        forwarded_auto_scan: document.getElementById("forwarded-auto-scan").checked,
        forwarded_auto_album: document.getElementById("forwarded-auto-album").checked,
        forwarded_auto_archive: document.getElementById("forwarded-auto-archive").checked,
        forwarded_album_prefix: document.getElementById("forwarded-album-prefix").value.trim(),
      }),
    });
    notify(state.message, "success");
    document.getElementById("immich-key").value = "";
    await loadLibraries(state.library_id, state.forwarded_library_id);
  } catch (error) {
    notify(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

document.getElementById("sync-main-library").addEventListener("click", () => queuePipeline("main"));
document.getElementById("sync-forwarded-library").addEventListener("click", () => queuePipeline("forwarded"));

document.getElementById("sync-albums").addEventListener("click", async () => {
  try {
    const result = await request("/api/immich/albums/sync", {method: "POST"});
    notify(`人物相册校正完成：新建 ${result.created_albums}，更新 ${result.updated_albums}，尚未入库 ${result.missing_assets} 项。`, "success");
    await loadAlbumReport();
  } catch (error) { notify(error.message, "error"); }
});

document.getElementById("sync-forwarded-albums").addEventListener("click", async () => {
  try {
    const result = await request("/api/immich/forwarded/albums/sync", {method: "POST"});
    notify(`机器人相册校正完成：新建 ${result.created_albums}，更新 ${result.updated_albums}，尚未入库 ${result.missing_assets} 项。`, "success");
  } catch (error) { notify(error.message, "error"); }
});

loadSettings().catch((error) => notify(error.message, "error"));
loadRefreshStatus();
loadAlbumReport();
window.setInterval(loadRefreshStatus, 10000);
window.setInterval(loadAlbumReport, 30000);
