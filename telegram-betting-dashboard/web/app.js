const $ = (id) => document.getElementById(id);
let controlToken = "";
let latestState = null;
let toastTimer = null;
let selectedAccountId = localStorage.getItem("selectedAccountId") || "default";
let selectedGroupId = localStorage.getItem("selectedGroupId") || "";
let allGroupsState = null;
let allAccountsState = null;
let actionVersion = 0;
let inspectionVersion = 0;
let sidesDirty = false;
let modeDirty = false;
let deliveryDirty = false;
let loginToken = "";
let reportDirty = false;

const statusLabels = {
  new: "等待处理", sending: "正在发送", sent: "等待确认", accepted: "下注成功",
  rejected: "未接受", ack_timeout: "回执超时", send_error: "发送失败",
  result_timeout: "开奖超时", abandoned: "失联订单已跳过", dry_run: "模拟下注", skipped_paused: "暂停跳过", skipped_target: "达到目标"
};
const outcomeLabels = { win: "中了", loss: "未中", tie: "持平", unknown: "待核对", not_bet: "未下注" };
const eventLabels = {
  open: "新一期", sent: "已发送", accepted: "下注成功", rejected: "下注拒绝",
  accepted_by_result: "骰子确认", instant_start: "即时开局", result: "开奖", closed: "封盘",
  send_error: "发送失败", ack_timeout: "回执超时", result_timeout: "开奖超时",
  unmatched_dice: "未匹配骰子", duplicate_result: "重复结果", restart_pending: "启动保护",
  ambiguous_private: "私聊回执未匹配", ack_recheck: "回查回执",
  ack_recovered: "回执恢复", watchdog_check: "连续性检查",
  watchdog_recovered: "已自动恢复", watchdog_wait: "等待核对",
  watchdog_error: "自动检查异常", result_recheck_error: "开奖回查异常",
  stalled_skipped: "已跳过失联订单", watchdog_skipped_stalled: "自动跳过失联订单",
  late_abandoned_result: "迟到骰子已忽略", late_abandoned_ack: "迟到回执已忽略"
};

function text(id, value) { $(id).textContent = value ?? "—"; }
function number(value) {
  if (value == null || value === "") return "—";
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric.toLocaleString("zh-CN", { maximumFractionDigits: 2 }) : "—";
}
function time(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}
function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast show${isError ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { toast.className = "toast"; }, 3200);
}

function selectedAccountGroups(state) {
  const accounts = Array.isArray(state?.accounts) ? state.accounts : [];
  const account = accounts.find(item => item.account_id === selectedAccountId);
  return Array.isArray(account?.groups) ? account.groups : [];
}

async function postAction(action, data = {}) {
  actionVersion++;
  const response = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Local-Token": controlToken },
    body: JSON.stringify({ action, account_id: selectedAccountId, group_id: selectedGroupId, ...data })
  });
  const payload = await response.json();
  if (!response.ok || !payload.ok) throw new Error(payload.error || "操作失败");
  render(payload.data);
  return payload.data;
}

