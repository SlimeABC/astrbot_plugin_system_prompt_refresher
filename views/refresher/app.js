/**
 * astrbot_plugin_system_prompt_refresher —— WebUI 视图
 *
 * 列出对话，支持按机器人、私聊/群聊、会话筛选，按最近更新/消息数等排序，
 * 可切换按会话分组；从 WebUI 刷新系统提示词、另存 xxx_backup 备份、清除快照。
 * 所有需要 Dashboard 身份的操作都通过 window.AstrBotPluginView bridge。
 */

const bridge = window.AstrBotPluginView;

const els = {
  heading: document.getElementById("heading"),
  subtitle: document.getElementById("subtitle"),
  refresh: document.getElementById("refresh"),
  search: document.getElementById("search"),
  searchLabel: document.getElementById("search-label"),
  bots: document.getElementById("bots"),
  botsLabel: document.getElementById("bots-label"),
  types: document.getElementById("types"),
  typesLabel: document.getElementById("types-label"),
  sort: document.getElementById("sort"),
  sortLabel: document.getElementById("sort-label"),
  direction: document.getElementById("direction"),
  group: document.getElementById("group"),
  groupLabel: document.getElementById("group-label"),
  listTitle: document.getElementById("list-title"),
  listCount: document.getElementById("list-count"),
  listHint: document.getElementById("list-hint"),
  stats: document.getElementById("stats"),
  list: document.getElementById("list"),
  status: document.getElementById("status"),
  confirmDialog: document.getElementById("confirm-dialog"),
  confirmTitle: document.getElementById("confirm-title"),
  confirmMessage: document.getElementById("confirm-message"),
  confirmField: document.getElementById("confirm-field"),
  confirmFieldLabel: document.getElementById("confirm-field-label"),
  confirmTextarea: document.getElementById("confirm-textarea"),
  confirmInputField: document.getElementById("confirm-input-field"),
  confirmInputLabel: document.getElementById("confirm-input-label"),
  confirmInput: document.getElementById("confirm-input"),
  confirmCancel: document.getElementById("confirm-cancel"),
  confirmOk: document.getElementById("confirm-ok"),
};

const SORT_OPTIONS = [
  ["updated", "sort_updated", "最近更新"],
  ["messages", "sort_messages", "消息数"],
  ["chars", "sort_chars", "字符数"],
  ["title", "sort_title", "标题"],
  ["created", "sort_created", "创建时间"],
];

const state = {
  items: [],
  bots: [],
  totals: null,
  config: null,
  ui: { theme_color: "#22c55e" },
  filters: {
    query: "",
    bots: new Set(),
    types: new Set(),
  },
  sort: "updated",
  direction: "desc",
  groupBySession: false,
  expandedGroups: new Set(),
  loading: false,
  busy: false,
  refreshTimer: null,
  autoRefresh: { enabled: true, intervalMinutes: 60 },
};

let persistTimer = null;

function t(key, fallback) {
  if (bridge && typeof bridge.t === "function") {
    return bridge.t(`views.refresher.${key}`, fallback);
  }
  return fallback;
}

function tFormat(key, fallback, ...args) {
  let text = t(key, fallback);
  for (const arg of args) {
    text = text.replace("%s", arg);
  }
  return text;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => {
    switch (char) {
      case "&":
        return "&amp;";
      case "<":
        return "&lt;";
      case ">":
        return "&gt;";
      case '"':
        return "&quot;";
      case "'":
        return "&#39;";
      default:
        return char;
    }
  });
}

function fmtNumber(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "0";
  }
  return new Intl.NumberFormat().format(number);
}

function fmtTime(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) {
    return "—";
  }
  return new Date(value * 1000).toLocaleString();
}

function fmtRelative(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) {
    return "—";
  }
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - value);
  if (diff < 60) {
    return t("just_now", "刚刚");
  }
  if (diff < 3600) {
    return tFormat("minutes_ago", "%s 分钟前", Math.floor(diff / 60));
  }
  if (diff < 86400) {
    return tFormat("hours_ago", "%s 小时前", Math.floor(diff / 3600));
  }
  return tFormat("days_ago", "%s 天前", Math.floor(diff / 86400));
}

function chatTypeLabel(type) {
  if (type === "group") {
    return t("group", "群聊");
  }
  if (type === "private") {
    return t("private", "私聊");
  }
  return t("unknown_type", "其他");
}

function botLabel(bot) {
  const platformId = String(bot?.platform_id || "").trim();
  const name = String(bot?.name || bot?.bot_name || "").trim();
  const lower = platformId.toLowerCase();
  if (platformId && lower !== "default" && lower !== "unknown") {
    return platformId;
  }
  if (name && platformId && name !== platformId) {
    return `${name} (${platformId})`;
  }
  return name || platformId || t("unknown_bot", "未知机器人");
}

