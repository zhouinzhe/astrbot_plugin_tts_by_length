// Fish TTS 控制台：音色库 + 全部设置。所有数据都通过插件后端接口读写（bridge 会带上 Dashboard 的登录身份）。
// 页面跑在沙箱 iframe 里（allow-scripts allow-forms allow-downloads）：
//   - 没有 allow-modals：不能用 alert / confirm，提示和确认都在页面里自己做；
//   - 没有 allow-same-origin：不能用 localStorage，状态只放在内存里。
// 设置表单是按后端给的设置项定义（来自 _conf_schema.json）自动生成的，新增设置项这里不用改。
const bridge = window.AstrBotPluginView ?? window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const els = {
  chips: $("chips"), banners: $("banners"), fatal: $("fatal"), fatalText: $("fatal-text"), retry: $("btn-retry"),
  tabVoices: $("tab-voices"), tabSettings: $("tab-settings"), tabDot: $("tab-dot"),
  panelVoices: $("panel-voices"), panelSettings: $("panel-settings"),
  // 音色库
  testText: $("test-text"), testCurrent: $("btn-test-current"), makeDefault: $("btn-default"), add: $("btn-add"),
  list: $("list"), count: $("count"),
  formCard: $("form-card"), formTitle: $("form-title"), name: $("f-name"), id: $("f-id"), note: $("f-note"),
  save: $("btn-save"), testForm: $("btn-test-form"), cancel: $("btn-cancel"),
  // 设置
  search: $("search"), toggleAll: $("btn-toggle-all"), warnings: $("warnings"), warningsList: $("warnings-list"),
  warningsClose: $("btn-warnings-close"), groups: $("groups"), noMatch: $("no-match"),
  // 底部
  dock: $("dock"), player: $("player"), playerLabel: $("player-label"), audio: $("audio"), playerClose: $("btn-player-close"),
  savebar: $("savebar"), saveCount: $("save-count"), discard: $("btn-discard"), saveSettings: $("btn-save-settings"),
  toast: $("toast"),
};

// ---------- 状态 ----------
let voice = { voices: [], active: "", default_id: "", backend: "fish", has_key: true, max_voices: 50, test_max_chars: 200 };
let editing = null; // 正在编辑的音色名称；null = 添加新音色
let confirming = null; // 点过一次「删除」、等待再点一次确认的音色名称
let confirmTimer = 0;
let toastTimer = 0;
let busy = false; // 有保存 / 修改类请求在进行时，按钮先不响应
let testing = false; // 试听最多等 30 秒，单独计，不挡住保存

let groups = []; // 后端给的设置分组
const itemIndex = new Map(); // 路径 -> 设置项（value 是已保存的值）
const controls = new Map(); // 路径 -> {set(value), focus(), refreshSecret?()}
const rows = new Map(); // 路径 -> 这一项的 DOM
const groupEls = new Map(); // 分组名 -> 这个分组的 DOM
let form = {}; // 路径 -> 表单里当前的值
let secrets = {}; // 路径 -> {value, clear}：密钥项不回显，只记「这次要改成什么」
let errors = {}; // 路径 -> 后端返回的错误原因
const openGroups = new Set(["basic"]);
let query = "";

// ---------- 小工具 ----------
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else if (key === "hidden") node.hidden = true;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat().filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const clone = (value) => (Array.isArray(value) ? [...value] : value);
const errorText = (error) => error?.message || String(error);

function toast(message, isError = false) {
  els.toast.textContent = message;
  els.toast.className = isError ? "show error" : "show";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.className = ""; }, isError ? 5200 : 3200);
}

// 修改 / 保存类操作：同一时间只做一件，出错统一用 toast 提示
async function run(task) {
  if (busy) return undefined;
  busy = true;
  document.body.classList.add("busy");
  try {
    return await task();
  } catch (error) {
    toast(errorText(error), true);
    return undefined;
  } finally {
    busy = false;
    document.body.classList.remove("busy");
  }
}

