"use strict";

const HOST_NAME = "com.wasabihu.sessfork";
const DEFAULT_EXT_ID = "eipgnmlpcpmadpbignmifidnbjfmlphm";

// Aligned with src/sessfork/chrome.py & rust/sessfork-mcp/src/native_host.rs so root-domain
// calculation never widens to an entire ccTLD or shared hosting suffix.
const MULTI_LABEL_SUFFIXES = new Set([
  "co.uk", "org.uk", "ac.uk", "gov.uk",
  "com.cn", "net.cn", "org.cn", "gov.cn", "edu.cn",
  "com.au", "net.au", "org.au", "edu.au", "gov.au",
  "co.jp", "ne.jp", "or.jp", "ac.jp", "go.jp",
  "co.kr", "com.br", "com.hk", "com.tw", "com.sg", "gov.sg", "co.in",
  "co.nz", "co.za", "co.id", "co.il", "com.mx", "com.ar",
  "com.my", "com.tr", "com.vn",
  "github.io", "gitlab.io", "vercel.app", "netlify.app",
  "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
  "herokuapp.com", "onrender.com", "fly.dev", "railway.app",
  "appspot.com", "run.app", "azurewebsites.net", "cloudfront.net",
  "amazonaws.com", "s3.amazonaws.com", "googleusercontent.com",
  "myshopify.com", "notion.site", "hf.space", "streamlit.app",
  "supabase.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com",
  "deno.dev", "replit.app", "replit.dev",
  "glitch.me", "surge.sh", "bitbucket.io", "azurestaticapps.net"
]);

const GENERIC_SECOND_LEVEL_CCTLD_RE =
  /^(?:co|com|net|org|gov|edu|ac|go|or|ne|mil|sch|ltd|plc|me)\.[a-z]{2}$/;

const I18N = {
  en: {
    langBtn: "中文",
    checking: "Checking…",
    connected: "Connected",
    setupNeeded: "Setup needed",
    activeSite: "Active Site",
    syncedChip: "Synced",
    unsupported: "Open an http/https tab to fork",
    readySub: "Click to sync Cookies & LocalStorage to local sessfork-mcp",
    syncedSub: (c, k) => `${c} cookies · ${k} localStorage keys synced`,
    includeParent: (d) => `Also include parent domain (${d})`,
    forkSite: "Fork This Site",
    resyncSite: "Re-Sync Site",
    syncing: (d) => `Syncing ${d}…`,
    syncedOk: (d, c, k) => `Synced ${c} cookies + ${k} keys for ${d} (hot-reload ready)`,
    clearedSite: (d) => `Cleared saved session for ${d}`,
    setupTitle: "One-Time Host Registration",
    setupHint: "Run once in your terminal to connect Chrome/Edge to local sessfork-mcp:",
    copyCmd: "Copy Command",
    copied: "Copied!",
    retry: "Retry",
    otherTabs: (n) => `Other Open Tabs (${n})`,
    forkSelected: (n) => n > 0 ? `Fork Selected (${n})` : "Select tabs to fork",
    batchDone: (ok, total) => `Batch sync complete: ${ok}/${total} site(s) updated`,
    syncedSessions: "Synced Sessions",
    clearAll: "Clear All",
    confirmClearAll: "Confirm Clear All?",
    clearedAll: "Cleared all saved sessions",
    noSessions: "No sessions synced yet.",
    footerSecurity: "Local only · Zero cloud · Per-click consent",
    totals: (d, c) => `${d} site(s) · ${c} cookies`,
    removeAria: (d) => `Remove ${d}`,
  },
  zh: {
    langBtn: "EN",
    checking: "检测中…",
    connected: "已连接",
    setupNeeded: "需注册桥接",
    activeSite: "当前站点",
    syncedChip: "已同步",
    unsupported: "请切换到 http/https 网页标签页",
    readySub: "点击后由浏览器安全导出 Cookie 与 LocalStorage 到本机",
    syncedSub: (c, k) => `已缓存 ${c} 条 Cookie · ${k} 条 LocalStorage`,
    includeParent: (d) => `同时包含父域名 Cookie（${d}）`,
    forkSite: "同步当前站点",
    resyncSite: "重新同步",
    syncing: (d) => `正在同步 ${d}…`,
    syncedOk: (d, c, k) => `已同步 ${d}：${c} 条 Cookie + ${k} 条 LocalStorage（热重载就绪）`,
    clearedSite: (d) => `已移除 ${d} 的本地登录态`,
    setupTitle: "一次性注册本地桥接",
    setupHint: "在终端执行一次以下命令，即可把浏览器连到本机 sessfork-mcp：",
    copyCmd: "复制命令",
    copied: "已复制",
    retry: "重连",
    otherTabs: (n) => `批量同步其他打开的标签页（${n}）`,
    forkSelected: (n) => n > 0 ? `一键同步已选（${n}）` : "请勾选要同步的站点",
    batchDone: (ok, total) => `批量同步完成：成功 ${ok}/${total} 个站点`,
    syncedSessions: "已同步站点",
    clearAll: "清空全部",
    confirmClearAll: "确认清空全部？",
    clearedAll: "已清空全部已同步登录态",
    noSessions: "尚未同步任何站点。",
    footerSecurity: "纯本地 · 零云端 · 逐次手动授权",
    totals: (d, c) => `${d} 站 · ${c} Cookie`,
    removeAria: (d) => `移除 ${d}`,
  },
};

