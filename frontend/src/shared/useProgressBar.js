// Shared simulated-progress helper for tool "Procesar" actions.
//
// Most tool backend calls are a single synchronous request, so we cannot
// stream a real percentage. To still give the operator a moving progress
// bar with a percentage (instead of a bare spinner), this composable
// animates an asymptotic ramp toward ~90% while the request is in flight
// and then snaps to 100% when the caller signals completion.
//
// It mirrors the hand-rolled ``startProgress/completeProgress/stopProgress``
// pattern already used by Bolsas/DPR/etc., but as one reusable unit so
// every tool can show the same percentage bar with a single import.
//
// Usage:
//   import { useProgressBar } from "../../shared/useProgressBar";
//   const { progress, startProgress, completeProgress, resetProgress } =
//     useProgressBar();
//   ...
//   isProcessing.value = true;
//   startProgress();              // optionally startProgress(stages)
//   try {
//     ... await runTool(...) ...
//     completeProgress();
//   } finally {
//     isProcessing.value = false; // bar hides with v-if="isProcessing"
//   }
//
// Template:
//   <LoadingBar
//     v-if="isProcessing"
//     :active="isProcessing"
//     :percent="progress.percent"
//     :label="t('common.processing')"
//     :sublabel="progress.stage"
//     variant="panel"
//   />

import { onBeforeUnmount, reactive } from "vue";

export function useProgressBar({ tickMs = 250, ceiling = 90, ramp = 2600 } = {}) {
  // ``percent`` starts at null so the bar can render indeterminate before
  // the first tick, then becomes a number as soon as progress begins.
  const progress = reactive({ percent: null, stage: "" });
  let handle = null;

  function stopProgress() {
    if (handle) {
      clearInterval(handle);
      handle = null;
    }
  }

  // ``stages`` (optional): array of { at, percent, label } where ``at`` is
  // elapsed ms. When provided we step through the named stages (matches the
  // existing Bolsas pattern). When omitted we use a smooth asymptotic ramp
  // toward ``ceiling`` so the number always keeps moving.
  function startProgress(stages = null) {
    stopProgress();
    progress.percent = 0;
    progress.stage = "";
    const started = Date.now();
    const apply = () => {
      if (progress.percent >= 100) return;
      const elapsed = Date.now() - started;
      if (Array.isArray(stages) && stages.length) {
        let current = stages[0];
        for (const stage of stages) {
          if (stage.at <= elapsed) current = stage;
        }
        progress.percent = current.percent;
        progress.stage = current.label || "";
      } else {
        const value = ceiling * (1 - Math.exp(-elapsed / ramp));
        progress.percent = Math.min(ceiling, Math.round(value));
      }
    };
    apply();
    handle = setInterval(apply, tickMs);
  }

  function completeProgress(stage = "") {
    stopProgress();
    progress.percent = 100;
    if (stage) progress.stage = stage;
  }

  function resetProgress() {
    stopProgress();
    progress.percent = null;
    progress.stage = "";
  }

  onBeforeUnmount(stopProgress);

  return { progress, startProgress, completeProgress, resetProgress, stopProgress };
}
