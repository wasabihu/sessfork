"use strict";

const HOST_NAME = "com.wasabihu.sessfork";

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

async function readActiveTabLocalStorage(tabId, expectedOrigin) {
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
    // Guard against mid-open tab navigation (e.g. SSO redirect) attaching wrong origin's storage.
    if (res.origin.toLowerCase() !== expectedOrigin.toLowerCase()) {
      throw new Error("Active tab navigated to a different origin; reopen popup and retry.");
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

document.addEventListener("DOMContentLoaded", async () => {
  const hostBadge = document.getElementById("hostBadge");
  const domainText = document.getElementById("domainText");
  const subToggleRow = document.getElementById("subToggleRow");
  const rootDomainToggle = document.getElementById("rootDomainToggle");
  const rootDomainLabel = document.getElementById("rootDomainLabel");
  const syncBtn = document.getElementById("syncBtn");
  const clearBtn = document.getElementById("clearBtn");
  const statusMsg = document.getElementById("statusMsg");
  const setupCard = document.getElementById("setupCard");
  const registerCmd = document.getElementById("registerCmd");
  const copyCmdBtn = document.getElementById("copyCmdBtn");
  const summaryText = document.getElementById("summaryText");

  const regCommand = `sessfork-mcp register-host --extension-id ${chrome.runtime.id}`;
  registerCmd.textContent = regCommand;
  copyCmdBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(regCommand);
      copyCmdBtn.textContent = "Copied!";
      setTimeout(() => { copyCmdBtn.textContent = "Copy Registration Command"; }, 1500);
    } catch (_) {
      copyCmdBtn.textContent = "Select & Copy Above";
    }
  });

  let activeTab = null;
  let exactHost = "";
  let rootDomain = "";
  let activeOrigin = "";
  let hostReady = false;

  const targetDomain = () => (rootDomainToggle.checked && rootDomain ? rootDomain : exactHost);
  const updateDomainDisplay = () => {
    domainText.textContent = targetDomain() || "Unsupported page (open an http/https tab)";
  };

  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab?.url && /^https?:\/\//i.test(tab.url)) {
      const parsed = new URL(tab.url);
      activeTab = tab;
      exactHost = parsed.hostname.toLowerCase();
      const candRoot = computeRegistrableDomain(exactHost);
      rootDomain = isUnsafePublicSuffix(candRoot) ? "" : candRoot;
      activeOrigin = parsed.origin;
      if (rootDomain && rootDomain !== exactHost) {
        subToggleRow.classList.remove("hidden");
        rootDomainLabel.textContent = `Also include parent domain (${rootDomain})`;
      }
    }
  } catch (tabErr) {
    statusMsg.textContent = `Could not inspect active tab: ${tabErr?.message || tabErr}`;
    statusMsg.className = "msg err";
  }
  updateDomainDisplay();
  rootDomainToggle.addEventListener("change", updateDomainDisplay);

  async function refreshHostStatus() {
    try {
      const resp = await sendNative({ action: "status" });
      if (resp?.ok) {
        hostReady = true;
        hostBadge.textContent = "Connected";
        hostBadge.className = "badge ok";
        setupCard.classList.add("hidden");
        const count = Array.isArray(resp.domains) ? resp.domains.length : 0;
        summaryText.textContent = `Synced: ${count} domain(s) · ${resp.cookieCount ?? 0} cookies · ${resp.originCount ?? 0} origins`;
      } else {
        throw new Error(resp?.error || "Host returned error");
      }
    } catch (err) {
      hostReady = false;
      hostBadge.textContent = "Setup needed";
      hostBadge.className = "badge err";
      setupCard.classList.remove("hidden");
      statusMsg.textContent = err.message;
      statusMsg.className = "msg err";
    }
    const canAct = hostReady && Boolean(activeTab && targetDomain());
    syncBtn.disabled = !canAct;
    clearBtn.disabled = !canAct;
  }

  await refreshHostStatus();

  syncBtn.addEventListener("click", async () => {
    const domain = targetDomain();
    if (!domain || !activeTab) return;
    syncBtn.disabled = true;
    statusMsg.textContent = `Syncing ${domain}…`;
    statusMsg.className = "msg";
    try {
      const cookieQuery = rootDomainToggle.checked && rootDomain
        ? { domain }
        : { url: activeTab.url };
      if (activeTab.cookieStoreId) {
        cookieQuery.storeId = activeTab.cookieStoreId;
      }
      const rawCookies = await chrome.cookies.getAll(cookieQuery);
      const cookies = rawCookies.map(mapChromeCookie);
      const lsRes = await readActiveTabLocalStorage(activeTab.id, activeOrigin);
      const origins = lsRes && lsRes.origin
        ? [{ origin: lsRes.origin, localStorage: lsRes.items }]
        : [];
      const resp = await sendNative({ action: "sync", domain, cookies, origins });
      if (!resp?.ok) throw new Error(resp?.error || "Sync failed");
      statusMsg.textContent = `✓ Synced ${resp.cookiesSynced} cookies + ${resp.localStorageKeysSynced} localStorage keys for ${resp.domain}`;
      statusMsg.className = "msg ok";
      await refreshHostStatus();
    } catch (err) {
      statusMsg.textContent = `Sync failed: ${err.message}`;
      statusMsg.className = "msg err";
    } finally {
      syncBtn.disabled = !hostReady;
    }
  });

  clearBtn.addEventListener("click", async () => {
    const domain = targetDomain();
    if (!domain) return;
    clearBtn.disabled = true;
    try {
      const resp = await sendNative({ action: "clear", domain });
      if (!resp?.ok) throw new Error(resp?.error || "Clear failed");
      statusMsg.textContent = `Cleared saved session for ${domain}`;
      statusMsg.className = "msg ok";
      await refreshHostStatus();
    } catch (err) {
      statusMsg.textContent = `Clear failed: ${err.message}`;
      statusMsg.className = "msg err";
    } finally {
      clearBtn.disabled = !hostReady;
    }
  });
});
