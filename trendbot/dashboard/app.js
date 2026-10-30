/* TrendBot dashboard client. Talks only to the backend /api (same process as the EA endpoints).
   Auth: token stored in sessionStorage (never in the page source). No secrets are ever displayed. */
"use strict";

const state = { token: "", account: null, accounts: [], overview: null };

/* ---------- helpers ---------- */
const $ = (id) => document.getElementById(id);
const money = (v, cur) => v == null ? "—" :
  (Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : v.toFixed(2)) +
  " " + (cur || state.overview?.account?.currency || "");
const fmtT = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "content-type": "application/json", "authorization": "Bearer " + state.token, ...(opts.headers || {}) },
  });
  if (r.status === 401) { toast("Unauthorized — check TB_DASHBOARD_TOKEN", true); throw new Error("401"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { toast(j.error || ("HTTP " + r.status), true); throw new Error(j.error || r.status); }
  return j;
}

let toastTimer = null;
function toast(msg, err) {
  const t = $("toast"); t.textContent = msg; t.className = "toast" + (err ? " err" : "");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

/* ---------- token gate ---------- */
function ensureToken() {
  let tok = sessionStorage.getItem("tb_token") || new URLSearchParams(location.search).get("token") || "";
  if (!tok) tok = prompt("TrendBot dashboard token (TB_DASHBOARD_TOKEN):") || "";
  if (!tok) { toast("Token required — API is disabled without it", true); return false; }
  sessionStorage.setItem("tb_token", tok); state.token = tok; return true;
}

/* ---------- tabs ---------- */
document.querySelectorAll(".tab").forEach(b => b.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === b));
  document.querySelectorAll("main .panel").forEach(p => p.classList.add("hidden"));
  $("tab-" + b.dataset.tab).classList.remove("hidden");
  render();
}));

/* ---------- account selector ---------- */
async function loadAccounts() {
  const d = await api("/api/accounts");
  state.accounts = d.accounts;
  const sel = $("account-select");
  sel.innerHTML = d.accounts.map(a =>
    `<option value="${esc(a.id)}" ${a.id === (state.account || d.selected) ? "selected" : ""}>${esc(a.name)}${a.status !== "ACTIVE" ? " [" + a.status + "]" : ""}</option>`
  ).join("");
  state.account = d.selected && d.accounts.some(a => a.id === d.selected) ? d.selected : (d.accounts[0] || {}).id || null;
  sel.onchange = async () => {
    state.account = sel.value;
    await api("/api/accounts/" + encodeURIComponent(state.account) + "/action",
      { method: "POST", body: JSON.stringify({ action: "select" }) });
    refresh();
  };
}

/* ---------- overview / chips / cards ---------- */
async function loadOverview() {
  if (!state.account) return;
  state.overview = await api("/api/overview?account=" + encodeURIComponent(state.account));
}

function chip(el, label, ok, warnText, okText, badText) {
  el.textContent = label + ": " + (ok === null ? "?" : ok ? okText : (warnText ?? badText));
  el.className = "chip " + (ok === null ? "" : ok ? "ok" : "bad");
}

function renderTop() {
  const o = state.overview; if (!o) return;
  chip($("chip-backend"), "Backend", o.backend.online, null, "ONLINE", "OFFLINE");
  const eaOn = o.ea.online;
  $("chip-ea").textContent = "EA: " + (eaOn ? "ONLINE" : "OFFLINE");
  $("chip-ea").className = "chip " + (eaOn ? "ok" : "bad");
  const on = o.trading.on;
  $("chip-trading").textContent = "Trading: " + (on ? "ON" : "BLOCKED");
  $("chip-trading").className = "chip " + (on ? "ok" : "bad");
  const banner = $("blocked-banner");
  if (!on && o.trading.blocked_reasons.length) {
    banner.classList.remove("hidden");
    banner.innerHTML = "<b>Trading blocked.</b> Reason(s): " + o.trading.blocked_reasons.map(esc).join(", ");
  } else banner.classList.add("hidden");
}