function render(state) {
  if (state.accounts) {
    allAccountsState = state;
    text("reportStatus", `${state.report_status || "等待运行"}${state.next_report_at ? " · 下次 " + time(state.next_report_at) : ""}${state.last_report_at ? " · 上次 " + time(state.last_report_at) : ""}`);
    const reportAccounts = state.accounts.filter(account => account.account_username);
    const reportOptions = reportAccounts.map(account => `<option value="@${escapeHtml(account.account_username)}">${escapeHtml(account.account_label)} · @${escapeHtml(account.account_username)}</option>`).join("");
    if ($("reportAccountSelect").innerHTML !== reportOptions) $("reportAccountSelect").innerHTML = reportOptions;
    if (!reportDirty) {
      $("reportEnabled").checked = Boolean(state.report_enabled);
      $("reportIncludeDetails").checked = Boolean(state.report_include_details);
      $("reportAccountSelect").value = `@${state.report_sender_username || ""}`;
      $("reportChatInput").value = state.report_chat_selector || "";
    }
    const overallModeText = state.mode === "live" ? "真实发送" : "模拟模式";
    $("modeBadge").className = `badge ${state.mode}`;
    text("modeBadge", overallModeText);
    $("connectionDot").className = "connection online";
    $("connectionDot").innerHTML = "<i></i> 本地连接正常";
    if (!state.accounts.some(account => account.account_id === selectedAccountId)) {
      selectedAccountId = state.accounts[0]?.account_id || "default";
      localStorage.setItem("selectedAccountId", selectedAccountId);
    }
    const accountOptions = state.accounts.map(account => {
      const suffix = account.account_status === "connected" ? `${account.groups.length} 个群` : "连接异常";
      return `<option value="${escapeHtml(account.account_id)}">${escapeHtml(account.account_label)} · ${escapeHtml(suffix)}</option>`;
    }).join("");
    if ($("accountSelect").innerHTML !== accountOptions) $("accountSelect").innerHTML = accountOptions;
    $("accountSelect").value = selectedAccountId;
    const account = state.accounts.find(item => item.account_id === selectedAccountId);
    if (!account) return;
    const identity = account.account_username ? `@${account.account_username}` : account.account_display_name;
    text("accountInfo", `${account.account_status === "connected" ? "已连接" : "连接异常"} · ${identity || account.account_label}${account.account_phone_mask ? " · " + account.account_phone_mask : ""}${account.account_error ? " · " + account.account_error : ""}`);
    $("removeAccountButton").disabled = !account.can_delete;
    $("addGroupButton").disabled = account.account_status !== "connected";
    state = account;
  }
  if (state.groups) {
    allGroupsState = allAccountsState || state;
    if (!state.groups.some(g => String(g.group_id) === selectedGroupId)) {
      selectedGroupId = String(state.groups[0]?.group_id || "");
      localStorage.setItem(`selectedGroupId:${selectedAccountId}`, selectedGroupId);
    }
    text("groupCount", `· ${state.groups.length} 个群`);
    // Preserve focused buttons during the two-second refresh.
    const cards = state.groups.map(g => `<button type="button" class="group-card" data-group-id="${g.group_id}" aria-pressed="${String(g.group_id) === selectedGroupId}">
      <strong>${escapeHtml(g.group_name)}</strong><small>${g.group_id} · ${g.turnover_reached ? "已达目标" : g.paused && g.run_requested ? "自动恢复等待中" : g.paused ? "已暂停" : "运行中"}</small>
      <span>流水 ${escapeHtml(number(g.turnover))} / ${escapeHtml(number(g.target_turnover))}</span>
      <progress class="group-progress" max="100" value="${Math.min(100, Number(g.turnover_progress) || 0)}" aria-label="此群流水进度"></progress>
      <small>${escapeHtml(g.game_mode_label || "定时开盘")}${g.game_mode === "instant_dice" ? ` · 间隔 ${g.instant_interval} 秒` : ""} · ${escapeHtml(g.bet_delivery_label || "群内发送")} · ${escapeHtml(g.bet_sides.join("＋"))} · 单项 ${g.min_amount}～${g.max_amount} · 已结算 ${g.summary.settled} 局</small></button>`).join("");
    if ($("groupCards").innerHTML !== cards) $("groupCards").innerHTML = cards;
    $("groupErrors").replaceChildren();
    for (const item of state.errors || []) {
      const line = document.createElement("div");
      line.textContent = `${item.group_name} (${item.group_id}) 加载失败：${item.error}`;
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "button secondary retry-group"; retry.textContent = "重试连接";
      retry.onclick = async () => { retry.disabled = true; try { await postAction("retry_group", {group_id: item.group_id}); } catch(e) { showToast(e.message, true); retry.disabled = false; } };
      const remove = document.createElement("button");
      remove.type = "button"; remove.className = "button danger retry-group"; remove.textContent = "删除配置";
      remove.onclick = () => removeGroup(item.group_id, item.group_name || String(item.group_id));
      line.append(retry, remove); $("groupErrors").append(line);
    }
    state = state.groups.find(g => String(g.group_id) === selectedGroupId);
    if (!state) {
      latestState = null;
      text("selectedGroupInfo", "这个账号还没有群。点击“＋ 添加群”开始独立配置。");
      for (const section of document.querySelectorAll(".hero-grid, .kpi-grid, .workspace-grid, .logs-panel")) section.hidden = true;
      return;
    }
    for (const section of document.querySelectorAll(".hero-grid, .kpi-grid, .workspace-grid, .logs-panel")) section.hidden = false;
    text("selectedGroupInfo", `当前：${state.group_name} · ${state.game_mode_label || "定时开盘"} · ${state.bet_delivery === "private_bot" ? "群内开奖 / 私聊下注" : "群内开奖 / 群内下注"} · 群 ID ${state.group_id} · 机器人 ${state.bot_id} · 文件保存于 ${state.data_directory}`);
  } else {
    text("selectedGroupInfo", "后台仍为旧版本，请关闭旧程序后重新启动多群版本。");
    $("addGroupButton").disabled = true;
    $("pauseAllButton").disabled = true;
  }
  latestState = state;
  const isPaused = state.paused || state.turnover_reached;
  const modeText = state.mode === "live" ? "真实发送" : "模拟模式";
  $("modeBadge").className = `badge ${state.mode}`;
  text("modeBadge", modeText);
  $("connectionDot").className = "connection online";
  $("connectionDot").innerHTML = "<i></i> 本地连接正常";
  text("groupName", state.group_name);
  text("runStatus", state.turnover_reached ? "已达流水目标" : (state.paused && state.run_requested ? "自动恢复等待中" : (isPaused ? "已暂停" : "运行中")));
  const loopDetail = state.game_mode === "instant_dice"
    ? (state.instant_pending_issue ? `已发送 ${state.instant_pending_issue}，等待机器人骰子` : (isPaused ? "即时循环已暂停" : (state.instant_waiting_next ? `已开奖，等待 ${state.instant_interval} 秒后发送下一单` : "准备发送下一单")))
    : "等待机器人公布新期号";
  text("statusDetail", `${state.game_mode_label || "定时开盘"} · ${state.bet_delivery_label || "群内发送"} · ${loopDetail} · ${state.bet_sides.join("＋")} · 单项随机 ${state.min_amount}～${state.max_amount}`);
  text("watchdogDetail", `自动检查：${state.watchdog_status || "每 3 分钟检查一次"} · 已恢复 ${state.recovery_count || 0} 次${state.last_watchdog_at ? " · 最近 " + time(state.last_watchdog_at) : ""}`);
  $("statusLight").className = `status-light ${isPaused ? "paused" : "running"}`;
  $("pauseButton").disabled = !state.run_requested;
  $("resumeButton").disabled = state.run_requested && !isPaused;
  $("deleteGroupButton").disabled = !state.can_delete_group;
  $("deleteGroupButton").title = state.can_delete_group ? "删除并归档此群的本地数据" : "当前群不可删除";

  text("turnoverValue", number(state.turnover));
  text("turnoverTarget", number(state.target_turnover));
  text("turnoverResetInfo", `历史总流水 ${number(state.lifetime_turnover)} · ${state.turnover_reset_at ? "重置于 " + time(state.turnover_reset_at) : "尚未手动重置"}`);
  text("turnoverPercent", `${number(state.turnover_progress)}%`);
  $("turnoverBar").style.width = `${Math.min(100, Number(state.turnover_progress) || 0)}%`;

  const latest = state.latest;
  text("latestIssue", latest?.issue || "暂无");
  text("latestCommand", latest?.command || "—");
  const outcome = latest?.outcome ? (outcomeLabels[latest.outcome] || latest.outcome) : "等待开奖";
  text("latestOutcome", outcome);
  $("latestOutcome").className = latest?.outcome === "win" ? "win" : (latest?.outcome === "loss" ? "loss" : (latest?.outcome === "tie" ? "neutral" : ""));
  text("latestDice", latest?.dice ?? "—");
  text("latestBalance", number(latest?.balance));
  text("latestNet", latest?.net == null ? "—" : `${Number(latest.net) >= 0 ? "+" : ""}${number(latest.net)}`);

  text("settledCount", state.summary.settled);
  text("winCount", state.summary.wins);
  text("lossCount", state.summary.losses);
  text("tieCount", state.summary.ties || 0);
  text("hitRate", `${number(state.summary.hit_rate)}%`);
  text("totalNet", `${Number(state.summary.total_net) >= 0 ? "+" : ""}${number(state.summary.total_net)}`);
  $("totalNet").className = Number(state.summary.total_net) >= 0 ? "positive" : "negative";
  text("currentBalance", number(state.current_balance ?? latest?.balance));
  text("lastUpdated", `更新于 ${time(state.updated_at)}`);

  for (const [id, value] of [["minAmount", state.min_amount], ["maxAmount", state.max_amount], ["targetInput", state.target_turnover]]) {
    if (!$(id).dataset.dirty && document.activeElement !== $(id)) $(id).value = value;
  }
  if (!sidesDirty) {
    for (const input of document.querySelectorAll('input[name="betSide"]')) input.checked = state.bet_sides.includes(input.value);
  }
  if (!modeDirty && document.activeElement !== $("gameMode")) $("gameMode").value = state.game_mode || "scheduled";
  if (!modeDirty && document.activeElement !== $("instantInterval")) $("instantInterval").value = state.instant_interval || 5;
  if (!deliveryDirty && document.activeElement !== $("betDelivery")) $("betDelivery").value = state.bet_delivery || "group";
  $("gameMode").disabled = !state.paused || Boolean(state.instant_pending_issue);
  $("saveModeButton").disabled = false;
  $("betDelivery").disabled = !state.paused || Boolean(state.instant_pending_issue);
  $("saveDeliveryButton").disabled = !state.paused || Boolean(state.instant_pending_issue);
  text("deliveryHelp", state.bet_delivery === "private_bot"
    ? "继续在当前群读取开盘和开奖结果；下注命令发送给当前游戏机器人私聊，私聊回执会自动归入本群。"
    : "开盘、下注和开奖结果都在当前群处理。切换前必须先暂停此群。");
  text("gameModeHelp", state.game_mode === "instant_dice"
    ? `收到骰子并记录结果后等待 ${state.instant_interval} 秒，再发送下一单。${state.mode === "simulation" ? "当前是模拟模式，只生成一单且不会触发机器人骰子。" : `超过 ${state.instant_result_timeout} 秒未收到骰子会自动暂停。`}`
    : "等待机器人公布新期号后下注。间隔设置仅用于“下注后掷骰”；切换开奖方式前必须先暂停此群。");
  updateStakePreview();
  renderRounds(state.rounds);
  renderLogs(state.events);
}