// 底部的播放条 / 保存条：有内容才显示，并把它的高度告诉页面，内容不会被盖住
function syncDock() {
  els.dock.hidden = els.player.hidden && els.savebar.hidden;
  document.documentElement.style.setProperty("--dock-h", els.dock.hidden ? "0px" : `${els.dock.offsetHeight}px`);
}
new ResizeObserver(syncDock).observe(els.dock);

// ---------- 标签页 ----------
function setTab(name) {
  const isVoices = name === "voices";
  els.tabVoices.setAttribute("aria-selected", String(isVoices));
  els.tabSettings.setAttribute("aria-selected", String(!isVoices));
  els.panelVoices.hidden = !isVoices;
  els.panelSettings.hidden = isVoices;
  window.scrollTo({ top: 0 });
}

function goSettings(groupKey) {
  setTab("settings");
  if (!groupKey) return;
  openGroups.add(groupKey);
  query = els.search.value = "";
  applyVisibility();
  requestAnimationFrame(() => groupEls.get(groupKey)?.root.scrollIntoView({ block: "start" }));
}

// ---------- 顶部状态 ----------
const savedValue = (path, fallback) => (itemIndex.has(path) ? itemIndex.get(path).value : fallback);

function chip(kind, label, value) {
  return el("span", { class: `chip ${kind}`.trim() }, `${label} `, el("b", { text: value }));
}

function renderStatus() {
  const enabled = savedValue("basic.enabled", true);
  const viaFish = voice.backend === "fish";
  const chips = [chip(enabled ? "ok" : "warn", "插件", enabled ? "已开启" : "已关闭"), chip(viaFish ? "ok" : "", "合成", viaFish ? "Fish 直连" : "AstrBot 内置")];
  if (viaFish) chips.push(chip(voice.has_key ? "ok" : "warn", "API Key", voice.has_key ? "已设置" : "未设置"));
  chips.push(chip("", "音色", voice.active || (voice.default_id ? "设置里的默认 ID" : "Fish 默认")));
  els.chips.replaceChildren(...chips);

  const warn = [];
  if (!enabled) warn.push(["插件总开关已关闭，现在不会转语音，也不会处理情绪标签。", "basic", "去开启"]);
  if (!viaFish) warn.push(["语音合成方式不是 Fish 直连，音色库和试听暂时不生效。", "fish", "去修改"]);
  if (viaFish && !voice.has_key) warn.push(["还没有填写 Fish API Key，无法试听，也无法合成语音。", "fish", "去填写"]);
  els.banners.replaceChildren(
    ...warn.map(([text, group, label]) => el("div", { class: "banner" }, el("span", { text }), el("button", { type: "button", text: label, onclick: () => goSettings(group) }))),
  );
}

// ---------- 音色库 ----------
function renderVoices() {
  const { voices, max_voices: max } = voice;
  els.count.textContent = `${voices.length} / ${max}`;
  els.makeDefault.hidden = !voice.active;
  els.testCurrent.textContent = voice.active ? `试听「${voice.active}」` : "试听当前音色";
  els.list.replaceChildren(...voices.map(renderVoice));
  if (!voices.length) {
    els.list.append(el("p", { class: "empty muted", text: "还没有保存音色。点「添加音色」，填写名称和音色 ID 就行。" }));
  }
}

function renderVoice(item) {
  const confirmNow = confirming === item.name;
  const head = el("div", { class: "voice-head" }, el("strong", { text: item.name }));
  if (item.active) head.append(el("span", { class: "badge", text: "使用中" }));

  const buttons = el("div", { class: "row" }, el("button", { type: "button", "data-test": "", text: "试听", onclick: () => testVoice({ name: item.name }) }));
  if (!item.active) buttons.append(el("button", { type: "button", class: "primary", text: "使用", onclick: () => useVoice(item.name) }));
  buttons.append(
    el("button", { type: "button", text: "编辑", onclick: () => startEdit(item) }),
    el("button", { type: "button", class: confirmNow ? "danger" : "", text: confirmNow ? "再点一次删除" : "删除", onclick: () => removeVoice(item.name) }),
  );

  const card = el("article", { class: item.active ? "voice active" : "voice" }, head);
  if (item.note) card.append(el("p", { class: "note", text: item.note }));
  card.append(el("code", { class: "vid", text: item.id }), buttons);
  return card;
}