function detectInitialLang() {
  try {
    const saved = window.localStorage.getItem("sessfork_ui_lang");
    if (saved === "zh" || saved === "en") return saved;
  } catch (err) {
    console.warn("sessfork lang preference read skipped:", err);
  }
  return (navigator.language || "").toLowerCase().startsWith("zh") ? "zh" : "en";
}

function isUnsafePublicSuffix(host) {
  return MULTI_LABEL_SUFFIXES.has(host) || GENERIC_SECOND_LEVEL_CCTLD_RE.test(host);
}

function computeRegistrableDomain(hostname) {
  const host = (hostname || "").toLowerCase().replace(/^\.+|\.+$/g, "");
  if (!host || host === "localhost" || /^[0-9.]+$/.test(host)) return host;
  const parts = host.split(".");
  if (parts.length <= 2) return host;
  const lastTwo = parts.slice(-2).join(".");
  if (isUnsafePublicSuffix(lastTwo)) {
    return parts.length >= 3 ? parts.slice(-3).join(".") : host;
  }
  return lastTwo;
}

function hostMatchesDomain(host, domain) {
  const h = (host || "").toLowerCase().replace(/^\.+|\.+$/g, "");
  const d = (domain || "").toLowerCase().replace(/^\.+|\.+$/g, "");
  if (!h || !d) return false;
  if (d === "localhost" || /^[0-9.]+$/.test(d)) return h === d;
  return h === d || h.endsWith(`.${d}`);
}

function sendNative(payload) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendNativeMessage(HOST_NAME, payload, (resp) => {
      if (chrome.runtime.lastError) {
        reject(new Error(chrome.runtime.lastError.message));
        return;
      }
      resolve(resp);
    });
  });
}

async function readTabLocalStorage(tabId, expectedOrigin) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const items = [];
        let origin = "";
        let inaccessible = false;
        try {
          origin = window.location.origin || "";
          for (let i = 0; i < window.localStorage.length; i += 1) {
            const key = window.localStorage.key(i);
            if (key !== null) {
              items.push({ name: key, value: window.localStorage.getItem(key) ?? "" });
            }
          }
        } catch (storageErr) {
          inaccessible = Boolean(storageErr);
        }
        return { origin, items, inaccessible };
      },
    });
    const res = results?.[0]?.result;
    if (!res || typeof res.origin !== "string" || !Array.isArray(res.items)) {
      return null;
    }
    if (res.origin.toLowerCase() !== expectedOrigin.toLowerCase()) {
      throw new Error("Tab navigated to a different origin; reopen popup and retry.");
    }
    return res;
  } catch (err) {
    if (err && String(err.message || "").includes("different origin")) {
      throw err;
    }
    return null;
  }
}

