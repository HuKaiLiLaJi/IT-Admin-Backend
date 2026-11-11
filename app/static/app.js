"use strict";

// Authentication and drafts belong to this page instance, so accounts cannot share them.
const state = { token: null, user: null, ticket: null, systems: [], conversation: null,
  previousTicket: null, offset: 0, total: 0, busy: false, view: "tickets",
  filters: {}, detailView: "overview", intakeFiles: [] };
const $ = (id) => document.getElementById(id);
const statusNames = { pending_assignment: "待分派", pending_acceptance: "待接单",
  in_progress: "处理中", awaiting_confirmation: "待员工确认", closed: "已关闭", cancelled: "已撤销" };
const roleNames = { submitter: "提交人", dispatcher: "分派人员", handler: "处理人",
  manager: "管理者", config_admin: "配置管理员" };
const actionNames = { created: "创建工单", assigned: "分派", reassigned: "改派", accepted: "接单",
  note_added: "公开处理备注", result_submitted: "提交处理结果", confirmed_unresolved: "确认未解决",
  confirmed_resolved: "确认解决并关闭", cancelled: "撤销工单", cancellation_requested: "申请撤销",
  cancellation_approved: "批准撤销", cancellation_rejected: "驳回撤销", attachments_added: "追加附件",
  supplement_added: "员工补充" };
const ratingNames = { good: "好评", neutral: "中评", bad: "差评" };
const decisionNames = { pending: "待审批", approved: "已批准", rejected: "已驳回", superseded: "工单已结束" };
const sourceNames = { active: "员工主动评价", automatic: "到期自动好评" };
const timeText = (value) => value == null ? "—" : new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
}).format(new Date(value * 1000));
const hasRole = (role) => state.user.roles.includes(role);
const ticketPath = (id) => `/api/v1/tickets/${encodeURIComponent(id)}`;
let messageTimer;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function message(text, error = false) {
  clearTimeout(messageTimer);
  const modal = document.querySelector("dialog[open]");
  (modal || document.querySelector("main")).append($("message"));
  $("message").hidden = false;
  $("message").textContent = text;
  $("message").classList.toggle("error", error);
  if (!error) messageTimer = setTimeout(() => { $("message").hidden = true; }, 6000);
}

function clearSession() {
  document.querySelectorAll("dialog[open]").forEach((dialog) => dialog.close());
  Object.assign(state, { token: null, user: null, ticket: null, systems: [],
    conversation: null, previousTicket: null, offset: 0, total: 0, view: "tickets", filters: {}, intakeFiles: [] });
  $("filter-form").reset();
  $("workspace").hidden = true;
  $("login-panel").hidden = false;
  $("ticket-list").replaceChildren();
  $("detail").replaceChildren();
  $("statistics").replaceChildren();
  $("ticket-form").reset();
  $("login-form").reset();
  $("employee-login-form").reset();
}

async function api(path, method = "GET", body, responseType = "json") {
  const multipart = body instanceof FormData;
  let response;
  try {
    response = await fetch(path, { method, signal: AbortSignal.timeout(multipart ? 60000 : 15000), headers: {
      ...(!multipart ? { "Content-Type": "application/json" } : {}),
      ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}),
    }, body: body === undefined ? undefined : multipart ? body : JSON.stringify(body) });
  } catch {
    throw new Error("连接失败。操作结果需刷新核对，建单可用原会话重试。");
  }
  const result = response.status === 204 ? null
    : response.ok && responseType === "blob" ? await response.blob() : await response.json();
  if (!response.ok) {
    if (response.status === 401) clearSession();
    const detail = result?.detail;
    const error = new Error(`${detail?.message || "请求失败"}（${detail?.code || response.status}）`);
    error.status = response.status;
    throw error;
  }
  return result;
}

async function uploadFiles(files) {
  if (!files.length || files.length > 5 || [...files].some((file) => !file.size || file.size > 10 * 1024 * 1024)) {
    throw new Error("请选择1～5个非空文件，单文件最多10 MiB。");
  }
  const body = new FormData();
  for (const file of files) body.append("files", file);
  return (await api("/api/v1/attachments", "POST", body)).items.map((file) => file.file_id);
}

