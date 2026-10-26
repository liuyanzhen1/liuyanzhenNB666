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
let loginToken = "";

const statusLabels = {
  new: "等待处理", sending: "正在发送", sent: "等待确认", accepted: "下注成功",
  rejected: "未接受", ack_timeout: "回执超时", send_error: "发送失败",
  result_timeout: "开奖超时", dry_run: "模拟下注", skipped_paused: "暂停跳过", skipped_target: "达到目标"
};
const outcomeLabels = { win: "中了", loss: "未中", tie: "持平", unknown: "待核对", not_bet: "未下注" };
const eventLabels = {
  open: "新一期", sent: "已发送", accepted: "下注成功", rejected: "下注拒绝",
  accepted_by_result: "骰子确认", instant_start: "即时开局", result: "开奖", closed: "封盘",
  send_error: "发送失败", ack_timeout: "回执超时", result_timeout: "开奖超时",
  unmatched_dice: "未匹配骰子", duplicate_result: "重复结果", restart_pending: "启动保护"
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
      <strong>${escapeHtml(g.group_name)}</strong><small>${g.group_id} · ${g.turnover_reached ? "已达目标" : g.paused ? "已暂停" : "运行中"}</small>
      <span>流水 ${escapeHtml(number(g.turnover))} / ${escapeHtml(number(g.target_turnover))}</span>
      <progress class="group-progress" max="100" value="${Math.min(100, Number(g.turnover_progress) || 0)}" aria-label="此群流水进度"></progress>
      <small>${escapeHtml(g.game_mode_label || "定时开盘")}${g.game_mode === "instant_dice" ? ` · 间隔 ${g.instant_interval} 秒` : ""} · ${escapeHtml(g.bet_sides.join("＋"))} · 单项 ${g.min_amount}～${g.max_amount} · 已结算 ${g.summary.settled} 局</small></button>`).join("");
    if ($("groupCards").innerHTML !== cards) $("groupCards").innerHTML = cards;
    $("groupErrors").replaceChildren();
    for (const item of state.errors || []) {
      const line = document.createElement("div");
      line.textContent = `${item.group_name} (${item.group_id}) 加载失败：${item.error}`;
      const retry = document.createElement("button");
      retry.type = "button"; retry.className = "button secondary retry-group"; retry.textContent = "重试连接";
      retry.onclick = async () => { retry.disabled = true; try { await postAction("retry_group", {group_id: item.group_id}); } catch(e) { showToast(e.message, true); retry.disabled = false; } };
      line.append(retry); $("groupErrors").append(line);
    }
    state = state.groups.find(g => String(g.group_id) === selectedGroupId);
    if (!state) {
      latestState = null;
      text("selectedGroupInfo", "这个账号还没有群。点击“＋ 添加群”开始独立配置。");
      for (const section of document.querySelectorAll(".hero-grid, .kpi-grid, .workspace-grid, .logs-panel")) section.hidden = true;
      return;
    }
    for (const section of document.querySelectorAll(".hero-grid, .kpi-grid, .workspace-grid, .logs-panel")) section.hidden = false;
    text("selectedGroupInfo", `当前：${state.group_name} · ${state.game_mode_label || "定时开盘"} · 群 ID ${state.group_id} · 机器人 ${state.bot_id} · 文件保存于 ${state.data_directory}`);
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
  text("runStatus", state.turnover_reached ? "已达流水目标" : (isPaused ? "已暂停" : "运行中"));
  const loopDetail = state.game_mode === "instant_dice"
    ? (state.instant_pending_issue ? `已发送 ${state.instant_pending_issue}，等待机器人骰子` : (isPaused ? "即时循环已暂停" : (state.instant_waiting_next ? `已开奖，等待 ${state.instant_interval} 秒后发送下一单` : "准备发送下一单")))
    : "等待机器人公布新期号";
  text("statusDetail", `${state.game_mode_label || "定时开盘"} · ${loopDetail} · ${state.bet_sides.join("＋")} · 单项随机 ${state.min_amount}～${state.max_amount}`);
  $("statusLight").className = `status-light ${isPaused ? "paused" : "running"}`;
  $("pauseButton").disabled = isPaused;
  $("resumeButton").disabled = !isPaused;

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
  $("gameMode").disabled = !state.paused || Boolean(state.instant_pending_issue);
  $("saveModeButton").disabled = false;
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
$("modeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await postAction("set_game_mode", {game_mode: $("gameMode").value, instant_interval: $("instantInterval").value});
    modeDirty = false;
    showToast("开奖方式和下一单间隔已保存");
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
    const existing = new Set(data.groups.map(g => String(g.group_id)));
    $("newGroupId").innerHTML = '<option value="">请选择要添加的群</option>' + data.available_groups
      .filter(g => !existing.has(String(g.group_id)))
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
    if (data.bot_candidates.length === 1 && !$("newBotId").value.trim()) {
      const bot = data.bot_candidates[0];
      $("newBotId").value = bot.username ? `@${bot.username}` : bot.bot_id;
    }
    text("botCandidates", data.bot_candidates.length ? "识别到：" + data.bot_candidates.map(b => `${b.name}（${b.username ? '@' + b.username : b.bot_id}）`).join("、") : "最近 150 条消息未识别到开盘、开奖或骰子机器人，可直接粘贴机器人的 @用户名。");
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
      instant_interval: $("newInstantInterval").value });
    selectedGroupId = gid;
    for (const id of ["minAmount", "maxAmount", "targetInput"]) delete $(id).dataset.dirty;
    render(data);
    $("addGroupPanel").hidden = true;
    showToast("已添加，此群处于暂停状态；核对设置后点击恢复此群");
  } catch(error) { showToast(error.message, true); }
  finally { $("submitAddButton").disabled = false; }
});
