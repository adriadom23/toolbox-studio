import { openInFirefox } from "./api/http";

/**
 * Cross-browser login/identification tab opener.
 *
 * The Toolbox backend authenticates against Amazon internal tools by reading
 * SSO cookies from the operator's **Firefox** profile. So any login tab has to
 * land in Firefox — if the operator opened the Toolbox in Chrome/Edge and logs
 * in there, the backend never sees the session and every tool stays "red".
 *
 * A web page cannot launch a different browser (sandbox), but the local Flask
 * server can. This helper hides that split:
 *
 *   - Host browser IS Firefox  → plain ``window.open`` (callers keep the tab
 *     handle, used to auto-close the tab once the session is detected).
 *   - Host browser is anything else → ask the backend to spawn Firefox
 *     natively. No handle is available (returns ``null``); the poll-based
 *     reconnect loops still detect the session and stop on their own.
 */

// True when the Toolbox SPA is itself running inside Firefox.
export function isFirefox() {
  if (typeof navigator === "undefined") return true;
  const ua = navigator.userAgent || "";
  return /firefox\//i.test(ua) && !/seamonkey/i.test(ua);
}

function _openHere(url, name, features) {
  try {
    return window.open(url, name, features);
  } catch (_e) {
    /* popup blocked */
    return null;
  }
}

/**
 * Open ``url`` for identification, routing to Firefox when needed.
 *
 * @param {string} url        the login URL
 * @param {string} [name]     ``window.open`` target name (tab reuse)
 * @param {object} [opts]
 * @param {string} [opts.features]  ``window.open`` features string
 * @param {boolean} [opts.forceNative]  hint that the call is NOT backed by a
 *   fresh user click (auto-identify at boot, poll timers) or is one of several
 *   opens from a single click. Even then, when the host browser IS Firefox we
 *   still TRY ``window.open`` first: if it succeeds (operator allowed popups
 *   for the Toolbox, or the gesture is still fresh) we keep the tab handle so
 *   the reconnect loops can AUTO-CLOSE the tab once the session is detected.
 *   Tabs spawned through the backend launcher have no handle and can never be
 *   closed from here — that is why the launcher is only the fallback.
 * @param {boolean} [opts.noNativeFallback]  NEVER hand the URL to the backend
 *   Firefox launcher. Callers that can route a blocked open through a
 *   self-closing flow (e.g. the global connections work-tab runner) pass this
 *   so no raw ``firefox -new-tab`` — which has no handle and lingers open
 *   forever after the session connects — is ever spawned. When set, a blocked
 *   or non-Firefox-host open just returns ``null``.
 * @returns {Window|null} the tab handle when opened in-browser, else ``null``.
 */
export function openLoginTab(
  url,
  name = "_blank",
  { features = "", forceNative = false, noNativeFallback = false } = {},
) {
  if (!url) return null;

  // Host browser is Firefox: always attempt an in-browser open first, even
  // without a click gesture. A popup-blocked ``window.open`` just returns
  // ``null`` synchronously (no exception, no page disruption), and a
  // successful one gives us the ONLY thing that makes auto-close possible:
  // the tab handle. ``forceNative`` merely tells us a block is likely — it
  // must not skip the attempt, or the tab is guaranteed to linger forever.
  // Only when the open IS blocked do we hand the URL to the backend Firefox
  // launcher (no handle: that tab cannot be auto-closed).
  if (isFirefox()) {
    const handle = _openHere(url, name, features);
    if (handle) return handle;
    if (!noNativeFallback) openInFirefox(url).catch(() => {});
    return null;
  }

  if (noNativeFallback) return null;

  // Non-Firefox host: hand the URL to the backend Firefox launcher. If Firefox
  // is missing (opened === false) or the call fails, fall back to opening the
  // tab in the current browser so the operator is never left stuck. Note: the
  // fallback ``window.open`` runs async, so an aggressive popup blocker may
  // suppress it — but that only happens on machines with no Firefox at all,
  // where the cookie-based auth could not work anyway.
  openInFirefox(url)
    .then((res) => {
      if (!res || res.opened === false) _openHere(url, name, features);
    })
    .catch(() => _openHere(url, name, features));

  return null;
}