async function downloadFile(file) {
  const blob = await api(`/api/v1/attachments/${encodeURIComponent(file.file_id)}`, "GET", undefined, "blob");
  const url = URL.createObjectURL(blob);
  const link = element("a");
  link.href = url;
  link.download = file.file_name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function run(work) {
  if (state.busy) return;
  state.busy = true;
  document.querySelector("main").inert = true;
  document.querySelector("main").setAttribute("aria-busy", "true");
  try {
    await work();
  } catch (error) {
    message(error.message, true);
  } finally {
    state.busy = false;
    document.querySelector("main").inert = false;
    document.querySelector("main").removeAttribute("aria-busy");
  }
}

function button(text, action, className = "secondary") {
  const node = element("button", text, className);
  node.type = "button";
  node.addEventListener("click", () => run(action));
  return node;
}

function updateConversation() {
  $("conversation-info").textContent = state.conversation
    ? `当前会话 ${state.conversation.id}。再次提交将返回同一张工单。`
    : state.previousTicket ? `新会话将关联旧单 ${state.previousTicket}` : "新会话将在首次提交时创建。";
  $("submit-ticket").textContent = state.conversation?.ticket_id ? "重试同会话建单" : "提交工单";
}

function newConversation(previousTicket = null) {
  $("detail-dialog").close();
  state.conversation = null;
  state.previousTicket = previousTicket;
  state.intakeFiles = [];
  $("ticket-form").reset();
  updateConversation();
  if (!$("intake").open) $("intake").showModal();
  message(previousTicket ? "已准备关联旧单的新会话，请填写新工单。" : "已准备新会话，请填写新工单。");
}

function updateIntakeProfile() {
  for (const [id, value] of [["intake-submitter", state.user.display_name],
    ["intake-department", state.user.department], ["intake-email", state.user.email]]) {
    $(id).value = value || "";
    $(id).defaultValue = value || "";
  }
  $("intake-location").options[0].textContent = state.user.location
    ? `使用账号 Base 地（${state.user.location}）` : "请选择（可选）";
  const profile = state.user.iam_profile;
  $("refresh-iam-profile").hidden = !profile?.refresh_available;
  $("intake-profile-note").textContent = profile
    ? `工号 ${profile.employee_number} · 人员资料更新于 ${timeText(profile.refreshed_at)}${profile.stale ? "，当前使用已保存资料，可更新本人资料。" : ""}`
    : "人员资料由当前登录账号带入。";
}

async function loadList() {
  const query = new URLSearchParams({ limit: 10, ...state.filters });
  query.set("offset", state.offset);
  let result = await api(`/api/v1/tickets?${query}`);
  if (state.offset && state.offset >= result.total) {
    state.offset = Math.max(0, Math.ceil(result.total / 10) - 1) * 10;
    query.set("offset", state.offset);
    result = await api(`/api/v1/tickets?${query}`);
  }
  state.total = result.total;
  $("total").textContent = `${result.total} 张`;
  $("page").textContent = `${Math.floor(state.offset / 10) + 1} / ${Math.max(1, Math.ceil(result.total / 10))}`;
  $("previous").disabled = state.offset === 0;
  $("next").disabled = state.offset + 10 >= result.total;
  const list = $("ticket-list");
  list.replaceChildren();
  if (!result.items.length) {
    const row = element("tr");
    const cell = element("td", null, "empty");
    cell.colSpan = 6;
    cell.append(element("strong", "暂无工单"), element("span", "当前授权范围及查询条件下没有工单。"));
    row.append(cell);
    list.append(row);
  }
  for (const ticket of result.items) {
    const open = async () => { state.detailView = "overview"; await loadDetail(ticket.id); await loadList(); };
    const row = element("tr");
    row.className = `ticket-row${state.ticket?.id === ticket.id ? " selected" : ""}`;
    const title = element("td");
    title.append(button(ticket.title, open, "ticket-title"), element("small", ticket.id));
    const status = element("td");
    status.append(element("span", statusNames[ticket.status], `status status-${ticket.status}`));
    if (ticket.priority) title.append(element("small", `优先级 ${ticket.priority}`));
    const operation = element("td");
    operation.append(button("查看", open, "text-button"));
    row.append(title, status,
      element("td", state.systems.find((system) => system.id === ticket.service_system_id)?.name || ticket.service_system_id),
      element("td", ticket.assignee_id || "未分派"), element("td", timeText(ticket.updated_at)), operation);
    list.append(row);
  }
}

async function loadStatistics() {
  if (!hasRole("manager")) return;
  const data = await api("/api/v1/evaluation-statistics");
  const box = $("statistics");
  box.replaceChildren(element("h2", "授权处理组评价汇总"),
    element("p", `处理组 ${data.group_ids.join("、") || "无"} · 截至 ${timeText(data.as_of)}`, "muted"));
  const metrics = element("div", null, "metrics");
  for (const [label, value] of [["已关闭", data.closed_total], ["主动评价", data.active_total],
    ["自动好评", data.automatic_total], ["未评价", data.unevaluated_total],
    ["到期待补写", data.overdue_unevaluated],
    ["主动参与率", data.participation_rate == null ? "—" : `${(data.participation_rate * 100).toFixed(1)}%`]]) {
    const item = element("div", label, "muted");
    item.append(element("strong", String(value)));
    metrics.append(item);
  }
  const table = element("table");
  const heading = element("tr");
  for (const text of ["来源", "好评", "中评", "差评"]) heading.append(element("th", text));
  table.append(heading);
  for (const source of ["active", "automatic"]) {
    const row = element("tr");
    for (const text of [sourceNames[source], data[source].good, data[source].neutral, data[source].bad]) row.append(element("td", String(text)));
    table.append(row);
  }
  box.append(metrics, table, element("p", "主动参与率为主动评价工单数 / 全部已关闭工单数，包含评价窗口内工单。到期自动好评需定期执行到期命令。", "muted"));
}

async function refresh() {
  state.user = await api("/api/v1/me");
  updateIntakeProfile();
  $("identity").textContent = state.user.display_name;
  $("roles").textContent = state.user.roles.map((role) => roleNames[role] || role).join(" · ");
  $("open-intake").hidden = !hasRole("submitter");
  $("nav-statistics").hidden = !hasRole("manager");
  state.systems = (await api("/api/v1/service-systems")).items;
  const selected = $("systems").value;
  $("systems").replaceChildren();
  for (const system of state.systems) {
    const option = element("option", system.name);
    option.value = system.id;
    $("systems").append(option);
  }
  if (state.systems.some((system) => system.id === selected)) $("systems").value = selected;
  const filterSystem = $("system-filter");
  filterSystem.replaceChildren(element("option", "全部运维系统"));
  filterSystem.firstChild.value = "";
  for (const system of state.systems) {
    const option = element("option", system.name);
    option.value = system.id;
    filterSystem.append(option);
  }
  filterSystem.value = state.filters.service_system_id || "";
  showPage(state.view);
  await loadList();
  await loadStatistics();
  if (state.ticket) await loadDetail(state.ticket.id);
}

function showPage(view) {
  state.view = view === "statistics" && hasRole("manager") ? "statistics" : "tickets";
  const statistics = state.view === "statistics";
  const title = hasRole("dispatcher") || hasRole("handler") ? "工单处理" : "我的工单";
  $("nav-ticket-label").textContent = title;
  $("page-title").textContent = statistics ? "评价统计" : title;
  $("breadcrumb-current").textContent = $("page-title").textContent;
  $("page-description").textContent = statistics ? "了解授权处理组的服务评价与参与情况。" : "集中查看工单，跟进处理进展与当前待办。";
  $("ticket-section").hidden = statistics;
  $("statistics").hidden = !statistics;
  for (const [id, active] of [["nav-tickets", !statistics], ["nav-statistics", statistics]]) {
    $(id).classList.toggle("active", active);
    if (active) $(id).setAttribute("aria-current", "page");
    else $(id).removeAttribute("aria-current");
  }
}

function facts(parent, entries) {
  const list = element("dl");
  for (const [label, value] of entries) list.append(element("dt", label), element("dd", value ?? "—"));
  parent.append(list);
}

function actionForm(parent, title, action, fields = [], method = "POST") {
  const form = element("form", null, "action-form");
  const fieldset = element("fieldset");
  fieldset.append(element("h3", title));
  for (const field of fields) {
    const label = element("label", field.label);
    const input = element(field.options ? "select" : "textarea");
    input.name = field.name;
    input.required = field.required !== false;
    if (field.options) {
      for (const [value, text] of field.options) {
        const option = element("option", text);
        option.value = value;
        input.append(option);
      }
    } else {
      input.rows = 3;
      input.maxLength = 10000;
    }
    if (field.value != null) input.value = field.value;
    label.append(input);
    fieldset.append(label);
  }
  const submit = element("button", title);
  submit.type = "submit";
  fieldset.append(submit);
  form.append(fieldset);
  const ticket = state.ticket;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form));
    if (action === "evaluation" && fields.rating !== "good" && !fields.reason.trim()) {
      message("中评和差评须填写原因。", true);
      return;
    }
    run(async () => {
      const body = { expected_version: ticket.version, ...fields };
      if (action === "confirmations") body.result_round = ticket.result_round;
      try {
        await api(`${ticketPath(ticket.id)}/${action}`, method, body);
      } catch (error) {
        if (error.status === 409) {
          await loadDetail(ticket.id);
          await loadList();
          throw new Error(`${error.message} 已重新读取工单，请检查最新状态后重新操作。`);
        }
        if ([403, 404].includes(error.status)) await refresh();
        throw error;
      }
      await loadDetail(ticket.id);
      await loadList();
      message(`${title}成功。`);
    });
  });
  parent.append(form);
}