function renderRounds(rounds) {
  if (!rounds.length) {
    $("roundsBody").innerHTML = '<tr><td colspan="7" class="empty">暂无下注记录</td></tr>';
    return;
  }
  $("roundsBody").innerHTML = rounds.map((row) => {
    const outcome = row.outcome ? (outcomeLabels[row.outcome] || row.outcome) : "待开奖";
    const resultClass = row.outcome === "win" ? "result-win" : (row.outcome === "loss" ? "result-loss" : (row.outcome === "tie" ? "neutral" : ""));
    return `<tr>
      <td class="mono">${escapeHtml(row.issue)}</td>
      <td class="mono">${escapeHtml(row.command || "—")}</td>
      <td><span class="status-tag ${escapeHtml(row.status)}">${escapeHtml(statusLabels[row.status] || row.status)}</span></td>
      <td class="${resultClass}">${escapeHtml(outcome)}${row.dice == null ? "" : ` · ${escapeHtml(row.dice)} · ${escapeHtml((row.result_side || "").replaceAll(",", "＋"))}`}</td>
      <td>${escapeHtml(number(row.turnover))}</td>
      <td>${escapeHtml(number(row.balance))}</td>
      <td>${escapeHtml(time(row.result_at || row.ack_at || row.opened_at))}</td>
    </tr>`;
  }).join("");
}

