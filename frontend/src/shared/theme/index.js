import { computed, ref } from "vue";

const STORAGE_KEY = "toolbox.theme";
const VALID_THEMES = ["light", "dark"];

function readStored() {
  try {
    const value = localStorage.getItem(STORAGE_KEY);
    return VALID_THEMES.includes(value) ? value : null;
  } catch (error) {
    return null;
  }
}

function preferredTheme() {
  if (typeof window === "undefined" || !window.matchMedia) return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveInitial() {
  return readStored() ?? preferredTheme();
}

function applyTheme(theme) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
}

const currentTheme = ref(resolveInitial());

/**
 * Apply the theme as soon as possible (call from main.js before mounting).
 * Re-applying is safe.
 */
export function initTheme() {
  applyTheme(currentTheme.value);
}

export function useTheme() {
  const theme = computed(() => currentTheme.value);
  const isDark = computed(() => currentTheme.value === "dark");

  function setTheme(next) {
    if (!VALID_THEMES.includes(next)) return;
    currentTheme.value = next;
    applyTheme(next);
    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch (error) {
      /* ignore quota / privacy mode */
    }
  }

  function toggleTheme() {
    setTheme(currentTheme.value === "dark" ? "light" : "dark");
  }

  return {
    theme,
    isDark,
    setTheme,
    toggleTheme,
  };
}