function sessionLabel(session) {
  const parts = [];
  const bot = botLabel(session);
  if (bot) {
    parts.push(bot);
  }
  parts.push(chatTypeLabel(session.chat_type));
  const sessionId = session.session_id || session.umo || "—";
  const sessionName = String(session.session_name || "").trim();
  parts.push(sessionName ? `${sessionId}(${sessionName})` : sessionId);
  return parts.join(" ");
}

function setStatus(text, kind = "") {
  els.status.textContent = text || "";
  els.status.classList.toggle("is-ok", kind === "ok");
  els.status.classList.toggle("is-error", kind === "error");
}

function hexToRgba(hex, alpha) {
  let value = String(hex || "")
    .replace("#", "")
    .trim();
  if (value.length === 3) {
    value = value
      .split("")
      .map((char) => char + char)
      .join("");
  }
  if (!/^[0-9a-fA-F]{6}$/.test(value)) {
    return "";
  }
  const red = parseInt(value.slice(0, 2), 16);
  const green = parseInt(value.slice(2, 4), 16);
  const blue = parseInt(value.slice(4, 6), 16);
  return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
}

function applyThemeColor() {
  const color = state.ui?.theme_color || "#22c55e";
  const root = document.documentElement;
  root.style.setProperty("--accent", color);
  const isDark = root.dataset.theme === "dark";
  const soft = hexToRgba(color, isDark ? 0.18 : 0.12);
  if (soft) {
    root.style.setProperty("--accent-soft", soft);
  }
}

function applyTheme() {
  let isDark = false;
  try {
    isDark = Boolean(bridge?.getContext?.()?.isDark);
  } catch {
    isDark = false;
  }
  document.documentElement.dataset.theme = isDark ? "dark" : "light";
  applyThemeColor();
}

function updateDirectionButton() {
  els.direction.textContent = state.direction === "desc" ? "↓" : "↑";
  els.direction.setAttribute(
    "aria-label",
    t(
      state.direction === "desc" ? "sort_desc" : "sort_asc",
      state.direction === "desc" ? "降序" : "升序",
    ),
  );
}

function renderSortOptions() {
  const current = state.sort;
  els.sort.innerHTML = SORT_OPTIONS.map(
    ([value, key, fallback]) =>
      `<option value="${value}">${escapeHtml(t(key, fallback))}</option>`,
  ).join("");
  els.sort.value = current;
}

function renderTypeButtons() {
  els.types.querySelectorAll("button[data-type]").forEach((button) => {
    const type = button.dataset.type;
    button.textContent = chatTypeLabel(type);
    button.classList.toggle("is-active", state.filters.types.has(type));
  });
}

function renderLabels() {
  const heading = t("heading", "系统提示词刷新");
  document.title = heading;
  els.heading.textContent = heading;
  els.subtitle.textContent = t("subtitle", "刷新系统提示词，或备份当前对话");
  els.refresh.textContent = t("refresh", "刷新");
  els.searchLabel.textContent = t("search_label", "搜索关键词");
  els.search.placeholder = t("search_placeholder", "搜索标题、昵称或 ID…");
  els.botsLabel.textContent = t("bots_label", "机器人");
  els.typesLabel.textContent = t("types_label", "对话类型");
  els.sortLabel.textContent = t("sort_label", "排序");
  els.listTitle.textContent = t("list_title", "对话");
  if (els.listHint) {
    els.listHint.textContent = t(
      "list_hint",
      "“刷新提示词”会把当前系统提示词重新写入该对话，并保留聊天记录；提示词需要先在这个对话里发一条消息才会被记录",
    );
  }
  els.groupLabel.textContent = t("group_by_session", "按会话分组");
  els.group.checked = state.groupBySession;
  renderSortOptions();
  renderTypeButtons();
  updateDirectionButton();
}

function applyPreferences(prefs) {
  if (!prefs || typeof prefs !== "object") {
    return;
  }
  state.filters.query = String(prefs.query || "").slice(0, 200);
  state.filters.bots = new Set(
    Array.isArray(prefs.bots)
      ? prefs.bots.filter((value) => typeof value === "string" && value)
      : [],
  );
  state.filters.types = new Set(
    Array.isArray(prefs.types)
      ? prefs.types.filter(
          (value) => value === "private" || value === "group",
        )
      : [],
  );
  const sortValues = new Set(SORT_OPTIONS.map(([value]) => value));
  state.sort = sortValues.has(prefs.sort) ? prefs.sort : "updated";
  state.direction = prefs.direction === "asc" ? "asc" : "desc";
  state.groupBySession = Boolean(prefs.group_by_session);
  if (state.groupBySession) {
    state.expandedGroups.clear();
  }
  els.search.value = state.filters.query;
  renderLabels();
  renderTypeButtons();
}