function card(k, v, sub, cls) {
  return `<div class="card"><div class="k">${esc(k)}</div><div class="v ${cls || ""}">${v}</div>${sub ? `<div class="s ${esc(sub)}">${esc(sub)}</div>` : ""}</div>`;
}

function renderCards() {
  const o = state.overview; if (!o) return;
  const m = o.money, p = o.prop, s = o.stats;
  $("overview-cards").innerHTML = [
    card("Balance", money(m.balance)),
    card("Equity", money(m.equity)),
    card("Today's P/L", money(m.today_pl), null, m.today_pl > 0 ? "pos" : m.today_pl < 0 ? "neg" : ""),
    card("Overall Drawdown", money(p.current_drawdown_money), p.overall_status),
    card("Remaining Drawdown", money(p.remaining_drawdown_money)),
    card("Daily Remaining", money(p.daily_remaining_money), p.daily_status),
    card("Risk %", (s.risk_percent ?? "—") + "%"),
    card("Trades Today", `${s.trades_today} / ${s.max_trades_per_day}`),
    card("Win Rate", s.win_rate == null ? "—" : s.win_rate + "%"),
    card("Open Positions", o.positions.length),
    card("Consec. Losses", s.consecutive_losses, s.locked_out ? "BLOCKED" : "SAFE"),
    card("Target Mult.", "×" + s.target_multiplier),
  ].join("");
}

/* ---------- risk panel ---------- */
function renderRisk() {
  const o = state.overview; if (!o) return;
  const p = o.prop, m = o.money, acc = o.account;
  const op = p.overall_used_pct, dp = p.daily_used_pct;
  $("dd-meter").style.width = Math.min(op ?? 0, 100) + "%";
  $("dd-meter-label").textContent =
    `${op == null ? "No overall DD limit" : Math.round(op) + "% used"} — ` +
    `${money(p.current_drawdown_money)} used / ${money(p.remaining_drawdown_money)} remaining`;
  $("daily-meter").style.width = Math.min(dp ?? 0, 100) + "%";
  $("daily-meter-label").textContent =
    `${dp == null ? "No daily limit" : Math.round(dp) + "% used"} — ` +
    `${money(p.daily_used_money)} used / ${money(p.daily_remaining_money)} remaining`;
  const rows = [
    ["Account", acc.name], ["MT5 Login", acc.mt5_login ?? "—"], ["Mode", acc.mode.toUpperCase()],
    ["Initial Account", money(p.initial_balance)], ["Current Balance", money(m.balance)],
    ["Current Equity", money(m.equity)], ["Floating P/L", money(m.floating_pl)],
    ["Max Drawdown", money(p.max_drawdown_money)], ["Current Drawdown", money(p.current_drawdown_money)],
    ["Remaining Drawdown", money(p.remaining_drawdown_money)],
    ["Daily Limit", money(p.daily_limit_money)], ["Daily Used", money(p.daily_used_money)],
    ["Daily Remaining", money(p.daily_remaining_money)], ["Current Open Risk", money(p.open_risk_money)],
    ["Next Trade Risk", money(p.next_trade_risk_money)],
  ];
  $("risk-table").innerHTML = rows.map(r => `<div class="kv"><span class="l">${esc(r[0])}</span><span class="r">${esc(r[1])}</span></div>`).join("");
}

/* ---------- positions ---------- */
function posTable(positions, withActions) {
  if (!positions.length) return `<p style="color:var(--muted)">No open positions.</p>`;
  const dir = (d) => `<span class="${d === "BUY" ? "buy" : "sell"}">${d}</span>`;
  return `<table><tr><th>Symbol</th><th>Dir</th><th>Lots</th><th>Entry</th><th>SL</th><th>TP</th><th>Risk</th><th>Basket</th><th>Opened</th>${withActions ? "<th></th>" : ""}</tr>` +
    positions.map(p => `<tr>
      <td>${esc(p.symbol)}</td><td>${dir(p.direction)}</td><td>${p.lots}</td>
      <td>${p.entry ?? "—"}</td><td>${p.sl ?? "—"}</td><td>${p.tp || "target"}</td>
      <td>${money(p.risk_money)}</td><td title="${esc(p.basket)}">${esc(String(p.basket).slice(0, 8))}</td>
      <td>${new Date(p.opened_at * 1000).toLocaleString()}</td>
      ${withActions ? `<td><button class="small" onclick="closePos('${esc(p.ticket)}','${esc(p.basket)}')">Close</button></td>` : ""}
    </tr>`).join("") + "</table>";
}