function openForm(open) {
  els.formCard.hidden = !open;
  els.add.setAttribute("aria-expanded", String(open));
  els.add.textContent = open ? "收起表单" : "＋ 添加音色";
}

function resetForm() {
  editing = null;
  els.name.value = els.id.value = els.note.value = "";
  els.name.disabled = false;
  els.formTitle.textContent = "添加音色";
  els.save.textContent = "保存";
  openForm(false);
}

function startEdit(item) {
  editing = item.name;
  els.name.value = item.name;
  els.name.disabled = true; // 改名请删掉重建
  els.id.value = item.id;
  els.note.value = item.note;
  els.formTitle.textContent = `编辑「${item.name}」`;
  els.save.textContent = "保存修改";
  openForm(true);
  els.formCard.scrollIntoView({ behavior: "smooth", block: "start" });
}

function applyVoiceState(next) {
  voice = next;
  els.testText.maxLength = voice.test_max_chars;
  renderVoices();
  renderStatus();
}

// 改动类接口：后端返回 {message, ...最新状态}
async function mutate(endpoint, body = {}) {
  const result = await bridge.apiPost(endpoint, body);
  applyVoiceState(result);
  toast(result.message);
}

const saveVoice = () => run(async () => {
  await mutate("voices/save", { name: els.name.value, id: els.id.value, note: els.note.value });
  resetForm();
});

const useVoice = (name) => run(() => mutate("voices/use", { name }));

function removeVoice(name) {
  if (confirming !== name) { // 第一次点：先变成「再点一次删除」，4 秒内没再点就恢复
    confirming = name;
    clearTimeout(confirmTimer);
    confirmTimer = setTimeout(() => { confirming = null; renderVoices(); }, 4000);
    renderVoices();
    return;
  }
  clearTimeout(confirmTimer);
  confirming = null;
  run(async () => {
    await mutate("voices/remove", { name });
    if (editing === name) resetForm();
  });
}

// ---------- 试听 ----------
// target: {name} 试音色库里的音色；{id} 试还没保存的 ID；{} 试当前在用的音色
function closePlayer() {
  els.audio.pause();
  els.audio.removeAttribute("src");
  els.audio.load();
  els.player.hidden = true;
  syncDock();
}

async function testVoice(target) {
  if (testing) return;
  testing = true;
  document.body.classList.add("testing");
  els.playerLabel.textContent = "合成中…";
  els.audio.hidden = true;
  els.player.hidden = false;
  syncDock();
  try {
    const result = await bridge.apiPost("voices/test", { ...target, text: els.testText.value });
    els.playerLabel.textContent = `试听：${result.voice} · 用时 ${result.seconds} 秒`;
    els.audio.src = `data:${result.mime};base64,${result.audio}`;
    els.audio.hidden = false;
    syncDock();
    try {
      await els.audio.play();
    } catch {
      els.playerLabel.textContent += "（浏览器没有自动播放，点播放键收听）";
    }
  } catch (error) {
    closePlayer();
    toast(errorText(error), true);
  } finally {
    testing = false;
    document.body.classList.remove("testing");
  }
}

// 设置页里的「试听」：有没保存的修改就先保存，再用保存后的设置试听
async function testWithSettings() {
  if (dirtyCount() && !(await saveSettings())) return;
  await testVoice({});
}

// ---------- 设置：值与状态 ----------
const normalize = (item, value) => {
  if (item.type === "list") return (value || []).map((line) => line.trim()).filter(Boolean);
  if (item.type === "string" || item.type === "text") return String(value ?? "").trim();
  return value;
};