async function loadPreferences() {
  if (!bridge || typeof bridge.apiGet !== "function") {
    return;
  }
  try {
    applyPreferences(await bridge.apiGet("preferences"));
  } catch {
    // 读取失败时使用默认筛选
  }
}

function persistPreferences() {
  if (!bridge || typeof bridge.apiPost !== "function") {
    return;
  }
  clearTimeout(persistTimer);
  persistTimer = setTimeout(async () => {
    try {
      await bridge.apiPost("preferences", {
        query: state.filters.query,
        bots: [...state.filters.bots],
        types: [...state.filters.types],
        sort: state.sort,
        direction: state.direction,
        group_by_session: state.groupBySession,
      });
    } catch {
      // 保存失败不影响使用
    }
  }, 300);
}

function renderStats() {
  const totals = state.totals || {};
  const cards = [
    ["stat_conversations", "对话", totals.conversations],
    ["stat_sessions", "会话", totals.sessions],
    ["stat_with_snapshot", "有刷新快照", totals.with_snapshot],
    ["stat_refreshable", "提示词已记录", totals.refreshable],
  ];
  els.stats.innerHTML = cards
    .map(
      ([labelKey, fallback, value]) =>
        `<div class="stat"><span class="stat-value">${fmtNumber(
          value,
        )}</span><span class="stat-label">${escapeHtml(
          t(labelKey, fallback),
        )}</span></div>`,
    )
    .join("");
}

function renderBotList() {
  if (!state.bots.length) {
    els.bots.innerHTML = `<span class="bot-empty">${escapeHtml(
      t("no_bots", "暂无机器人"),
    )}</span>`;
    return;
  }
  els.bots.innerHTML = state.bots
    .map((bot) => {
      const checked = state.filters.bots.has(bot.platform_id);
      return `<label class="bot-filter"><input type="checkbox" value="${escapeHtml(
        bot.platform_id,
      )}"${
        checked ? " checked" : ""
      }><span class="bot-name">${escapeHtml(
        botLabel(bot),
      )}</span><span class="bot-count">${fmtNumber(
        bot.conversations,
      )}</span></label>`;
    })
    .join("");
}

function promptInfo(item) {
  if (!item.can_refresh) {
    return t("prompt_not_cached", "尚未记录系统提示词");
  }
  return tFormat(
    "prompt_cached",
    "已缓存 %s 字符 · %s",
    fmtNumber(item.prompt_chars),
    fmtRelative(item.prompt_cached_at),
  );
}

function shortConversationId(cid) {
  const value = String(cid || "").trim();
  if (!value) {
    return "";
  }
  // 对话 ID 是 uuid，去掉连字符后取前 4 位，例如 a1b2。
  return value.replace(/-/g, "").slice(0, 4);
}

function conversationTitle(item) {
  const base = item?.title || t("untitled", "未命名对话");
  const cidShort = shortConversationId(item?.cid);
  return cidShort ? `${base}(${cidShort})` : base;
}