window.closePos = async (ticket, basket) => {
  if (!confirm(`Close position ticket ${ticket}? This sends a real close order through the EA.`)) return;
  await api("/api/positions/action", { method: "POST", body: JSON.stringify(
    { action: "close_position", ticket: Number(ticket), basket, account: state.account, confirm: true }) });
  toast("Close command queued for EA"); refresh();
};

async function closeBasket(basket) {
  if (!confirm(`Close ALL positions in basket ${String(basket).slice(0, 8)}?`)) return;
  await api("/api/positions/action", { method: "POST", body: JSON.stringify(
    { action: "close_basket", basket, account: state.account, confirm: true }) });
  toast("Basket close queued"); refresh();
}
window.closeBasket = closeBasket;

$("btn-close-all").onclick = async () => {
  const phrase = prompt('Type "STOP" to confirm emergency stop (queues close of ALL positions; broker-side SL/TP remain until then):');
  if (phrase !== "STOP") { toast("Emergency stop cancelled"); return; }
  await api("/api/positions/action", { method: "POST", body: JSON.stringify(
    { action: "close_all", account: state.account, confirm: true }) });
  toast("EMERGENCY STOP queued for EA"); refresh();
};

/* ---------- history ---------- */
async function renderHistory() {
  const sym = $("hist-symbol").value.trim(), res = $("hist-result").value;
  const q = new URLSearchParams({ account: state.account });
  if (sym) q.set("symbol", sym); if (res) q.set("result", res);
  const d = await api("/api/history?" + q);
  if (!d.history.length) { $("history-table").innerHTML = `<p style="color:var(--muted)">No closed trades yet.</p>`; return; }
  $("history-table").innerHTML = `<table><tr><th>Closed</th><th>Account</th><th>Symbol</th><th>Dir</th><th>Risk</th><th>P/L</th><th>R</th><th>Result</th><th>Reason</th></tr>` +
    d.history.map(h => `<tr><td>${new Date(h.closed_at * 1000).toLocaleString()}</td><td>${esc(h.account)}</td>
      <td>${esc(h.symbol)}</td><td><span class="${h.direction === "BUY" ? "buy" : "sell"}">${h.direction}</span></td>
      <td>${money(h.risk)}</td><td class="${h.pnl > 0 ? "pos" : "neg"}" style="color:${h.pnl > 0 ? "var(--green)" : "var(--red)"}">${money(h.pnl)}</td>
      <td>${h.r_multiple ?? "—"}</td><td>${esc(h.result)}</td><td>${esc(h.reason)}</td></tr>`).join("") + "</table>";
}
$("hist-refresh").onclick = renderHistory;

/* ---------- activity feed ---------- */
const EV_CLASS = (t) => {
  t = (t || "").toUpperCase();
  if (t.includes("BLOCK") || t.includes("REJECT") || t.includes("LOCKOUT") || t.includes("ERROR")) return "blocked";
  if (t.includes("ORDER") || t.includes("POSITION") || t.includes("BASKET") || t.includes("FILL")) return "trade";
  if (t.includes("PASS") || t.includes("CONFIRM") || t.includes("ACK") || t.includes("CLOSED_WIN")) return "pass";
  return "info";
};
function evLine(e) {
  const reason = e.detail && (e.detail.reason || e.detail.message || e.detail.blocked_reason);
  const extra = reason ? ` — ${reason}` : "";
  return `<div class="ev ${EV_CLASS(e.type)}"><span class="t">${fmtT(e.ts)}</span><span>${esc(e.type)}${esc(extra)}</span></div>`;
}
async function renderActivity() {
  const d = await api("/api/activity?limit=80");
  $("activity-feed").innerHTML = d.activity.map(evLine).join("") || `<p style="color:var(--muted)">No events yet.</p>`;
  $("ov-activity").innerHTML = d.activity.slice(0, 12).map(evLine).join("");
}