function renderLogs(events) {
  if (!events.length) {
    $("logList").innerHTML = '<div class="empty">暂无日志</div>';
    return;
  }
  $("logList").innerHTML = events.map((event) => `<div class="log-entry">
    <span class="log-time">${escapeHtml(time(event.created_at))}</span>
    <span class="log-issue">${escapeHtml(event.issue || "系统")}</span>
    <span><b>${escapeHtml(eventLabels[event.event_type] || event.event_type)}</b> ${escapeHtml(event.text || "")}</span>
  </div>`).join("");
}

async function refresh() {
  const version = actionVersion;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || !payload.ok) throw new Error(payload.error || "刷新失败");
    if (version === actionVersion) render(payload.data);
  } catch (error) {
    $("connectionDot").className = "connection offline";
    $("connectionDot").innerHTML = "<i></i> 本地连接中断";
  }
}

async function setup() {
  try {
    const response = await fetch("/api/bootstrap", { cache: "no-store" });
    const payload = await response.json();
    controlToken = payload.token;
    await refresh();
    setInterval(refresh, 2000);
  } catch (error) {
    showToast(`面板初始化失败：${error.message}`, true);
  }
}

$("pauseButton").addEventListener("click", async () => {
  try { await postAction("pause"); showToast("已暂停下注，日志仍会继续记录"); }
  catch (error) { showToast(error.message, true); }
});
$("resumeButton").addEventListener("click", async () => {
  try { await postAction("resume"); showToast(latestState?.game_mode === "instant_dice" ? "已恢复，即时模式正在发送下一单" : "已恢复，从下一期开始下注"); }
  catch (error) { showToast(error.message, true); }
});