function isDirty(item) {
  if (item.secret) {
    const secret = secrets[item.path];
    return Boolean(secret && (secret.clear || secret.value.trim()));
  }
  return !same(normalize(item, form[item.path]), normalize(item, item.value));
}

const canReset = (item) => !item.secret && !same(normalize(item, form[item.path]), normalize(item, item.default));
const dirtyItems = () => [...itemIndex.values()].filter(isDirty);
const dirtyCount = () => dirtyItems().length;

function isActive(item) {
  return (item.depends || []).every((rule) => rule.in.some((allowed) => same(allowed, form[rule.path])));
}

function payloadValue(item) {
  if (item.secret) {
    const secret = secrets[item.path];
    return secret.clear ? "" : secret.value.trim();
  }
  return item.type === "list" ? normalize(item, form[item.path]) : form[item.path];
}

function collectValues() {
  const values = {};
  for (const item of dirtyItems()) {
    const [group, key] = item.path.split(".");
    (values[group] ??= {})[key] = payloadValue(item);
  }
  return values;
}

function onEdit(path, value) {
  form[path] = value;
  delete errors[path];
  refresh();
}

// 把「已改动 / 未生效 / 出错 / 保存条」这些状态刷新到界面上（不重建表单，输入框不会丢焦点）
function refresh() {
  const modified = {};
  for (const item of itemIndex.values()) {
    const row = rows.get(item.path);
    if (!row) continue;
    const dirty = isDirty(item);
    const inactive = !isActive(item);
    const message = errors[item.path];
    const group = item.path.split(".")[0];
    if (dirty) modified[group] = (modified[group] || 0) + 1;
    row.root.classList.toggle("dirty", dirty);
    row.root.classList.toggle("inactive", inactive);
    row.root.classList.toggle("has-error", Boolean(message));
    row.reset.hidden = !canReset(item);
    row.needs.hidden = !(inactive && item.needs);
    row.error.hidden = !message;
    row.error.textContent = message || "";
  }
  const total = Object.values(modified).reduce((sum, n) => sum + n, 0);
  for (const [key, parts] of groupEls) {
    parts.mod.hidden = !modified[key];
    parts.mod.textContent = `${modified[key] || 0} 处修改`;
    if (parts.test) parts.test.textContent = total ? "保存并试听" : "试听当前设置";
  }
  els.savebar.hidden = total === 0;
  els.saveCount.textContent = `${total} 项设置还没保存`;
  els.tabDot.hidden = total === 0;
  syncDock();
}

// ---------- 设置：搜索与展开 ----------
function matchesQuery(item) {
  if (!query) return true;
  return `${item.title} ${item.hint} ${item.path}`.toLowerCase().includes(query);
}

function applyVisibility() {
  let anyVisible = false;
  for (const group of groups) {
    const parts = groupEls.get(group.key);
    const hits = group.items.filter(matchesQuery);
    const open = query ? hits.length > 0 : openGroups.has(group.key);
    parts.root.hidden = hits.length === 0;
    parts.body.hidden = !open;
    parts.head.setAttribute("aria-expanded", String(open));
    for (const item of group.items) rows.get(item.path).root.hidden = !matchesQuery(item);
    anyVisible ||= hits.length > 0;
  }
  els.noMatch.hidden = anyVisible;
  els.toggleAll.hidden = Boolean(query);
  els.toggleAll.textContent = groups.every((g) => openGroups.has(g.key)) ? "全部收起" : "全部展开";
}

function toggleGroup(key) {
  if (openGroups.has(key)) openGroups.delete(key);
  else openGroups.add(key);
  applyVisibility();
}

// ---------- 设置：控件 ----------
const rangeText = (item) => `范围 ${item.min} ~ ${item.max}`;