/* ---------- accounts management ---------- */
async function renderAccounts() {
  const d = await api("/api/accounts");
  const rows = d.accounts.map(a => `<tr>
    <td>${esc(a.name)}</td><td>${esc(a.broker || "—")}</td><td>${a.mt5_login ?? "—"}</td>
    <td>${esc(a.prop_profile || "custom")}</td><td>${money(a.initial_balance, a.currency)}</td>
    <td>${a.ea_online ? '<span style="color:var(--green)">ONLINE</span>' : '<span style="color:var(--red)">OFFLINE</span>'}</td>
    <td>${esc(a.status)}</td>
    <td>
      <button class="small" onclick="acctAction('${esc(a.id)}','select')">Select</button>
      <button class="small" onclick="acctAction('${esc(a.id)}','activate')">Activate</button>
      <button class="small" onclick="acctAction('${esc(a.id)}','pause')">Pause</button>
      <button class="small danger" onclick="acctAction('${esc(a.id)}','archive')">Archive</button>
    </td></tr>`).join("");
  $("accounts-table").innerHTML = `<table><tr><th>Name</th><th>Broker</th><th>MT5 ID</th><th>Prop</th><th>Initial</th><th>EA</th><th>Status</th><th></th></tr>${rows}</table>`;
}
window.acctAction = async (id, action) => {
  const body = { action };
  if (action === "archive") { if (!confirm("Archive this account? Its data stays in the DB but it stops trading.")) return; body.confirm = true; }
  const d = await api("/api/accounts/" + encodeURIComponent(id) + "/action", { method: "POST", body: JSON.stringify(body) });
  if (action === "activate" && d.checks) {
    toast("Activated. EA online: " + (d.checks.ea_online ? "yes" : "NO — no new trades until heartbeat") +
          ", identity verified: " + (d.checks.identity_verified ? "yes" : "pending"));
  } else toast(action + " ok");
  await loadAccounts(); refresh();
};

$("btn-add-account").onclick = () => $("account-form").classList.toggle("hidden");
$("acc-cancel").onclick = () => $("account-form").classList.add("hidden");
$("acc-save").onclick = async () => {
  const body = {
    name: $("acc-name").value.trim(), broker: $("acc-broker").value.trim(),
    mt5_login: $("acc-login").value ? Number($("acc-login").value) : null,
    account_type: $("acc-type").value, initial_balance: Number($("acc-initial").value || 0),
    currency: $("acc-currency").value.trim() || "USD", prop_profile: $("acc-prop").value || null,
  };
  if (!body.name) { $("acc-form-msg").textContent = "Name required"; return; }
  try {
    const d = await api("/api/accounts", { method: "POST", body: JSON.stringify(body) });
    $("acc-form-msg").textContent = ""; $("account-form").classList.add("hidden");
    toast("Account created: " + d.account.name);
    await loadAccounts(); refresh();
  } catch (e) { $("acc-form-msg").textContent = "Failed — see toast"; }
};