async function loadDetail(id) {
  // Clear the previous view before fetching so a failed refresh cannot leave actionable stale data.
  state.ticket = null;
  $("detail").replaceChildren(element("p", "正在读取工单…", "muted"));
  $("intake").close();
  if (!$("detail-dialog").open) $("detail-dialog").showModal();
  try {
    state.ticket = await api(ticketPath(id));
    await renderDetail();
  } catch (error) {
    state.ticket = null;
    $("detail").replaceChildren(element("h2", "工单详情"), element("p", error.message, "muted"));
    throw error;
  }
}

async function renderDetail() {
  const ticket = state.ticket;
  const root = $("detail");
  root.replaceChildren(element("span", statusNames[ticket.status], `status status-${ticket.status}`), element("h2", ticket.title),
    element("p", ticket.id, "muted"));
  const tabs = element("nav", null, "detail-tabs");
  tabs.setAttribute("aria-label", "工单详情分区");
  const overview = element("section");
  const actions = element("section");
  const history = element("section");
  const panels = [["overview", "基本信息", overview], ["actions", "处理与评价", actions], ["history", "流转记录", history]];
  function selectPanel(key) {
    state.detailView = key;
    panels.forEach(([id, , panel], index) => {
      panel.hidden = id !== key;
      tabs.children[index].classList.toggle("active", id === key);
      tabs.children[index].setAttribute("aria-pressed", String(id === key));
    });
  }
  for (const [key, label, panel] of panels) {
    tabs.append(button(label, () => selectPanel(key), ""));
    panel.setAttribute("aria-label", label);
  }
  root.append(tabs, overview, actions, history);
  selectPanel(state.detailView);
  let box = overview;
  facts(box, [["工单编号", ticket.id], ["提交人", ticket.submitter_id], ["当前处理人", ticket.assignee_id],
    ["运维系统", state.systems.find((system) => system.id === ticket.service_system_id)?.name || ticket.service_system_id],
    ["优先级", ticket.priority], ["处理组", ticket.group_id], ["版本", String(ticket.version)], ["创建时间", timeText(ticket.created_at)],
    ["接单时间", timeText(ticket.accepted_at)], ["关闭时间", timeText(ticket.closed_at)],
    ["撤销时间", timeText(ticket.cancelled_at)], ["部门", ticket.department], ["邮箱", ticket.email],
    ["提单人 Base 地", ticket.location], ["关联旧单", ticket.previous_ticket_id]]);
  box.append(element("h3", "问题描述"), element("p", ticket.description, "prose"));
  for (const entry of ticket.history.filter((event) => event.action === "supplement_added")) {
    box.append(element("h3", "员工补充"),
      element("p", `${entry.actor_id} · ${timeText(entry.created_at)}`, "muted"),
      element("p", entry.note, "prose"));
  }
  box.append(element("h3", "附件"));
  if (!ticket.files.length) box.append(element("p", "暂无附件。", "muted"));
  for (const file of ticket.files) {
    box.append(button(`${file.file_name}（${(file.size / 1024).toFixed(1)} KiB）`,
      () => downloadFile(file), "secondary attachment-download"));
  }
  box = actions;
  const own = hasRole("submitter") && ticket.submitter_id === state.user.id;
  if (own && !["closed", "cancelled"].includes(ticket.status)) {
    const form = element("form", null, "action-form");
    const descriptionLabel = element("label", "补充说明");
    const description = element("textarea");
    description.maxLength = 10000;
    description.rows = 4;
    descriptionLabel.append(description);
    const label = element("label", "补充附件（每次最多5个，单文件最多10 MiB）");
    const input = element("input");
    input.type = "file";
    input.multiple = true;
    label.append(input);
    const submit = element("button", "提交补充");
    submit.type = "submit";
    form.append(descriptionLabel, label, submit);
    let uploaded = [];
    input.addEventListener("change", () => { uploaded = []; });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      run(async () => {
        const text = description.value.trim();
        if (!text && !input.files.length && !uploaded.length) throw new Error("请填写补充说明或选择附件");
        if (input.files.length && !uploaded.length) uploaded = await uploadFiles(input.files);
        try {
          await api(`${ticketPath(ticket.id)}/supplements`, "POST", {
            expected_version: ticket.version, description: text || null, file_ids: uploaded,
          });
        } catch (error) {
          if (error.status === 409) await loadDetail(ticket.id);
          throw error;
        }
        await loadDetail(ticket.id);
        await loadList();
        message("补充内容已保存到原工单。");
      });
    });
    box.append(form);
  }
  const handler = hasRole("handler") && ticket.assignee_id === state.user.id;
  const dispatcher = hasRole("dispatcher") && (state.user.group_scopes.dispatcher || []).includes(ticket.group_id);
  const reason = { name: "reason", label: "原因（必填）" };
  if (dispatcher && ["pending_assignment", "pending_acceptance", "in_progress"].includes(ticket.status)) {
    const candidates = (await api(`/api/v1/support-groups/${encodeURIComponent(ticket.group_id)}/handlers`)).items;
    const assigning = ticket.status === "pending_assignment";
    const options = candidates.filter((user) => assigning || user.id !== ticket.assignee_id)
      .map((user) => [user.id, `${user.display_name}（${user.id}）`]);
    if (assigning) {
      if (options.length) actionForm(box, "分派工单", "assignments",
        [{ name: "assignee_id", label: "处理人", options }]);
      else box.append(element("p", "当前组没有可用的候选处理人。", "muted"));
    } else {
      const assignee = candidates.find((user) => user.id === ticket.assignee_id);
      const progress = ticket.status === "pending_acceptance" ? "等待接单" : "正在处理";
      box.append(element("p", `已分派给 ${assignee?.display_name || ticket.assignee_id}，${progress}。`));
      if (options.length) {
        const editor = element("div");
        editor.id = "reassignment-editor";
        editor.hidden = true;
        function showEditor(open) {
          editor.hidden = !open;
          changeAssignee.setAttribute("aria-expanded", String(open));
          if (open) editor.querySelector("select").focus();
          else changeAssignee.focus();
        }
        const changeAssignee = button("改派", () => showEditor(editor.hidden));
        changeAssignee.setAttribute("aria-expanded", "false");
        changeAssignee.setAttribute("aria-controls", editor.id);
        actionForm(editor, "改派工单", "reassignments",
          [{ name: "assignee_id", label: "处理人", options }, reason]);
        editor.append(button("取消改派", () => {
          editor.querySelector("form").reset();
          showEditor(false);
        }));
        box.append(changeAssignee, editor);
      } else box.append(element("p", "当前组没有其他可用的候选处理人。", "muted"));
    }
  }
  if (handler && ticket.status === "pending_acceptance") actionForm(box, "接单", "accept");
  if (handler && ticket.status === "in_progress") {
    actionForm(box, "添加公开备注", "notes", [{ name: "note", label: "处理备注（提交人可见）" }]);
    actionForm(box, "提交处理结果", "results", [{ name: "result", label: "处理结果" }]);
  }
  if (own && ticket.status === "awaiting_confirmation") actionForm(box, "确认本轮结果", "confirmations", [
    { name: "outcome", label: `第 ${ticket.result_round} 轮确认`, options: [["unresolved", "未解决，退回处理"], ["resolved", "已解决，关闭工单"]] },
  ]);
  if (own && !ticket.cancellation_requires_approval && ["pending_assignment", "pending_acceptance"].includes(ticket.status)) actionForm(box, "撤销工单", "cancel", [reason]);
  const pending = ticket.cancellation_requests.find((request) => request.status === "pending");
  if (own && !pending && ticket.cancellation_requires_approval && ["pending_acceptance", "in_progress", "awaiting_confirmation"].includes(ticket.status)) actionForm(box, "申请撤销", "cancellation-requests", [reason]);
  if (handler && pending) actionForm(box, "审批撤销申请", `cancellation-requests/${encodeURIComponent(pending.id)}/decision`, [
    { name: "decision", label: "审批结果", options: [["rejected", "驳回，继续处理"], ["approved", "批准撤销"]] }, reason,
  ]);
  if (own && ["closed", "cancelled"].includes(ticket.status)) box.append(button("关联此单再次报障", () => newConversation(ticket.id)));
  box = history;
  box.append(element("h3", "处理结果轮次"));
  if (!ticket.results.length) box.append(element("p", "暂无处理结果。", "muted"));
  for (const result of ticket.results) {
    const entry = element("div", null, "event");
    entry.append(element("strong", `第 ${result.round} 轮 · ${result.handler_id}`), element("p", result.content),
      element("small", `${timeText(result.submitted_at)} · ${result.confirmation === "resolved" ? "已解决" : result.confirmation === "unresolved" ? "未解决" : "待确认"}`));
    box.append(entry);
  }
  box.append(element("h3", "撤销申请"));
  if (!ticket.cancellation_requests.length) box.append(element("p", "暂无撤销申请。", "muted"));
  for (const request of ticket.cancellation_requests) {
    const entry = element("div", null, "event");
    entry.append(element("strong", decisionNames[request.status]), element("p", request.reason),
      element("small", `申请 ${timeText(request.requested_at)} · 审批 ${timeText(request.decided_at)}`));
    if (request.decision_reason) entry.append(element("p", request.decision_reason));
    box.append(entry);
  }
  if (ticket.status === "closed") {
    box = actions;
    box.append(element("h3", "评价"));
    const evaluation = ticket.evaluation;
    box.append(element("p", evaluation ? `${ratingNames[evaluation.rating]} · ${sourceNames[evaluation.source]}` : "尚未评价"));
    if (evaluation?.reason) box.append(element("p", evaluation.reason, "prose"));
    box.append(element("p", `截止 ${timeText(ticket.evaluation_deadline_at)}（北京时间）· ${ticket.evaluation_locked ? "已锁定" : "窗口内可修改"}`, "muted"));
    if (own && !ticket.evaluation_locked) actionForm(box, "保存评价", "evaluation", [
      { name: "rating", label: "满意度", options: Object.entries(ratingNames), value: evaluation?.rating || "good" },
      { name: "reason", label: "评价原因（中评、差评必填）", required: false, value: evaluation?.reason || "" },
    ], "PUT");
    for (const event of ticket.evaluation_history) {
      box.append(element("p", `${timeText(event.created_at)} · ${ratingNames[event.rating]} · ${sourceNames[event.source]} · ${event.actor_id || "系统"}\n${event.reason || ""}`, "prose muted"));
    }
  }
  if (!actions.children.length) actions.append(element("p", "当前状态下没有可执行的操作。", "muted"));
  box = history;
  box.append(element("h3", "操作历史"));
  for (const event of ticket.history) {
    const entry = element("div", null, "event");
    entry.append(element("strong", actionNames[event.action] || event.action),
      element("small", ` · ${timeText(event.created_at)} · ${event.actor_id}`),
      element("p", `${statusNames[event.from_status] || "开始"} → ${statusNames[event.to_status]}`));
    if (event.to_assignee_id) entry.append(element("p", `负责人 ${event.from_assignee_id || "未分派"} → ${event.to_assignee_id}`));
    if (event.note) entry.append(element("p", event.note));
    box.append(entry);
  }
}