function mapChromeCookie(c) {
  let sameSite;
  if (c.sameSite === "strict") sameSite = "Strict";
  else if (c.sameSite === "lax") sameSite = "Lax";
  else if (c.sameSite === "no_restriction") sameSite = "None";

  const out = {
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    httpOnly: Boolean(c.httpOnly),
    secure: Boolean(c.secure),
  };
  if (typeof c.expirationDate === "number" && Number.isFinite(c.expirationDate)) {
    out.expires = c.expirationDate;
  }
  if (sameSite) out.sameSite = sameSite;
  return out;
}

async function syncOneTab(tab, domain, includeParent) {
  const cookieQuery = includeParent ? { domain } : { url: tab.url };
  if (tab.cookieStoreId) {
    cookieQuery.storeId = tab.cookieStoreId;
  }
  const rawCookies = await chrome.cookies.getAll(cookieQuery);
  const cookies = rawCookies.map(mapChromeCookie);
  const parsed = new URL(tab.url);
  const lsRes = await readTabLocalStorage(tab.id, parsed.origin);
  const origins = lsRes && lsRes.origin
    ? [{ origin: lsRes.origin, localStorage: lsRes.items }]
    : [];
  const resp = await sendNative({ action: "sync", domain, cookies, origins });
  if (!resp?.ok) {
    throw new Error(resp?.error || "Sync failed");
  }
  return resp;
}