async function copyText(text) {
  const value = String(text || "");
  if (!value) {
    return false;
  }
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return true;
    }
  } catch {
    // 非安全上下文或权限被拒绝时回退到 execCommand
  }
  try {
    const area = document.createElement("textarea");
    area.value = value;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.top = "-1000px";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

function renderItem(item, index = 0) {
  const title = item.title || t("untitled", "未命名对话");
  const cidShort = shortConversationId(item.cid);
  const idButton = cidShort
    ? `<button type="button" class="session-id" data-copy-cid="${escapeHtml(
        item.cid,
      )}" title="${escapeHtml(
        t("copy_id_hint", "点击复制完整对话 ID"),
      )}">${escapeHtml(cidShort)}</button>`
    : "";
  const meta = escapeHtml(sessionLabel(item));

  const badges = [];
  if (item.is_current) {
    badges.push(
      `<span class="badge badge-accent">${escapeHtml(
        t("current", "当前对话"),
      )}</span>`,
    );
  }
  if (item.has_snapshot) {
    badges.push(
      `<span class="badge">${escapeHtml(t("has_snapshot", "有刷新快照"))}</span>`,
    );
  }
  if (item.is_backup) {
    badges.push(
      `<span class="badge">${escapeHtml(t("backup", "备份"))}</span>`,
    );
  }
  if (item.can_refresh) {
    badges.push(
      `<span class="badge badge-ok">${escapeHtml(
        t("refreshable", "提示词已记录"),
      )}</span>`,
    );
  }

  const snapshotText = item.has_snapshot
    ? tFormat(
        "snapshot_chars",
        "刷新快照 %s 字符",
        fmtNumber(item.snapshot_chars),
      )
    : t("no_snapshot", "无刷新快照");

  const refreshTitle = item.can_refresh
    ? ""
    : ` title="${escapeHtml(
        t(
          "refresh_disabled_hint",
          "请先在对话中发一条消息，让插件记录系统提示词",
        ),
      )}"`;

  return `<article class="session" data-key="${escapeHtml(
    item.key,
  )}" style="animation-delay: ${Math.min(index, 10) * 18}ms">
  <div class="session-head">
    <div class="session-main">
      <div class="session-title-row">
        <span class="session-title" title="${escapeHtml(
          title,
        )}">${escapeHtml(title)}</span>
        ${badges.join("")}
      </div>
      <div class="session-meta">${meta}</div>
      <div class="session-stats">
        <span>${escapeHtml(
          tFormat("messages", "%s 条消息", fmtNumber(item.messages)),
        )}</span>
        <span>${escapeHtml(
          tFormat("chars", "%s 字符", fmtNumber(item.chars)),
        )}</span>
        <span>${escapeHtml(snapshotText)}</span>
        <span>${escapeHtml(promptInfo(item))}</span>
        <span>${escapeHtml(fmtTime(item.updated_at))}</span>
      </div>
    </div>
    ${idButton}
  </div>
  <div class="session-actions">
    <button type="button" data-action="refresh" class="${
      item.can_refresh ? "" : "is-muted"
    }"${refreshTitle}>${escapeHtml(
      t("action_refresh", "刷新提示词"),
    )}</button>
    <button type="button" data-action="copy">${escapeHtml(
      t("action_copy", "备份对话"),
    )}</button>
    <button type="button" data-action="clear" class="is-danger"${
      item.has_snapshot ? "" : " disabled"
    } title="${escapeHtml(
      t(
        "action_clear_hint",
        "删除刷新时写入的系统提示词快照，相当于撤销本次刷新；不会删除聊天记录、修改人格配置或删除备份对话",
      ),
    )}">${escapeHtml(t("action_clear", "清除快照"))}</button>
    <button type="button" data-action="delete" class="is-danger">${escapeHtml(
      t("action_delete", "删除对话"),
    )}</button>
  </div>
</article>`;
}

function sortValue(item, key) {
  switch (key) {
    case "messages":
      return Number(item.messages) || 0;
    case "chars":
      return Number(item.chars) || 0;
    case "title":
      return String(item.title || "").toLowerCase();
    case "created":
      return Number(item.created_at) || 0;
    case "updated":
    default:
      return Number(item.updated_at) || 0;
  }
}

function sortItems(items) {
  const direction = state.direction === "asc" ? 1 : -1;
  const sorted = [...items].sort((a, b) => {
    const av = sortValue(a, state.sort);
    const bv = sortValue(b, state.sort);
    if (typeof av === "string" || typeof bv === "string") {
      const result = String(av).localeCompare(String(bv), "zh-CN");
      if (result !== 0) {
        return result * direction;
      }
    } else {
      const result = av - bv;
      if (result !== 0) {
        return result * direction;
      }
    }
    return (Number(b.updated_at) || 0) - (Number(a.updated_at) || 0);
  });
  // 当前对话始终排在第一位，其余保持用户选择的排序
  return [
    ...sorted.filter((item) => item.is_current),
    ...sorted.filter((item) => !item.is_current),
  ];
}

function filteredItems() {
  const query = state.filters.query.trim().toLowerCase();
  let items = state.items;
  if (state.filters.bots.size) {
    items = items.filter((item) =>
      state.filters.bots.has(item.platform_id),
    );
  }
  if (state.filters.types.size) {
    items = items.filter((item) => state.filters.types.has(item.chat_type));
  }
  if (query) {
    items = items.filter((item) =>
      [
        item.title,
        item.umo,
        item.cid,
        item.session_id,
        item.platform_id,
        item.bot_name,
        item.session_name,
      ].some((value) => String(value || "").toLowerCase().includes(query)),
    );
  }
  return sortItems(items);
}

function renderList() {
  const items = filteredItems();
  els.listCount.textContent = tFormat(
    "conversation_count",
    "共 %s 条",
    fmtNumber(items.length),
  );

  if (!state.items.length) {
    els.list.innerHTML = `<div class="empty"><strong>${escapeHtml(
      t("empty", "暂无对话记录"),
    )}</strong>${escapeHtml(
      t("empty_hint", "AstrBot 产生对话后，这里会显示可以刷新提示词或备份的对话"),
    )}</div>`;
    return;
  }

  if (!items.length) {
    els.list.innerHTML = `<div class="empty"><strong>${escapeHtml(
      t("no_match", "没有匹配的对话"),
    )}</strong>${escapeHtml(t("no_match_hint", "换个关键词试试"))}</div>`;
    return;
  }

  if (!state.groupBySession) {
    els.list.innerHTML = items
      .map((item, index) => renderItem(item, index))
      .join("");
    return;
  }

  const groups = new Map();
  for (const item of items) {
    const key = item.umo || item.key;
    if (!groups.has(key)) {
      groups.set(key, { session: item, items: [] });
    }
    groups.get(key).items.push(item);
  }
  const grouped = [...groups.values()].sort((a, b) => {
    const at = Math.max(
      ...a.items.map((item) => Number(item.updated_at) || 0),
    );
    const bt = Math.max(
      ...b.items.map((item) => Number(item.updated_at) || 0),
    );
    return bt - at;
  });
  // 当前对话所在的组始终排在最前
  const currentGroupIndex = grouped.findIndex((group) =>
    group.items.some((item) => item.is_current),
  );
  if (currentGroupIndex > 0) {
    const [currentGroup] = grouped.splice(currentGroupIndex, 1);
    grouped.unshift(currentGroup);
  }
  els.list.innerHTML = grouped
    .map((group) => {
      const session = group.session;
      const groupKey = session.umo || session.key;
      const expanded = state.expandedGroups.has(groupKey);
      return `<section class="conversation-group${
        expanded ? "" : " is-collapsed"
      }">
  <button type="button" class="group-head" data-group-key="${escapeHtml(
    groupKey,
  )}" aria-expanded="${expanded ? "true" : "false"}" title="${escapeHtml(
    t("group_toggle_hint", "点击展开或折叠该会话"),
  )}">
    <span class="group-chevron" aria-hidden="true">›</span>
    <span class="group-title">${escapeHtml(sessionLabel(session))}</span>
    <span class="group-meta">${escapeHtml(
      tFormat("group_count", "%s 条对话", fmtNumber(group.items.length)),
    )}</span>
  </button>
  <div class="group-list"${
    expanded ? "" : ' aria-hidden="true" inert'
  }><div class="group-list-inner">${group.items
    .map((item, index) => renderItem(item, index))
    .join("")}</div></div>
</section>`;
    })
    .join("");
}

async function load(options = {}) {
  if (!bridge) {
    setStatus(
      t(
        "bridge_error",
        "AstrBotPluginView bridge 未注入，请在 AstrBot WebUI 中打开此页面",
      ),
      "error",
    );
    return;
  }
  if (state.loading) {
    return;
  }
  state.loading = true;
  if (!options.silent && !options.background) {
    setStatus(t("loading", "正在读取对话…"));
  }
  let fromCache = false;
  try {
    const payload = await bridge.apiGet(
      "conversations",
      options.force ? { refresh: 1 } : {},
    );
    state.items = Array.isArray(payload?.items) ? payload.items : [];
    state.bots = Array.isArray(payload?.bots) ? payload.bots : [];
    state.totals = payload?.totals || null;
    state.config = payload?.config || null;
    state.ui = payload?.ui || state.ui;
    state.autoRefresh = {
      enabled: payload?.refresh?.enabled !== false,
      intervalMinutes: Math.max(
        1,
        Number(payload?.refresh?.interval_minutes) || 60,
      ),
    };
    setupAutoRefresh(state.autoRefresh);
    fromCache = Boolean(payload?.cached) && !options.force;

    const botIds = new Set(state.bots.map((bot) => bot.platform_id));
    for (const id of [...state.filters.bots]) {
      if (!botIds.has(id)) {
        state.filters.bots.delete(id);
      }
    }

    applyThemeColor();
    renderBotList();
    renderTypeButtons();
    renderStats();
    renderList();
    if (options.background) {
      setStatus(t("updated", "已更新"), "ok");
    } else if (!options.silent) {
      if (fromCache) {
        setStatus(t("loading_cached", "已显示缓存，正在后台更新…"));
      } else {
        setStatus(
          tFormat("loaded", "已读取 %s 个对话", fmtNumber(state.items.length)),
          "ok",
        );
      }
    }
  } catch (error) {
    setStatus(
      `${t("error", "读取失败")}: ${error?.message || error}`,
      "error",
    );
  } finally {
    state.loading = false;
  }

  if (fromCache) {
    setTimeout(() => {
      load({ silent: true, force: true, background: true });
    }, 0);
  }
}

function setupAutoRefresh(config) {
  if (state.refreshTimer) {
    clearInterval(state.refreshTimer);
    state.refreshTimer = null;
  }
  const enabled = config?.enabled !== false;
  const minutes = Math.max(1, Number(config?.intervalMinutes) || 60);
  if (!enabled) {
    return;
  }
  state.refreshTimer = setInterval(() => {
    if (!state.loading && !state.busy) {
      load({ silent: true, force: true, background: true });
    }
  }, minutes * 60 * 1000);
}

function showNotice(title, message) {
  const dialog = els.confirmDialog;
  if (!dialog || typeof dialog.showModal !== "function") {
    window.alert(`${title}\n\n${message}`);
    return;
  }
  els.confirmTitle.textContent = title;
  els.confirmMessage.textContent = message;
  els.confirmCancel.hidden = true;
  els.confirmOk.textContent = t("confirm_ok_notice", "知道了");
  els.confirmOk.classList.remove("is-danger");
  els.confirmField.hidden = true;
  els.confirmTextarea.value = "";
  dialog.returnValue = "";
  dialog.onclose = () => {
    dialog.onclose = null;
    els.confirmCancel.hidden = false;
  };
  els.confirmOk.onclick = () => {
    dialog.returnValue = "ok";
    dialog.close("ok");
  };
  dialog.showModal();
}

async function confirmAction(action, item) {
  const title = conversationTitle(item);
  const backupEnabled = state.config?.backup_before_refresh !== false;
  const configs = {
    refresh: {
      title: t("confirm_title_refresh", "刷新系统提示词"),
      message: backupEnabled
        ? tFormat(
            "confirm_message_refresh",
            "将先备份对话“%s”，再把最新系统提示词快照写入该对话。不会在聊天中发送消息。刷新后的几条消息缓存命中率较低，可能产生额外费用",
            title,
          )
        : tFormat(
            "confirm_message_refresh_no_backup",
            "将把最新系统提示词快照写入对话“%s”。不会在聊天中发送消息。刷新后的几条消息缓存命中率较低，可能产生额外费用",
            title,
          ),
      ok: t("confirm_ok_refresh", "确认刷新"),
      danger: false,
    },
    copy: {
      title: t("confirm_title_copy", "备份对话"),
      message: tFormat(
        "confirm_message_copy",
        "将把对话“%s”另存为备份，原对话保持不变",
        title,
      ),
      ok: t("confirm_ok_copy", "确认备份"),
      danger: false,
      input: {
        label: t("backup_title_label", "备份名称"),
        value: `${item.title || t("untitled", "未命名对话")}_backup`,
        placeholder: t("backup_title_placeholder", "输入备份后的对话名称"),
      },
    },
    delete: {
      title: t("confirm_title_delete", "删除对话"),
      message: tFormat(
        "confirm_message_delete",
        "将永久删除对话“%s”及其全部聊天记录，无法恢复",
        title,
      ),
      ok: t("confirm_ok_delete", "确认删除"),
      danger: true,
      requireDoubleClick: true,
    },
    clear: {
      title: t("confirm_title_clear", "清除快照"),
      message: t(
        "confirm_message_clear",
        "将删除该对话中由刷新写入的系统提示词快照。不会删除聊天记录，也不会修改人格或系统提示词配置",
      ),
      ok: t("confirm_ok_clear", "确认清除"),
      danger: true,
    },
  };
  const config = configs[action];
  if (!config) {
    return { confirmed: true };
  }

  if (action === "refresh") {
    try {
      const info = await bridge.apiGet("prompt", {
        umo: item.umo,
        cid: item.cid,
      });
      const persona = String(info?.persona_prompt || "");
      if (info?.persona_found && persona) {
        config.textarea = {
          label: t("persona_label", "人格提示词片段"),
          value: persona,
        };
        config.message = backupEnabled
          ? tFormat(
              "confirm_message_refresh_edit",
              "将先备份对话“%s”，然后只替换下方人格提示词片段并写入快照；其他系统提示词保持不变，不会在聊天中发送消息。刷新后的几条消息缓存命中率较低，可能产生额外费用",
              title,
            )
          : tFormat(
              "confirm_message_refresh_edit_no_backup",
              "将只替换对话“%s”下方的人格提示词片段并写入快照；其他系统提示词保持不变，不会在聊天中发送消息。刷新后的几条消息缓存命中率较低，可能产生额外费用",
              title,
            );
      } else {
        config.message += `\n\n${t(
          "persona_unavailable",
          "未找到人格提示词片段，将使用完整系统提示词刷新",
        )}`;
      }
    } catch {
      config.message += `\n\n${t(
        "persona_unavailable",
        "未找到人格提示词片段，将使用完整系统提示词刷新",
      )}`;
    }
  }

  const dialog = els.confirmDialog;
  if (!dialog || typeof dialog.showModal !== "function") {
    if (config.textarea) {
      const value = window.prompt(
        `${config.title}\n\n${config.message}`,
        config.textarea.value || "",
      );
      return {
        confirmed: value !== null,
        personaPrompt: value ?? undefined,
      };
    }
    if (config.input) {
      const value = window.prompt(
        `${config.title}\n\n${config.message}`,
        config.input.value || "",
      );
      return {
        confirmed: value !== null,
        inputValue: value ?? undefined,
      };
    }
    return {
      confirmed: window.confirm(`${config.title}\n\n${config.message}`),
    };
  }

  els.confirmTitle.textContent = config.title;
  els.confirmMessage.textContent = config.message;
  els.confirmCancel.hidden = false;
  els.confirmCancel.textContent = t("confirm_cancel", "取消");
  els.confirmOk.textContent = config.ok;
  els.confirmOk.classList.toggle("is-danger", Boolean(config.danger));
  if (config.textarea) {
    els.confirmField.hidden = false;
    els.confirmFieldLabel.textContent = config.textarea.label;
    els.confirmTextarea.value = config.textarea.value;
  } else {
    els.confirmField.hidden = true;
    els.confirmTextarea.value = "";
  }
  if (config.input) {
    els.confirmInputField.hidden = false;
    els.confirmInputLabel.textContent = config.input.label;
    els.confirmInput.value = config.input.value || "";
    els.confirmInput.placeholder = config.input.placeholder || "";
  } else {
    els.confirmInputField.hidden = true;
    els.confirmInput.value = "";
  }
  dialog.returnValue = "";

  return new Promise((resolve) => {
    let armed = false;
    let armTimer = null;
    const resetArmed = () => {
      armed = false;
      if (armTimer) {
        clearTimeout(armTimer);
        armTimer = null;
      }
      els.confirmOk.textContent = config.ok;
      els.confirmOk.classList.remove("is-armed");
    };
    dialog.onclose = () => {
      resetArmed();
      const confirmed = dialog.returnValue === "confirm";
      const personaPrompt = config.textarea
        ? els.confirmTextarea.value
        : undefined;
      const inputValue = config.input ? els.confirmInput.value : undefined;
      dialog.onclose = null;
      resolve({ confirmed, personaPrompt, inputValue });
    };
    els.confirmCancel.onclick = () => {
      dialog.returnValue = "cancel";
      dialog.close("cancel");
    };
    els.confirmOk.onclick = () => {
      if (config.requireDoubleClick && !armed) {
        armed = true;
        els.confirmOk.textContent = t(
          "confirm_ok_delete_again",
          "再次点击确认删除",
        );
        els.confirmOk.classList.add("is-armed");
        armTimer = setTimeout(resetArmed, 3000);
        return;
      }
      resetArmed();
      dialog.returnValue = "confirm";
      dialog.close("confirm");
    };
    dialog.showModal();
  });
}

async function runAction(action, item, button) {
  if (state.busy) {
    return;
  }
  if (action === "refresh" && !item.can_refresh) {
    // 列表可能来自缓存或发消息前的旧数据，先强制重新读一次再判断，
    // 避免明明已经记录过系统提示词却提示“尚未记录”。
    if (!state.loading) {
      await load({ force: true, silent: true });
      const fresh = state.items.find(
        (candidate) =>
          candidate.umo === item.umo && candidate.cid === item.cid,
      );
      if (fresh) {
        item = fresh;
      }
    }
  }
  if (action === "refresh" && !item.can_refresh) {
    const title = conversationTitle(item);
    const message = tFormat(
      "refresh_need_record",
      "“%s”还没有记录系统提示词，请先在这个对话里发一条消息再刷新",
      title,
    );
    setStatus(message, "error");
    showNotice(
      t("refresh_need_record_title", "尚未记录系统提示词"),
      message,
    );
    return;
  }
  const confirmation = await confirmAction(action, item);
  if (!confirmation?.confirmed || state.busy) {
    return;
  }
  const endpoints = {
    refresh: "refresh",
    copy: "copy",
    delete: "delete-conversation",
    clear: "clear-snapshot",
  };
  const endpoint = endpoints[action];
  if (!endpoint) {
    return;
  }

  state.busy = true;
  const label = button?.textContent || "";
  button?.classList.add("is-loading");
  if (button) {
    button.disabled = true;
  }
  setStatus(t("working", "正在处理…"));

  try {
    const body = { umo: item.umo, cid: item.cid };
    if (action === "copy" && typeof confirmation.inputValue === "string") {
      body.title = confirmation.inputValue;
    }
    if (
      action === "refresh" &&
      typeof confirmation.personaPrompt === "string"
    ) {
      body.persona_prompt = confirmation.personaPrompt;
    }
    const response = await bridge.apiPost(endpoint, body);
    setStatus(response?.message || t("done", "操作完成"), "ok");
    await load({ silent: true });
  } catch (error) {
    const message = error?.message || String(error);
    setStatus(message, "error");
    showNotice(t("op_failed_title", "操作失败"), message);
  } finally {
    state.busy = false;
    button?.classList.remove("is-loading");
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}

function bindEvents() {
  els.search.addEventListener("input", () => {
    state.filters.query = els.search.value;
    renderList();
    persistPreferences();
  });

  els.refresh.addEventListener("click", async () => {
    if (state.loading) {
      return;
    }
    els.refresh.classList.add("is-loading");
    els.refresh.disabled = true;
    try {
      await load({ force: true });
    } finally {
      els.refresh.classList.remove("is-loading");
      els.refresh.disabled = false;
    }
  });

  els.bots.addEventListener("change", (event) => {
    const input = event.target.closest('input[type="checkbox"]');
    if (!input) {
      return;
    }
    if (input.checked) {
      state.filters.bots.add(input.value);
    } else {
      state.filters.bots.delete(input.value);
    }
    renderList();
    persistPreferences();
  });

  els.types.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-type]");
    if (!button) {
      return;
    }
    const type = button.dataset.type;
    if (state.filters.types.has(type)) {
      state.filters.types.delete(type);
    } else {
      state.filters.types.add(type);
    }
    renderTypeButtons();
    renderList();
    persistPreferences();
  });

  els.sort.addEventListener("change", () => {
    state.sort = els.sort.value;
    renderList();
    persistPreferences();
  });

  els.direction.addEventListener("click", () => {
    state.direction = state.direction === "desc" ? "asc" : "desc";
    updateDirectionButton();
    renderList();
    persistPreferences();
  });

  els.group.addEventListener("change", () => {
    state.groupBySession = els.group.checked;
    if (state.groupBySession) {
      state.expandedGroups.clear();
    }
    renderList();
    persistPreferences();
  });

  els.list.addEventListener("click", async (event) => {
    const idButton = event.target.closest("button[data-copy-cid]");
    if (idButton) {
      const cid = idButton.dataset.copyCid || "";
      const copied = await copyText(cid);
      if (copied) {
        setStatus(t("id_copied", "已复制对话 ID"), "ok");
        const label = idButton.textContent;
        idButton.textContent = t("id_copied_short", "已复制");
        idButton.classList.add("is-copied");
        setTimeout(() => {
          idButton.textContent = label;
          idButton.classList.remove("is-copied");
        }, 1200);
      } else {
        setStatus(
          t("id_copy_failed", "复制失败，请手动选择对话 ID"),
          "error",
        );
      }
      return;
    }
    const groupHead = event.target.closest("button.group-head");
    if (groupHead) {
      const key = groupHead.dataset.groupKey;
      if (!key) {
        return;
      }
      const section = groupHead.closest(".conversation-group");
      const list = section?.querySelector(".group-list");
      if (!section || !list) {
        return;
      }
      const expanded = !state.expandedGroups.has(key);
      if (expanded) {
        state.expandedGroups.add(key);
      } else {
        state.expandedGroups.delete(key);
      }
      groupHead.setAttribute(
        "aria-expanded",
        expanded ? "true" : "false",
      );
      section.classList.toggle("is-collapsed", !expanded);
      if (expanded) {
        list.removeAttribute("aria-hidden");
        list.removeAttribute("inert");
      } else {
        list.setAttribute("aria-hidden", "true");
        list.setAttribute("inert", "");
      }
      return;
    }
    const button = event.target.closest("button[data-action]");
    if (!button || button.disabled) {
      return;
    }
    const card = button.closest(".session");
    const item = state.items.find((entry) => entry.key === card?.dataset.key);
    if (!item) {
      return;
    }
    runAction(button.dataset.action, item, button);
  });
}

async function init() {
  renderLabels();
  bindEvents();
  applyTheme();

  if (!bridge) {
    setStatus(
      t(
        "bridge_error",
        "AstrBotPluginView bridge 未注入，请在 AstrBot WebUI 中打开此页面",
      ),
      "error",
    );
    return;
  }

  try {
    await bridge.ready();
  } catch (error) {
    setStatus(
      `${t("bridge_error", "初始化失败")}: ${error?.message || error}`,
      "error",
    );
    return;
  }

  applyTheme();
  renderLabels();
  renderBotList();
  renderTypeButtons();
  await loadPreferences();

  if (typeof bridge.onContext === "function") {
    bridge.onContext(() => {
      applyTheme();
      renderLabels();
      renderBotList();
      renderTypeButtons();
      renderStats();
      renderList();
    });
  }

  await load();
}

init();