function submitLogin(event) {
  event.preventDefault();
  const credentials = Object.fromEntries(new FormData(event.target));
  run(async () => {
    const result = await api("/api/v1/auth/login", "POST", credentials);
    state.token = result.access_token;
    $("login-form").reset();
    $("employee-login-form").reset();
    try { await refresh(); } catch (error) { clearSession(); throw error; }
    $("login-panel").hidden = true;
    $("workspace").hidden = false;
    updateConversation();
    message(result.profile_warning || "登录成功，当前只展示此账号授权范围内的工单和操作。", Boolean(result.profile_warning));
  });
}
$("login-form").addEventListener("submit", submitLogin);
$("employee-login-form").addEventListener("submit", submitLogin);
$("refresh-iam-profile").addEventListener("click", () => run(async () => {
  await api("/api/v1/me/profile/refresh", "POST");
  await refresh();
  message("本人资料已更新，后续新工单使用更新后的资料。");
}));

$("ticket-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(event.target));
  if (!body.priority) delete body.priority;
  run(async () => {
    if (!state.conversation) state.conversation = await api("/api/v1/conversations", "POST",
      state.previousTicket ? { previous_ticket_id: state.previousTicket } : {});
    updateConversation();
    if (!state.conversation.ticket_id && $("intake-attachments").files.length && !state.intakeFiles.length) {
      state.intakeFiles = await uploadFiles($("intake-attachments").files);
    }
    body.file_ids = state.intakeFiles;
    const ticket = await api(`/api/v1/conversations/${encodeURIComponent(state.conversation.id)}/ticket`, "POST", body);
    state.conversation.ticket_id = ticket.id;
    updateConversation();
    state.offset = 0;
    state.detailView = "overview";
    await loadDetail(ticket.id);
    await loadList();
    message("已取得当前会话工单。同一会话重试会返回原工单，其他报障请开始新会话。");
  });
});

