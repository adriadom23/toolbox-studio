// Toolbox Studio's own, deliberately trimmed ``api/http.js``.
//
// The real ToolBox file is 5273 lines exporting ~500 functions — almost
// all of them request-builders for specific ToolBox tools (Wave Plan,
// Mensajes XMDO, the config wizard, ...) that have no meaning for a blank
// Studio project. This file keeps only the generic ``request()``/
// ``requestInner()`` fetch core (retry/timeout/``okStatuses`` handling)
// plus the small set of functions a vendored component or store actually
// calls. See design.md §2.5 for the full reasoning and the kept-function
// table this file implements.
//
// Three calls the real core makes on every/some requests are deliberately
// stripped out, along with their three source files (none vendored):
//   - ``notifyProcessingStart``/``notifyProcessingEnd`` (global
//     "Procesando..." button overlay) — processingButtonFeedback.js
//   - ``captureSendContext`` (green send-check flash) — sendButtonFeedback.js
//   - ``setHubSlackConfigured`` (HUB-Slack-availability UI flag) —
//     hubSlackStatus.js
// None of these three behaviors has any meaning for a blank Studio
// project, so the calls are removed rather than carrying the files that
// back them.
import { getCurrentLocale, useI18n } from "../i18n";

const { t: i18nText } = useI18n();

const _viteEnv = typeof import.meta !== "undefined" ? import.meta.env ?? {} : {};
const API_BASE_URL = _viteEnv.VITE_TOOLBOX_API_URL ?? "/api";
const API_ORIGIN = API_BASE_URL.startsWith("http")
  ? API_BASE_URL.replace(/\/api\/?$/, "")
  : (typeof window !== "undefined" ? window.location.origin : "");