function numberControl(item, id) {
  const noMinus = item.min >= 0; // iOS 的纯数字键盘没有负号，有负数的项不限定键盘
  const input = el("input", {
    type: "number", id, min: item.min, max: item.max, step: "any", autocomplete: "off",
    inputmode: noMinus ? (item.type === "int" ? "numeric" : "decimal") : null,
  });
  const read = () => (input.value.trim() === "" || Number.isNaN(input.valueAsNumber) ? null : input.valueAsNumber);
  if (!item.slider) {
    input.addEventListener("input", () => onEdit(item.path, read()));
    return { node: input, set: (value) => { input.value = value ?? ""; }, focus: () => input.focus() };
  }
  const slider = el("input", { type: "range", min: item.min, max: item.max, step: item.step, "aria-label": `${item.title}（滑块）` });
  slider.addEventListener("input", () => {
    input.value = slider.value;
    onEdit(item.path, Number(slider.value));
  });
  input.addEventListener("input", () => {
    const value = read();
    if (value !== null) slider.value = String(value);
    onEdit(item.path, value);
  });
  return {
    node: el("div", { class: "slider-row" }, slider, input),
    set: (value) => { input.value = value ?? ""; slider.value = String(value ?? item.min); },
    focus: () => input.focus(),
  };
}

function secretControl(item, id) {
  const state = (secrets[item.path] ??= { value: "", clear: false });
  const status = el("p", { class: "secret-status" });
  const input = el("input", { type: "password", id, autocomplete: "new-password", autocapitalize: "off", spellcheck: "false", placeholder: item.placeholder });
  const toggle = el("button", { type: "button", text: "显示", onclick: () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    toggle.textContent = show ? "隐藏" : "显示";
  } });
  const clear = el("button", { type: "button", class: "ghost", text: "清除已保存的 Key", onclick: () => {
    state.clear = !state.clear;
    state.value = "";
    input.value = "";
    refreshSecret();
    refresh();
  } });
  const refreshSecret = () => {
    const stored = itemIndex.get(item.path) ?? item;
    if (state.clear) status.textContent = "保存后会清除已设置的 Key";
    else if (stored.is_set) status.textContent = `已设置（${stored.preview}）。留空表示不修改，输入新的 Key 会替换它`;
    else status.textContent = "还没有设置";
    status.classList.toggle("set", stored.is_set && !state.clear);
    clear.hidden = !stored.is_set;
    clear.textContent = state.clear ? "撤销清除" : "清除已保存的 Key";
    input.disabled = state.clear;
  };
  input.addEventListener("input", () => {
    state.value = input.value;
    delete errors[item.path];
    refresh();
  });
  refreshSecret();
  return {
    node: el("div", {}, status, el("div", { class: "pass-row" }, input, toggle), el("div", { class: "secret-actions" }, clear)),
    set: () => { state.value = ""; state.clear = false; input.value = ""; refreshSecret(); },
    focus: () => input.focus(),
    refreshSecret,
  };
}

function makeControl(item, id) {
  if (item.secret) return secretControl(item, id);
  if (item.type === "bool") {
    const input = el("input", { type: "checkbox", id, role: "switch" });
    input.addEventListener("change", () => onEdit(item.path, input.checked));
    return { node: el("label", { class: "switch" }, input, el("span", { class: "track" })), set: (value) => { input.checked = Boolean(value); }, focus: () => input.focus() };
  }
  if (item.type === "int" || item.type === "float") return numberControl(item, id);
  if (item.options) {
    const select = el("select", { id }, ...item.options.map((o) => el("option", { value: o.value, text: o.label })));
    select.addEventListener("change", () => onEdit(item.path, select.value));
    return { node: select, set: (value) => { select.value = value; }, focus: () => select.focus() };
  }
  if (item.type === "string") {
    const url = item.path === "fish.api_base";
    const input = el("input", {
      type: url ? "url" : "text", id, class: item.mono ? "mono" : "", autocomplete: "off", autocapitalize: "off", autocorrect: "off",
      spellcheck: "false", placeholder: item.placeholder, inputmode: url ? "url" : null,
    });
    input.addEventListener("input", () => onEdit(item.path, input.value));
    return { node: input, set: (value) => { input.value = value ?? ""; }, focus: () => input.focus() };
  }
  const isList = item.type === "list";
  const length = isList ? 0 : String(item.default || "").length;
  const area = el("textarea", {
    id, rows: isList ? 4 : Math.min(10, Math.max(3, Math.ceil(length / 36))), class: item.mono ? "mono" : "",
    autocapitalize: "off", autocorrect: "off", spellcheck: "false", placeholder: item.placeholder,
  });
  area.addEventListener("input", () => onEdit(item.path, isList ? area.value.split(/\r?\n/) : area.value));
  return { node: area, set: (value) => { area.value = isList ? (value || []).join("\n") : value ?? ""; }, focus: () => area.focus() };
}