/* ---------- settings ---------- */
const GROUPS = {
  "Strategy": ["gate", "profile"],
  "Risk & Basket": ["risk", "target", "open_risk", "max_positions", "basket_positions", "split", "window", "timeout", "be", "be_trigger", "be_offset", "partial", "min_conf", "trades_day", "daily_loss"],
  "Indicators": ["ind", "vol"],
  "News": ["news_high", "news_medium", "news_low", "news_missing"],
  "Trading Mode & Safety": ["mode", "kill"],
};
async function renderSettings() {
  const d = await api("/api/settings?account=" + encodeURIComponent(state.account));
  const applied = d.applied_by_ea;
  $("cfg-sync").innerHTML =
    `Config version: <b>#${d.version}</b> &nbsp; EA applied version: <b>#${d.applied_version}</b> &nbsp; ` +
    `<span class="applied ${applied ? "yes" : "no"}">${applied ? "APPLIED BY EA" : "PENDING EA ACK — not yet active"}</span>`;
  const groupsEl = $("settings-groups");
  groupsEl.innerHTML = "";
  for (const [gname, keys] of Object.entries(GROUPS)) {
    const box = document.createElement("div"); box.className = "box";
    box.innerHTML = `<h3>${gname}</h3>`;
    for (const key of keys) {
      const meta = d.schema.find(s => s.key === key); if (!meta) continue;
      const val = getDeep(d.values, key) ?? "";
      const row = document.createElement("div"); row.className = "settingrow";
      let input;
      if (meta.kind === "bool") {
        input = `<select data-key="${key}"><option value="">—</option><option ${val === true ? "selected" : ""} value="true">on</option><option ${val === false ? "selected" : ""} value="false">off</option></select>`;
      } else if (meta.kind === "toggle") {
        // multi-select list of enabled items (indicators / volatility states)
        const cur = Array.isArray(val) ? val.map(String.toLowerCase) : [];
        input = `<span class="toggles">${meta.choices.map(c =>
          `<label style="display:inline-flex;gap:4px;align-items:center;font-size:12px;color:var(--text)"><input type="checkbox" data-tog="${key}" value="${esc(c)}" ${cur.includes(c.toLowerCase()) ? "checked" : ""}>${esc(c)}</label>`).join(" ")}</span>`;
      } else if (meta.choices && meta.choices.length) {
        // LIVE mode intentionally NOT offered here; enabling live requires the documented code-level unlock.
        const choices = key === "mode" ? meta.choices.filter(c => c.toLowerCase() !== "live") : meta.choices;
        const curVal = Array.isArray(val) ? val[0] : val;   // enum stored in a list for vol states
        input = `<select data-key="${key}">${choices.map(c => `<option ${String(c).toLowerCase() === String(curVal).toLowerCase() ? "selected" : ""}>${c}</option>`).join("")}</select>`;
      } else {
        input = `<input size="8" data-key="${key}" value="${esc(Array.isArray(val) ? val.join(",") : val)}">`;
      }
      row.innerHTML = `<div class="lbl">${esc(meta.label)}<small>${esc(key)}</small></div>${input}<button class="small">Set</button>`;
      row.querySelector("button").onclick = async () => {
        let v;
        if (meta.kind === "toggle") {
          v = [...row.querySelectorAll(`[data-tog="${key}"]:checked`)].map(x => x.value).join(",");
        } else {
          v = row.querySelector(`[data-key="${key}"]`).value;
          if (v === "") return;
          if (meta.kind === "float" || meta.kind === "int") v = Number(v);
        }
        const body = { key, value: v };
        let r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
        if (!r.ok && r.proposal_id &&
            confirm(`${r.message}\nConfirm this change? It becomes active only after the EA acknowledges it.`)) {
          body.confirm = true;
          r = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
        }
        toast(r.ok ? `Saved ${key} → v${r.version}. Applied once EA acks.` : (r.message || "rejected"), !r.ok);
        renderSettings();
      };
      box.appendChild(row);
    }
    groupsEl.appendChild(box);
  }
}
function getDeep(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj); }

/* ---------- render loop ---------- */
function render() {
  renderTop(); renderCards(); renderRisk();
  const o = state.overview;
  $("ov-positions").innerHTML = o ? posTable(o.positions, false) : "";
  $("positions-table").innerHTML = o ? posTable(o.positions, true) : "";
  const active = document.querySelector(".tab.active").dataset.tab;
  if (active === "history") renderHistory();
  if (active === "activity") renderActivity();
  if (active === "accounts") renderAccounts();
  if (active === "settings") renderSettings();
}

async function refresh() {
  try {
    await loadOverview();
    render();
  } catch (e) { /* toast already shown */ }
}

(async function boot() {
  if (!ensureToken()) return;
  try {
    await loadAccounts();
    if (!state.accounts.length) {
      toast("No accounts yet — open the Accounts tab and add one.");
      document.querySelector('[data-tab="accounts"]').click();
    }
    await refresh();
    setInterval(refresh, 5000);
  } catch (e) { /* auth errors surfaced via toast */ }
})();