for (const id of ["reportEnabled", "reportIncludeDetails", "reportAccountSelect", "reportChatInput"]) {
  $(id).addEventListener("change", () => { reportDirty = true; });
  if (id === "reportChatInput") $(id).addEventListener("input", () => { reportDirty = true; });
}

$("reportForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("save_report_settings", {
      report_enabled: $("reportEnabled").checked,
      report_include_details: $("reportIncludeDetails").checked,
      report_sender_username: $("reportAccountSelect").value,
      report_chat_selector: $("reportChatInput").value
    });
    reportDirty = false;
    showToast("半小时汇报设置已保存");
  } catch (error) { showToast(error.message, true); }
});

$("sendReportNowButton").addEventListener("click", async () => {
  const button = $("sendReportNowButton");
  button.disabled = true;
  try {
    await postAction("send_report_now");
    showToast("汇报已发送");
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
});

async function removeGroup(groupId, groupName) {
  if (!confirm(`确定从当前账号删除“${groupName}”吗？\n\n该群会立即停止，本地数据库和每局记录将移到“已移除群”目录归档。不会退出或删除 Telegram 群。`)) return;
  try {
    const data = await postAction("remove_group", {group_id: groupId});
    selectedGroupId = String(data.groups?.[0]?.group_id || selectedAccountGroups(data)[0]?.group_id || "");
    localStorage.setItem(`selectedGroupId:${selectedAccountId}`, selectedGroupId);
    render(data);
    showToast(data.archived_group_path ? "群已删除，本地数据已归档" : "群已删除");
  } catch (error) {
    showToast(error.message, true);
  }
}

$("deleteGroupButton").addEventListener("click", () => {
  if (!latestState?.can_delete_group) return;
  removeGroup(latestState.group_id, latestState.group_name);
});
$("modeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_game_mode", {game_mode: $("gameMode").value, instant_interval: $("instantInterval").value});
    modeDirty = false;
    showToast("开奖方式和下一单间隔已保存");
  } catch(error) { showToast(error.message, true); }
});
$("deliveryForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_delivery", {bet_delivery: $("betDelivery").value});
    deliveryDirty = false;
    showToast($("betDelivery").value === "private_bot" ? "已设置为群内开奖、机器人私聊下注" : "已设置为群内下注");
  } catch(error) { showToast(error.message, true); }
});
$("amountForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_amount", { min_amount: $("minAmount").value, max_amount: $("maxAmount").value });
    delete $("minAmount").dataset.dirty;
    delete $("maxAmount").dataset.dirty;
    showToast("随机金额范围已保存");
  } catch (error) { showToast(error.message, true); }
});
function selectedSides(selector) {
  return [...document.querySelectorAll(selector)].filter(input => input.checked).map(input => input.value);
}
function updateStakePreview() {
  const count = selectedSides('input[name="betSide"]').length;
  const minimum = Number($("minAmount").value || latestState?.min_amount || 0) * count;
  const maximum = Number($("maxAmount").value || latestState?.max_amount || 0) * count;
  text("stakePreview", count ? `已选 ${count} 项；每期总流水约 ${number(minimum)}～${number(maximum)}` : "至少选择一项");
}
$("sidesForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_sides", {bet_sides: selectedSides('input[name="betSide"]')});
    sidesDirty = false;
    showToast("下注选项已保存，从下一期开始生效");
  } catch(error) { showToast(error.message, true); }
});
$("targetForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_target", { target_turnover: $("targetInput").value });
    delete $("targetInput").dataset.dirty;
    showToast("总流水目标已保存");
  } catch (error) { showToast(error.message, true); }
});
$("exportButton").addEventListener("click", async () => {
  try { const folder = latestState?.data_directory || "data"; await postAction("export"); showToast(`已导出到 ${folder}/rounds.csv`); }
  catch (error) { showToast(error.message, true); }
});
$("stopButton").addEventListener("click", async () => {
  if (!confirm("确定停止整个程序吗？Telegram 连接和本地网页都会关闭。")) return;
  try { await postAction("stop"); showToast("程序正在安全停止"); }
  catch (error) { showToast(error.message, true); }
});