$("intake-attachments").addEventListener("change", () => { state.intakeFiles = []; });

$("refresh").addEventListener("click", () => run(async () => { await refresh(); message("工作台已刷新。"); }));
$("logout").addEventListener("click", () => run(async () => {
  await api("/api/v1/auth/logout", "POST");
  clearSession();
  message("已退出登录。");
}));
$("open-intake").addEventListener("click", () => {
  $("intake").showModal();
  $("message").hidden = true;
});
$("close-intake").addEventListener("click", () => $("intake").close());
$("save-intake-draft").addEventListener("click", () => {
  $("intake").close();
  message("草稿已保留在当前页面，刷新或退出后清除。");
});
$("close-detail").addEventListener("click", () => $("detail-dialog").close());
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("close", () => {
    if (dialog.contains($("message"))) document.querySelector("main").append($("message"));
    $("message").hidden = true;
    if (dialog.id === "detail-dialog") {
      state.ticket = null;
      document.querySelectorAll(".ticket-row.selected").forEach((row) => row.classList.remove("selected"));
    }
  });
}
$("nav-tickets").addEventListener("click", () => showPage("tickets"));
$("nav-statistics").addEventListener("click", () => run(async () => { await loadStatistics(); showPage("statistics"); }));
$("filter-form").addEventListener("submit", (event) => {
  event.preventDefault();
  run(async () => {
    state.filters = Object.fromEntries([...new FormData(event.target)].map(([key, value]) => [key, value.trim()]).filter(([, value]) => value));
    state.offset = 0;
    await loadList();
  });
});
$("reset-filters").addEventListener("click", () => run(async () => {
  $("filter-form").reset();
  state.filters = {};
  state.offset = 0;
  await loadList();
}));
$("new-conversation").addEventListener("click", () => newConversation());
$("previous").addEventListener("click", () => run(async () => { state.offset = Math.max(0, state.offset - 10); await loadList(); }));
$("next").addEventListener("click", () => run(async () => { state.offset += 10; await loadList(); }));

run(async () => {
  const options = await api("/api/v1/auth/login-options");
  const password = $("employee-login-form").elements.password;
  password.disabled = options.employee_passwordless;
  password.required = !options.employee_passwordless;
  $("employee-password-label").hidden = options.employee_passwordless;
  $("employee-login-note").textContent = options.employee_passwordless
    ? "免密测试模式：输入工号即可进入，首次登录会查询 IAM 人员资料。"
    : "使用已导入的员工账号和设置的密码登录。";
  const result = await api("/api/v1/auth/test-accounts");
  const select = $("test-accounts");
  const placeholder = element("option", result.items.length ? "请选择测试账号" : "暂无可用测试账号，请先执行初始化命令");
  placeholder.value = "";
  select.replaceChildren(placeholder);
  for (const account of result.items) {
    const option = element("option", `${account.display_name}（${account.username}）`);
    option.value = account.username;
    select.append(option);
  }
});
