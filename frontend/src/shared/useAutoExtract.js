import { onMounted, watchEffect } from "vue";

/**
 * Generic "auto-run on open" helper.
 *
 * Fires ``run()`` at most ONCE per mount, as soon as BOTH:
 *   1. ``ready()`` is true (the session/source is live), and
 *   2. the optional ``when()`` guard is true (e.g. the tracking IDs or
 *      manifest the extraction needs have finished loading).
 *
 * Multi-source tools can pass ``key()`` to re-arm the trigger for a changed
 * scope or another source. A disconnected -> connected transition also
 * re-arms one run automatically, so recovery after login does not depend on
 * every consumer implementing its own watcher. While readiness remains true,
 * each scope still fires at most once.
 *
 * Session probing is deliberately outside this helper. Automatic runs react
 * to ``ready()`` but never open login tabs: only explicit connect/reconnect
 * gestures may do that.
 *
 * Tools are remounted on every open (keyed ``ToolWorkspace``), so "once per
 * mount" means "once per visit". The manual "Extraer" button stays as the
 * retry/re-extract path; any auto-run failure is swallowed so the tool's own
 * error handling + manual flow take over.
 *
 * ``run`` must be idempotent-guarded (all current handlers carry their own
 * ``*Extracting``/``*Running`` flag, so re-entry is already safe).
 *
 * @param {{
 *   run: () => (void | Promise<void>),
 *   ready?: () => boolean,
 *   when?: () => boolean,
 *   key?: (() => string) | null,
 * }} options
 */
export function useAutoRun({ run, ready = () => true, when = () => true, key = null }) {
  const firedKeys = new Set();
  let wasReady = false;
  let readyGeneration = 0;

  // Reactive trigger: re-evaluates whenever any ref read by ``ready()`` /
  // ``when()`` / ``key()`` changes. A disconnected -> connected transition
  // receives a fresh generation, so a source that reconnects after the first
  // attempt gets exactly one new automatic extraction without retry loops
  // while it remains green.
  watchEffect(() => {
    const isReady = Boolean(ready());
    if (!isReady) {
      wasReady = false;
      return;
    }
    if (!wasReady) readyGeneration += 1;
    wasReady = true;
    if (!when()) return;
    const scope = key ? key() : "";
    const fireKey = `${scope}\u0000${readyGeneration}`;
    if (firedKeys.has(fireKey)) return;
    firedKeys.add(fireKey);
    Promise.resolve()
      .then(run)
      .catch(() => {
        // Best-effort: on failure the card subtitles/manual button already
        // surface the error state, so the operator can retry by hand.
      });
  });
}

/**
 * SCC flavour: auto-run a tool's single "Extraer" action when the operator
 * opens the tool, wired to the ``useSccOnRoad`` composable.
 *
 * Usage in a tool (replaces the ``onMounted(scc.autoConnectScc)`` line):
 *   const scc = useSccOnRoad();
 *   useAutoExtract(scc, myExtractAllHandler, {
 *     when: () => trackingIds.value.length > 0,
 *   });
 *
 * @param {ReturnType<import("./stores/useSccOnRoad").useSccOnRoad>} scc
 * @param {() => (void | Promise<void>)} run  The tool's "Extraer" handler.
 * @param {{ when?: () => boolean }} [options]
 */
export function useAutoExtract(scc, run, { when = () => true } = {}) {
  // Probe only. A missing SCC session stays red until the operator explicitly
  // presses Connect/Identify; mount and auto-refresh must never open login tabs.
  onMounted(() => scc.checkSccStatus());
  useAutoRun({
    run,
    when,
    ready: () => scc.sccReady.value,
  });
}
