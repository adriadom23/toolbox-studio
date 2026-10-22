// ---------------------------------------------------------------------------
// Reactive click-to-sort for Vue-rendered tables.
//
// Unlike the global dblclick helper (tableDblClickSort.js, a DOM-only
// re-order that Vue undoes on the next render), this sorts the DATA, so the
// order survives re-renders (polls, filters, reactive updates). Tables using
// it carry ``data-no-dblsort`` so both mechanisms never fight.
//
// Usage:
//   const sorter = useTableSort({ route: (r) => r.route_code, failed: (r) => r.failed_pkgs });
//   const rows = computed(() => sorter.sortRows(baseRows.value));
//   <SortTh :sorter="sorter" k="route">Route</SortTh>
//
// ``accessors`` can also be a function ``(key) => (row) => value`` for
// dynamic columns (one per week, …). First click = ascending (A→Z / 0→9),
// same column again flips. Empty values always go last. With no column
// selected the incoming order is kept untouched.
// ---------------------------------------------------------------------------

import { reactive } from "vue";

const EMPTY_TEXT = /^(?:|—|–|-|n\/?a|n\/?d|null|none)$/i;

function normalize(value) {
  if (value === null || value === undefined) return { empty: true };
  if (typeof value === "boolean") return { num: value ? 1 : 0 };
  if (typeof value === "number") return Number.isFinite(value) ? { num: value } : { empty: true };
  const text = String(value).trim();
  if (EMPTY_TEXT.test(text)) return { empty: true };
  return { text };
}

export function compareSortValues(a, b) {
  const x = normalize(a);
  const y = normalize(b);
  if (x.empty || y.empty) return Number(Boolean(x.empty)) - Number(Boolean(y.empty));
  const xNum = typeof x.num === "number";
  const yNum = typeof y.num === "number";
  if (xNum && yNum) return x.num - y.num;
  if (xNum) return -1;
  if (yNum) return 1;
  return x.text.localeCompare(y.text, undefined, { numeric: true, sensitivity: "base" });
}

function isEmpty(value) {
  return Boolean(normalize(value).empty);
}

export function useTableSort(accessors = {}) {
  const state = reactive({ key: "", dir: "asc" });

  function accessorFor(key) {
    if (typeof accessors === "function") return accessors(key);
    return accessors[key] || ((row) => row?.[key]);
  }

  function toggle(key) {
    if (state.key === key) {
      state.dir = state.dir === "asc" ? "desc" : "asc";
    } else {
      state.key = key;
      state.dir = "asc";
    }
  }

  // Stable sort by the active column; ``desc`` flips the comparison but
  // never the empty-last rule. Returns a NEW array (never mutates input).
  function sortRows(rows) {
    const list = Array.isArray(rows) ? rows : [];
    if (!state.key) return list;
    const get = accessorFor(state.key);
    const sign = state.dir === "desc" ? -1 : 1;
    return list
      .map((row, i) => ({ row, i, value: get(row) }))
      .sort((a, b) => {
        const cmp = compareSortValues(a.value, b.value);
        if (cmp === 0) return a.i - b.i;
        return isEmpty(a.value) || isEmpty(b.value) ? cmp : cmp * sign;
      })
      .map((entry) => entry.row);
  }

  function ariaSort(key) {
    if (state.key !== key) return "none";
    return state.dir === "asc" ? "ascending" : "descending";
  }

  function indicator(key) {
    if (state.key !== key) return "";
    return state.dir === "asc" ? "▲" : "▼";
  }

  return { state, toggle, sortRows, ariaSort, indicator };
}