function resetItem(path) {
  const item = itemIndex.get(path);
  form[path] = clone(item.default);
  controls.get(path).set(form[path]);
  delete errors[path];
  refresh();
}

function buildItem(item) {
  const id = `f-${item.path.replace(".", "-")}`;
  const control = makeControl(item, id);
  controls.set(item.path, control);
  control.set(form[item.path]);

  const wide = item.type === "text" || item.type === "list" || (item.type === "string" && !item.options);
  const needs = el("p", { class: "item-needs", text: item.needs ? `未生效：${item.needs}` : "", hidden: true });
  const reset = el("button", { type: "button", class: "link", text: "恢复默认", hidden: true, onclick: () => resetItem(item.path) });
  const error = el("p", { class: "item-error", role: "alert", hidden: true });
  const meta = el("div", { class: "item-meta" }, el("span", { class: "range", text: item.type === "int" || item.type === "float" ? rangeText(item) : "" }), reset);
  const text = el("div", { class: "item-text" }, el("label", { class: "item-title", for: id, text: item.title }), el("p", { class: "item-hint", text: item.hint }), needs);
  const root = el("div", { class: `item type-${item.type}${wide ? " wide" : ""}`, "data-path": item.path }, el("div", { class: "item-main" }, text, el("div", { class: "item-control" }, control.node)), meta, error);
  rows.set(item.path, { root, reset, needs, error });
  return root;
}

function buildGroup(group) {
  const mod = el("span", { class: "group-mod", hidden: true });
  const head = el("button", { type: "button", class: "group-head", "aria-expanded": "false", onclick: () => toggleGroup(group.key) },
    group.icon ? el("span", { class: "group-icon", "aria-hidden": "true", text: group.icon }) : null,
    el("span", { class: "group-title", text: group.title }),
    mod,
    el("span", { class: "group-meta", text: `${group.items.length} 项` }),
    el("span", { class: "chev", "aria-hidden": "true" }),
  );
  const body = el("div", { class: "group-body", hidden: true }, group.hint ? el("p", { class: "group-hint", text: group.hint }) : null, ...group.items.map(buildItem));
  const parts = { root: el("section", { class: "group" }, head, body), head, body, mod, test: null };
  if (group.key === "fish" || group.key === "fish_advanced") {
    parts.test = el("button", { type: "button", "data-test": "", text: "试听当前设置", onclick: testWithSettings });
    body.append(el("div", { class: "group-foot" }, parts.test));
  }
  groupEls.set(group.key, parts);
  return parts.root;
}

// ---------- 设置：载入与保存 ----------
function indexGroups(next) {
  groups = next;
  itemIndex.clear();
  for (const group of groups) for (const item of group.items) itemIndex.set(item.path, item);
}

function loadGroups(next) {
  indexGroups(next);
  controls.clear(); rows.clear(); groupEls.clear();
  form = {}; secrets = {}; errors = {};
  for (const item of itemIndex.values()) form[item.path] = clone(item.value);
  els.groups.replaceChildren(...groups.map(buildGroup));
  applyVisibility();
  refresh();
}

// 保存成功后：用后端整理过的值更新表单（比如去重、补默认值），不重建 DOM
function adoptSaved(next) {
  indexGroups(next);
  errors = {};
  for (const item of itemIndex.values()) {
    form[item.path] = clone(item.value);
    controls.get(item.path)?.set(item.value);
  }
  refresh();
}