function buildApiUrl(path) {
  if (!path) return "";
  if (/^https?:\/\//i.test(path)) return path;
  if (path.startsWith("/api/")) return `${API_ORIGIN}${path}`;
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

// Inject ``locale`` into JSON POST bodies so a Studio backend route can
// translate user-facing prose without each caller plumbing the locale
// through manually.
function injectLocaleIntoBody(options) {
  if (!options || typeof options !== "object") return options;
  const method = String(options.method ?? "GET").toUpperCase();
  if (method === "GET" || method === "HEAD") return options;
  if (typeof options.body !== "string") return options;
  let parsed;
  try {
    parsed = JSON.parse(options.body);
  } catch (_err) {
    return options;
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    return options;
  }
  if (parsed.locale) return options;
  return { ...options, body: JSON.stringify({ ...parsed, locale: getCurrentLocale() }) };
}

// Some backends return HTTP 200 with a payload that still signals a
// logical failure (e.g. ``{status: "failed"}`` or ``{ok: false}``).
function payloadFlagsFailure(payload) {
  if (!payload || typeof payload !== "object") return false;
  if (payload.status === "failed" || payload.status === "error") return true;
  if (payload.ok === false || payload.success === false) return true;
  return false;
}

// Exported (design.md §2.5 describes http.js as keeping "the generic
// request()/requestInner() fetch core"): the scaffolded project .vue
// (design.md §6.3) does `import { request } from "../../shared/api/http"`
// and calls request(pingPath), so the generic core must be a named export
// for a scaffolded project to build. (FEAT-004 fix: FEAT-002 left the
// `export` off this one symbol, which broke `npm run build` on the
// scaffold output — see FEAT-004 findings.)
export async function request(path, options = {}) {
  return requestInner(path, options);
}

async function requestInner(path, options = {}) {
  // Detach option keys that are NOT valid ``fetch`` init options so they
  // are not forwarded to the platform fetch (which would warn).
  const { okStatuses, timeoutMs, retryNetwork = true, ...fetchOptions } = options || {};
  const finalOptions = injectLocaleIntoBody(fetchOptions);
  const allowedStatuses = Array.isArray(okStatuses) ? okStatuses : null;
  const maxAttempts = retryNetwork ? 2 : 1;

  // When the body is ``FormData`` (file uploads) the browser MUST set
  // ``Content-Type: multipart/form-data; boundary=...`` itself. Forcing
  // ``application/json`` here breaks Flask's multipart parsing.
  const isFormDataBody =
    typeof FormData !== "undefined" && finalOptions.body instanceof FormData;

  let lastError = null;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const controller =
      Number.isFinite(timeoutMs) && timeoutMs > 0 ? new AbortController() : null;
    const abortTimer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null;
    try {
      const response = await fetch(`${API_BASE_URL}${path}`, {
        headers: {
          ...(isFormDataBody ? {} : { "Content-Type": "application/json" }),
          ...(finalOptions.headers ?? {}),
        },
        ...finalOptions,
        ...(controller ? { signal: controller.signal } : {}),
      });

      const payload = await response.json().catch(() => ({}));
      const statusOk =
        response.ok || (allowedStatuses && allowedStatuses.includes(response.status));
      if (!statusOk) {
        const err = new Error(payload.message || `API error ${response.status}`);
        if (payload.reason) err.reason = payload.reason;
        if (payload.action) err.action = payload.action;
        if (payload.message) err.message = payload.message;
        err.payload = payload;
        err.status = response.status;
        // A 5xx with an empty body is the proxy reporting that the backend
        // connection dropped mid-request (reload/reset) — not a real API
        // response. Flag it so the retry logic below treats it like a
        // network-level failure instead of a final answer.
        err.transient = response.status >= 500 && !payload.message;
        throw err;
      }
      // A logical failure flagged inside a 2xx (or an explicitly allowed
      // status) payload is still returned as-is — the caller decides what
      // to do with it (e.g. a status that okStatuses allows through).
      void payloadFlagsFailure(payload);
      return payload;
    } catch (error) {
      const isTimeout = controller && error?.name === "AbortError";
      const isNetworkFailure =
        error instanceof TypeError || error.transient === true || isTimeout;
      if (!isNetworkFailure || attempt >= maxAttempts - 1) {
        if (isTimeout) {
          throw new Error(i18nText("components.http.timeout"));
        }
        if (isNetworkFailure) {
          throw new Error(i18nText("components.http.networkError"));
        }
        throw error;
      }
      lastError = error;
      // Brief back-off so the bundle has time to finish the previous
      // I/O before the retry hits.
      await new Promise((resolve) => setTimeout(resolve, 1500));
    } finally {
      if (abortTimer) clearTimeout(abortTimer);
    }
  }
  throw lastError || new Error("API request failed");
}

// ---------------------------------------------------------------------------
// Kept functions — see design.md §2.5 for why each one is here and which
// vendored component/store calls it.
// ---------------------------------------------------------------------------

export function getHealth() {
  return request("/health");
}

// Ask the local backend to open ``url`` in the operator's Firefox, even
// when the Studio SPA itself is running in another browser (Chrome/Edge).
// The vendored integration clients authenticate by reading cookies from
// the Firefox profile, so identification tabs must land there. Returns
// ``{ok, opened, error}``; never throws (a failure resolves to
// ``{ok:false, opened:false}`` so the caller can fall back).
export async function openInFirefox(url) {
  try {
    const response = await fetch(buildApiUrl("/api/browser/open-firefox"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    return await response.json().catch(() => ({ ok: false, opened: false }));
  } catch (_e) {
    return { ok: false, opened: false };
  }
}

// New, Studio-authored function: the real ToolBox http.js has no single
// generic "status of connection X" call (each ToolBox-specific source has
// its own function/endpoint) — Studio's rewritten globalConnections.js
// (see shared/stores/globalConnections.js) needs exactly this shape.
export function connectionStatus(id) {
  return request(`/connections/${encodeURIComponent(id)}/status`);
}

// ---------------------------------------------------------------------------
// Plan Manifest panel (per station + cycle) — backs PlanManifestPanel.vue
// ---------------------------------------------------------------------------

export function manifestCycles() {
  return request("/manifest/cycles");
}

export function manifestCookiesStatus(options = {}) {
  return request("/manifest/cookies-status", options);
}

export function manifestStatus({ date = "" } = {}) {
  const params = new URLSearchParams();
  if (date) params.set("date", date);
  const suffix = params.toString() ? `?${params.toString()}` : "";
  return request(`/manifest/status${suffix}`);
}

export async function manifestUpload({ file, stationCode, cycle, date } = {}) {
  if (!file) throw new Error("file requerido");
  if (!stationCode) throw new Error("stationCode requerido");
  const formData = new FormData();
  formData.append("file", file);
  formData.append("station_code", stationCode);
  if (cycle) formData.append("cycle", cycle);
  if (date) formData.append("date", date);
  const response = await fetch(`${API_BASE_URL}/manifest/upload`, {
    method: "POST",
    body: formData,
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(result.message || `API error ${response.status}`);
  }
  return result;
}

export function manifestFetch({ stationCode, cycle, date, overwrite } = {}) {
  return request("/manifest/fetch", {
    method: "POST",
    body: JSON.stringify({
      station_code: stationCode || "",
      cycle: cycle || "",
      date: date || "",
      overwrite: overwrite ?? true,
    }),
  });
}