document.addEventListener("DOMContentLoaded", async () => {
  let lang = detectInitialLang();
  const t = () => I18N[lang];

  const el = {
    verTag: document.getElementById("verTag"),
    langBtn: document.getElementById("langBtn"),
    hostBadge: document.getElementById("hostBadge"),
    hostBadgeText: document.getElementById("hostBadgeText"),
    lblActiveSite: document.getElementById("lblActiveSite"),
    siteSyncedChip: document.getElementById("siteSyncedChip"),
    domainText: document.getElementById("domainText"),
    siteSubText: document.getElementById("siteSubText"),
    subToggleRow: document.getElementById("subToggleRow"),
    rootDomainToggle: document.getElementById("rootDomainToggle"),
    rootDomainLabel: document.getElementById("rootDomainLabel"),
    syncBtn: document.getElementById("syncBtn"),
    syncIcon: document.getElementById("syncIcon"),
    syncBtnText: document.getElementById("syncBtnText"),
    clearBtn: document.getElementById("clearBtn"),
    statusMsg: document.getElementById("statusMsg"),
    setupCard: document.getElementById("setupCard"),
    lblSetup: document.getElementById("lblSetup"),
    setupHint: document.getElementById("setupHint"),
    registerCmd: document.getElementById("registerCmd"),
    copyCmdBtn: document.getElementById("copyCmdBtn"),
    retryBtn: document.getElementById("retryBtn"),
    otherTabsCard: document.getElementById("otherTabsCard"),
    otherTabsToggle: document.getElementById("otherTabsToggle"),
    otherTabsTitle: document.getElementById("otherTabsTitle"),
    otherTabsBody: document.getElementById("otherTabsBody"),
    otherTabsList: document.getElementById("otherTabsList"),
    batchSyncBtn: document.getElementById("batchSyncBtn"),
    sessionsCard: document.getElementById("sessionsCard"),
    lblSynced: document.getElementById("lblSynced"),
    clearAllBtn: document.getElementById("clearAllBtn"),
    sessionsList: document.getElementById("sessionsList"),
    footerSecurity: document.getElementById("footerSecurity"),
    footerTotals: document.getElementById("footerTotals"),
  };

  const manifestVer = chrome.runtime.getManifest?.().version || "0.2.1";
  el.verTag.textContent = `v${manifestVer}`;

  const regCommand = chrome.runtime.id === DEFAULT_EXT_ID
    ? "sessfork-mcp register-host"
    : `sessfork-mcp register-host --extension-id ${chrome.runtime.id}`;
  el.registerCmd.textContent = regCommand;

  let activeTab = null;
  let exactHost = "";
  let rootDomain = "";
  let otherTabs = [];
  let hostReady = false;
  let domainStats = [];
  let totalCookies = 0;
  let clearAllTimer = null;

  const targetDomain = () => (el.rootDomainToggle.checked && rootDomain ? rootDomain : exactHost);

  function showFeedback(kind, text) {
    if (!text) {
      el.statusMsg.className = "feedback hidden";
      el.statusMsg.textContent = "";
      return;
    }
    el.statusMsg.className = `feedback ${kind}`;
    el.statusMsg.textContent = text;
  }

  function findStatsFor(domain) {
    if (!domain) return null;
    let cookies = 0;
    let keys = 0;
    let matched = false;
    for (const s of domainStats) {
      if (hostMatchesDomain(s.domain, domain)) {
        matched = true;
        cookies += s.cookies || 0;
        keys += s.localStorageKeys || 0;
      }
    }
    return matched ? { cookies, keys } : null;
  }

  function renderActiveSite() {
    const dom = targetDomain();
    el.lblActiveSite.textContent = t().activeSite;
    el.domainText.textContent = dom || t().unsupported;
    if (rootDomain && rootDomain !== exactHost) {
      el.subToggleRow.classList.remove("hidden");
      el.rootDomainLabel.textContent = t().includeParent(rootDomain);
    } else {
      el.subToggleRow.classList.add("hidden");
    }

    const st = findStatsFor(dom);
    if (dom && st) {
      el.siteSyncedChip.textContent = t().syncedChip;
      el.siteSyncedChip.classList.remove("hidden");
      el.siteSubText.textContent = t().syncedSub(st.cookies, st.keys);
      el.syncBtnText.textContent = t().resyncSite;
      el.clearBtn.classList.remove("hidden");
    } else {
      el.siteSyncedChip.classList.add("hidden");
      el.siteSubText.textContent = dom ? t().readySub : "";
      el.syncBtnText.textContent = t().forkSite;
      el.clearBtn.classList.add("hidden");
    }

    const canAct = hostReady && Boolean(activeTab && dom);
    el.syncBtn.disabled = !canAct;
    el.clearBtn.disabled = !canAct;
  }

  function renderOtherTabs() {
    if (!hostReady || otherTabs.length === 0) {
      el.otherTabsCard.classList.add("hidden");
      return;
    }
    el.otherTabsCard.classList.remove("hidden");
    el.otherTabsTitle.textContent = t().otherTabs(otherTabs.length);
    el.otherTabsList.textContent = "";

    for (const item of otherTabs) {
      const row = document.createElement("label");
      row.className = "tab-item";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = String(item.tab.id);
      cb.dataset.host = item.host;
      cb.addEventListener("change", updateBatchBtn);

      const domSpan = document.createElement("span");
      domSpan.className = "tab-domain";
      domSpan.textContent = item.host;
      domSpan.title = item.tab.title || item.host;

      row.appendChild(cb);
      row.appendChild(domSpan);

      if (findStatsFor(item.host)) {
        const badge = document.createElement("span");
        badge.className = "tab-synced";
        badge.textContent = t().syncedChip;
        row.appendChild(badge);
      }
      el.otherTabsList.appendChild(row);
    }
    updateBatchBtn();
  }

  function updateBatchBtn() {
    const checked = el.otherTabsList.querySelectorAll("input[type=checkbox]:checked").length;
    el.batchSyncBtn.textContent = t().forkSelected(checked);
    el.batchSyncBtn.disabled = !hostReady || checked === 0;
  }

  function renderSessionsList() {
    if (!hostReady) {
      el.sessionsCard.classList.add("hidden");
      return;
    }
    el.sessionsCard.classList.remove("hidden");
    el.lblSynced.textContent = t().syncedSessions;
    el.sessionsList.textContent = "";

    if (domainStats.length === 0) {
      el.clearAllBtn.classList.add("hidden");
      const empty = document.createElement("div");
      empty.className = "empty-hint";
      empty.textContent = t().noSessions;
      el.sessionsList.appendChild(empty);
      return;
    }

    el.clearAllBtn.classList.remove("hidden");
    if (!clearAllTimer) {
      el.clearAllBtn.textContent = t().clearAll;
      el.clearAllBtn.classList.remove("confirming");
    }

    for (const s of domainStats) {
      const row = document.createElement("div");
      row.className = "session-item";

      const info = document.createElement("div");
      info.className = "session-info";
      const domEl = document.createElement("div");
      domEl.className = "session-dom";
      domEl.textContent = s.domain;
      const cntEl = document.createElement("div");
      cntEl.className = "session-counts";
      cntEl.textContent = `${s.cookies} cookies · ${s.localStorageKeys} keys`;
      info.appendChild(domEl);
      info.appendChild(cntEl);

      const rmBtn = document.createElement("button");
      rmBtn.type = "button";
      rmBtn.className = "icon-btn";
      rmBtn.setAttribute("aria-label", t().removeAria(s.domain));
      rmBtn.innerHTML =
        '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
      rmBtn.addEventListener("click", async () => {
        rmBtn.disabled = true;
        try {
          const resp = await sendNative({ action: "clear", domain: s.domain });
          if (!resp?.ok) throw new Error(resp?.error || "Clear failed");
          showFeedback("ok", t().clearedSite(s.domain));
          await refreshHostStatus();
        } catch (err) {
          showFeedback("err", err.message);
          rmBtn.disabled = false;
        }
      });

      row.appendChild(info);
      row.appendChild(rmBtn);
      el.sessionsList.appendChild(row);
    }
  }

  function renderAllText() {
    el.langBtn.textContent = t().langBtn;
    el.lblSetup.textContent = t().setupTitle;
    el.setupHint.textContent = t().setupHint;
    el.copyCmdBtn.textContent = t().copyCmd;
    el.retryBtn.textContent = t().retry;
    el.footerSecurity.textContent = t().footerSecurity;
    el.hostBadgeText.textContent = hostReady ? t().connected : t().setupNeeded;
    el.footerTotals.textContent = hostReady ? t().totals(domainStats.length, totalCookies) : "";
    renderActiveSite();
    renderOtherTabs();
    renderSessionsList();
  }

  async function loadTabs() {
    try {
      const tabs = await chrome.tabs.query({ currentWindow: true });
      const current = tabs.find((tb) => tb.active);
      if (current?.url && /^https?:\/\//i.test(current.url)) {
        const parsed = new URL(current.url);
        activeTab = current;
        exactHost = parsed.hostname.toLowerCase();
        const candRoot = computeRegistrableDomain(exactHost);
        rootDomain = isUnsafePublicSuffix(candRoot) ? "" : candRoot;
      }

      const seenHosts = new Set(exactHost ? [exactHost] : []);
      otherTabs = [];
      for (const tb of tabs) {
        if (!tb.url || tb.active || !/^https?:\/\//i.test(tb.url)) continue;
        try {
          const host = new URL(tb.url).hostname.toLowerCase();
          if (host && !seenHosts.has(host)) {
            seenHosts.add(host);
            otherTabs.push({ tab: tb, host });
          }
        } catch (urlErr) {
          console.warn("sessfork skipped malformed tab URL:", urlErr);
        }
      }
    } catch (tabErr) {
      showFeedback("err", `Could not inspect tabs: ${tabErr?.message || tabErr}`);
    }
  }

  async function refreshHostStatus() {
    try {
      const resp = await sendNative({ action: "status" });
      if (!resp?.ok) throw new Error(resp?.error || "Host returned error");
      hostReady = true;
      el.hostBadge.className = "status-pill ok";
      el.setupCard.classList.add("hidden");
      totalCookies = resp.cookieCount ?? 0;
      if (Array.isArray(resp.domainStats)) {
        domainStats = resp.domainStats;
      } else if (Array.isArray(resp.domains)) {
        domainStats = resp.domains.map((d) => ({ domain: d, cookies: 0, localStorageKeys: 0 }));
      } else {
        domainStats = [];
      }
    } catch (err) {
      hostReady = false;
      domainStats = [];
      totalCookies = 0;
      el.hostBadge.className = "status-pill err";
      el.setupCard.classList.remove("hidden");
      showFeedback("err", err.message);
    }
    renderAllText();
  }

  el.langBtn.addEventListener("click", () => {
    lang = lang === "zh" ? "en" : "zh";
    try {
      window.localStorage.setItem("sessfork_ui_lang", lang);
    } catch (err) {
      console.warn("sessfork lang preference save skipped:", err);
    }
    renderAllText();
  });

  el.rootDomainToggle.addEventListener("change", renderActiveSite);

  el.copyCmdBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(regCommand);
      el.copyCmdBtn.textContent = t().copied;
      setTimeout(() => { el.copyCmdBtn.textContent = t().copyCmd; }, 1500);
    } catch (err) {
      showFeedback("err", `Clipboard unavailable: ${err?.message || err}`);
    }
  });

  el.retryBtn.addEventListener("click", async () => {
    showFeedback("", "");
    await refreshHostStatus();
  });

  el.otherTabsToggle.addEventListener("click", () => {
    const expanded = el.otherTabsToggle.getAttribute("aria-expanded") === "true";
    el.otherTabsToggle.setAttribute("aria-expanded", String(!expanded));
    el.otherTabsBody.classList.toggle("hidden", expanded);
  });

  el.syncBtn.addEventListener("click", async () => {
    const domain = targetDomain();
    if (!domain || !activeTab) return;
    el.syncBtn.disabled = true;
    el.syncIcon.classList.add("spin");
    showFeedback("info", t().syncing(domain));
    try {
      const resp = await syncOneTab(
        activeTab,
        domain,
        Boolean(el.rootDomainToggle.checked && rootDomain)
      );
      showFeedback(
        "ok",
        t().syncedOk(
          resp.domain || domain,
          resp.cookiesSynced ?? 0,
          resp.localStorageKeysSynced ?? 0
        )
      );
      await refreshHostStatus();
    } catch (err) {
      showFeedback("err", err.message);
    } finally {
      el.syncIcon.classList.remove("spin");
      el.syncBtn.disabled = !hostReady;
    }
  });

  el.clearBtn.addEventListener("click", async () => {
    const domain = targetDomain();
    if (!domain) return;
    el.clearBtn.disabled = true;
    try {
      const resp = await sendNative({ action: "clear", domain });
      if (!resp?.ok) throw new Error(resp?.error || "Clear failed");
      showFeedback("ok", t().clearedSite(domain));
      await refreshHostStatus();
    } catch (err) {
      showFeedback("err", err.message);
    } finally {
      el.clearBtn.disabled = !hostReady;
    }
  });

  el.batchSyncBtn.addEventListener("click", async () => {
    const boxes = Array.from(el.otherTabsList.querySelectorAll("input[type=checkbox]:checked"));
    if (boxes.length === 0) return;
    el.batchSyncBtn.disabled = true;
    let okCount = 0;
    let lastErr = "";
    for (const cb of boxes) {
      const tabId = Number(cb.value);
      const item = otherTabs.find((x) => x.tab.id === tabId);
      if (!item) continue;
      try {
        await syncOneTab(item.tab, item.host, false);
        okCount += 1;
      } catch (err) {
        lastErr = `${item.host}: ${err.message}`;
      }
    }
    if (okCount === boxes.length) {
      showFeedback("ok", t().batchDone(okCount, boxes.length));
    } else {
      showFeedback("err", `${t().batchDone(okCount, boxes.length)} (${lastErr})`);
    }
    await refreshHostStatus();
  });

  el.clearAllBtn.addEventListener("click", async () => {
    if (!clearAllTimer) {
      el.clearAllBtn.textContent = t().confirmClearAll;
      el.clearAllBtn.classList.add("confirming");
      clearAllTimer = setTimeout(() => {
        clearAllTimer = null;
        el.clearAllBtn.textContent = t().clearAll;
        el.clearAllBtn.classList.remove("confirming");
      }, 4000);
      return;
    }
    clearTimeout(clearAllTimer);
    clearAllTimer = null;
    try {
      const resp = await sendNative({ action: "clear", all: true });
      if (!resp?.ok) throw new Error(resp?.error || "Clear failed");
      showFeedback("ok", t().clearedAll);
      await refreshHostStatus();
    } catch (err) {
      showFeedback("err", err.message);
    }
  });

  await loadTabs();
  await refreshHostStatus();
});