function showErrors(map) {
  errors = { ...map };
  const paths = Object.keys(errors).filter((path) => rows.has(path));
  for (const path of paths) openGroups.add(path.split(".")[0]);
  query = els.search.value = "";
  applyVisibility();
  refresh();
  const first = paths[0];
  if (first) {
    rows.get(first).root.scrollIntoView({ block: "center" });
    controls.get(first)?.focus();
  }
}

function showWarnings(list) {
  els.warnings.hidden = list.length === 0;
  els.warningsList.replaceChildren(...list.map((text) => el("p", { text: `⚠ ${text}` })));
}

// 返回 true = 保存成功（或本来就没有要保存的）
async function saveSettings() {
  const values = collectValues();
  if (!Object.keys(values).length) return true;
  const ok = await run(async () => {
    const result = await bridge.apiPost("settings/save", { values });
    if (!result.saved) {
      showErrors(result.errors || {});
      toast(result.message || "有设置不合法，没有保存", true);
      return false;
    }
    adoptSaved(result.groups);
    applyVoiceState(result.voice);
    showWarnings(result.warnings || []);
    toast(result.message);
    return true;
  });
  return ok === true;
}

function discardSettings() {
  for (const item of itemIndex.values()) {
    form[item.path] = clone(item.value);
    controls.get(item.path)?.set(item.value);
  }
  errors = {};
  refresh();
}

// ---------- 事件 ----------
els.tabVoices.addEventListener("click", () => setTab("voices"));
els.tabSettings.addEventListener("click", () => setTab("settings"));
els.add.addEventListener("click", () => {
  if (!els.formCard.hidden) { resetForm(); return; }
  openForm(true);
  els.formCard.scrollIntoView({ behavior: "smooth", block: "start" });
  els.name.focus({ preventScroll: true });
});
els.formCard.addEventListener("submit", (event) => { event.preventDefault(); saveVoice(); });
els.cancel.addEventListener("click", resetForm);
els.testCurrent.addEventListener("click", () => testVoice({}));
els.makeDefault.addEventListener("click", () => run(() => mutate("voices/default")));
els.testForm.addEventListener("click", () => {
  if (!els.id.value.trim()) toast("先填写音色 ID", true);
  else testVoice({ id: els.id.value });
});
els.playerClose.addEventListener("click", closePlayer);
els.search.addEventListener("input", () => { query = els.search.value.trim().toLowerCase(); applyVisibility(); });
els.toggleAll.addEventListener("click", () => {
  const all = groups.every((group) => openGroups.has(group.key));
  for (const group of groups) {
    if (all) openGroups.delete(group.key);
    else openGroups.add(group.key);
  }
  applyVisibility();
});
els.warningsClose.addEventListener("click", () => showWarnings([]));
els.saveSettings.addEventListener("click", saveSettings);
els.discard.addEventListener("click", discardSettings);
els.retry.addEventListener("click", () => boot());

// ---------- 启动 ----------
function showFatal(message) {
  els.fatalText.textContent = message;
  els.fatal.hidden = false;
  els.panelVoices.hidden = els.panelSettings.hidden = true;
}

async function boot() {
  els.fatal.hidden = true;
  if (!bridge) {
    showFatal("没有检测到 AstrBot 的页面桥接。请在 AstrBot 控制台的插件详情页里打开本页面，不要直接用浏览器打开文件。");
    return;
  }
  try {
    await Promise.race([bridge.ready(), new Promise((_, reject) => setTimeout(() => reject(new Error("等待 AstrBot 控制台响应超时")), 10000))]);
    const [voiceState, settingState] = await Promise.all([bridge.apiGet("voices"), bridge.apiGet("settings")]);
    loadGroups(settingState.groups);
    applyVoiceState(voiceState);
  } catch (error) {
    showFatal(`加载失败：${errorText(error)}。确认插件已启用后点「重新加载」。`);
    return;
  }
  setTab(els.tabSettings.getAttribute("aria-selected") === "true" ? "settings" : "voices");
}

await boot();