for (const id of ["minAmount", "maxAmount", "targetInput"]) {
  $(id).addEventListener("input", () => { $(id).dataset.dirty = "true"; updateStakePreview(); });
}
for (const input of document.querySelectorAll('input[name="betSide"]')) input.addEventListener("change", () => { sidesDirty = true; updateStakePreview(); });
$("gameMode").addEventListener("change", () => { modeDirty = true; });
$("instantInterval").addEventListener("input", () => { modeDirty = true; });
$("betDelivery").addEventListener("change", () => { deliveryDirty = true; });
$("resetTurnoverButton").addEventListener("click", async () => {
  const button = $("resetTurnoverButton");
  button.disabled = true;
  try {
    await postAction("reset_turnover", { target_turnover: $("targetInput").value });
    delete $("targetInput").dataset.dirty;
    showToast("本轮流水已归零，目标已保存；点击恢复下注开始新一轮");
  } catch (error) { showToast(error.message, true); }
  finally { button.disabled = false; }
});

function resetAccountWizard() {
  loginToken = "";
  $("accountCredentialsForm").hidden = false;
  $("accountVerifyForm").hidden = true;
  $("accountCodeLabel").hidden = false;
  $("accountPasswordLabel").hidden = true;
  $("accountCode").value = "";
  $("accountPassword").value = "";
}

$("accountSelect").addEventListener("change", () => {
  selectedAccountId = $("accountSelect").value;
  localStorage.setItem("selectedAccountId", selectedAccountId);
  selectedGroupId = localStorage.getItem(`selectedGroupId:${selectedAccountId}`) || "";
  sidesDirty = false;
  modeDirty = false;
  deliveryDirty = false;
  render(allAccountsState);
});

$("addAccountButton").addEventListener("click", () => {
  resetAccountWizard();
  $("addAccountPanel").hidden = false;
  $("accountLabel").focus();
});

$("cancelAccountButton").addEventListener("click", async () => {
  try {
    if (loginToken) await postAction("cancel_add_account", {login_token: loginToken});
  } catch(error) {
    showToast(error.message, true);
  }
  resetAccountWizard();
  $("addAccountPanel").hidden = true;
});

$("accountCredentialsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("sendCodeButton");
  button.disabled = true;
  try {
    const data = await postAction("begin_add_account", {
      label: $("accountLabel").value,
      phone: $("accountPhone").value
    });
    loginToken = data.login_pending?.token || "";
    $("accountCredentialsForm").hidden = true;
    $("accountVerifyForm").hidden = false;
    text("accountVerifyHint", `验证码已发送到 ${data.login_pending?.phone_mask || "该账号"}。验证码和密码不会保存。`);
    $("accountCode").focus();
  } catch(error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("accountVerifyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = $("verifyAccountButton");
  button.disabled = true;
  try {
    const data = await postAction("complete_add_account", {
      login_token: loginToken,
      code: $("accountCode").value,
      password: $("accountPassword").value
    });
    const pending = data.login_pending || {};
    if (pending.stage === "password") {
      $("accountCodeLabel").hidden = true;
      $("accountPasswordLabel").hidden = false;
      $("accountCode").value = "";
      text("accountVerifyHint", "此账号开启了两步验证，请输入密码完成登录。密码不会保存。");
      $("accountPassword").focus();
      return;
    }
    if (pending.stage === "done") {
      selectedAccountId = pending.account_id;
      selectedGroupId = "";
      localStorage.setItem("selectedAccountId", selectedAccountId);
      resetAccountWizard();
      $("addAccountPanel").hidden = true;
      render(data);
      showToast("账号添加成功，可以为它单独添加群");
    }
  } catch(error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
});

$("removeAccountButton").addEventListener("click", async () => {
  const account = allAccountsState?.accounts?.find(item => item.account_id === selectedAccountId);
  if (!account?.can_delete) return;
  if (!confirm(`确定从控制台移除“${account.account_label}”吗？\n\nTelegram 登录和该账号的群数据会移到“data/已移除账号”中备份，不会删除 Telegram 账号。`)) return;
  try {
    const data = await postAction("remove_account");
    selectedAccountId = data.accounts[0]?.account_id || "default";
    selectedGroupId = "";
    localStorage.setItem("selectedAccountId", selectedAccountId);
    render(data);
    showToast("账号已从控制台移除，本地文件已归档");
  } catch(error) {
    showToast(error.message, true);
  }
});

setup();

$("groupCards").addEventListener("click", (event) => {
  const button = event.target.closest("[data-group-id]");
  if (!button) return;
  selectedGroupId = button.dataset.groupId;
  localStorage.setItem("selectedGroupId", selectedGroupId);
  localStorage.setItem(`selectedGroupId:${selectedAccountId}`, selectedGroupId);
  for (const id of ["minAmount", "maxAmount", "targetInput"]) delete $(id).dataset.dirty;
  sidesDirty = false;
  modeDirty = false;
  deliveryDirty = false;
  document.activeElement?.blur();
  render(allGroupsState);
});
$("pauseAllButton").addEventListener("click", async () => {
  try { await postAction("pause_all"); showToast("所有群已暂停，继续记录回执和开奖"); }
  catch(error) { showToast(error.message, true); }
});
$("cancelAddButton").addEventListener("click", () => { $("addGroupPanel").hidden = true; });
$("addGroupButton").addEventListener("click", async () => {
  $("addGroupPanel").hidden = false;
  $("addGroupButton").disabled = true;
  try {
    const data = await postAction("list_groups");
    const currentGroups = Array.isArray(data.groups) ? data.groups : selectedAccountGroups(data);
    const availableGroups = Array.isArray(data.available_groups) ? data.available_groups : [];
    const existing = new Set(currentGroups.map(g => String(g.group_id)));
    const addableGroups = availableGroups.filter(g => !existing.has(String(g.group_id)));
    $("newGroupId").innerHTML = `<option value="">${addableGroups.length ? "请选择要添加的群" : "没有发现可添加的群"}</option>` + addableGroups
      .map(g => `<option value="${g.group_id}">${escapeHtml(g.name)} (${g.group_id})</option>`).join("");
  } catch(error) { showToast(error.message, true); }
  finally { $("addGroupButton").disabled = false; }
});
$("newGroupId").addEventListener("change", async () => {
  const version = ++inspectionVersion;
  $("newBotId").value = "";
  if (!$("newGroupId").value) return;
  text("botCandidates", "正在从最近的开盘、开奖或骰子消息中识别机器人…");
  try {
    const data = await postAction("inspect_group", {group_id: $("newGroupId").value});
    if (version !== inspectionVersion) return;
    const candidates = Array.isArray(data.bot_candidates) ? data.bot_candidates : [];
    if (candidates.length === 1 && !$("newBotId").value.trim()) {
      const bot = candidates[0];
      $("newBotId").value = bot.username ? `@${bot.username}` : bot.bot_id;
    }
    text("botCandidates", candidates.length ? "识别到：" + candidates.map(b => `${b.name}（${b.username ? '@' + b.username : b.bot_id}）`).join("、") : "最近 150 条消息未识别到开盘、开奖或骰子机器人，可直接粘贴机器人的 @用户名。");
  } catch(error) { if (version === inspectionVersion) text("botCandidates", error.message); }
});
$("addGroupForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("submitAddButton").disabled = true;
  const gid = $("newGroupId").value;
  try {
    const data = await postAction("add_group", { group_id: gid, bot_id: $("newBotId").value,
      min_amount: $("newMin").value, max_amount: $("newMax").value, target_turnover: $("newTarget").value,
      bet_sides: selectedSides('input[name="newBetSide"]'), game_mode: $("newGameMode").value,
      instant_interval: $("newInstantInterval").value, bet_delivery: $("newBetDelivery").value });
    selectedGroupId = gid;
    for (const id of ["minAmount", "maxAmount", "targetInput"]) delete $(id).dataset.dirty;
    render(data);
    $("addGroupPanel").hidden = true;
    showToast("已添加，此群处于暂停状态；核对设置后点击恢复此群");
  } catch(error) { showToast(error.message, true); }
  finally { $("submitAddButton").disabled = false; }
});
