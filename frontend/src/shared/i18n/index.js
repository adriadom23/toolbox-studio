import { computed, ref } from "vue";

import { messages } from "./messages";

export const availableLocales = [
  { code: "es", label: "ES", name: "Español", flag: "🇪🇸" },
  { code: "en", label: "EN", name: "English", flag: "🇬🇧" },
  { code: "fr", label: "FR", name: "Français", flag: "🇫🇷" },
  { code: "de", label: "DE", name: "Deutsch", flag: "🇩🇪" },
  { code: "it", label: "IT", name: "Italiano", flag: "🇮🇹" },
];

const currentLocale = ref(localStorage.getItem("toolbox.locale") || "es");

// Read the active locale code from outside a component (e.g. plain modules).
export function getCurrentLocale() {
  return currentLocale.value;
}

function readPath(source, path) {
  return path.split(".").reduce((acc, key) => acc?.[key], source);
}

export function useI18n() {
  const locale = computed(() => currentLocale.value);

  function setLocale(nextLocale) {
    if (!messages[nextLocale]) return;
    currentLocale.value = nextLocale;
    localStorage.setItem("toolbox.locale", nextLocale);
  }

  function t(path, params) {
    // FND-006: English is the canonical fallback (all locales sit at 100%
    // parity, so this only fires for typos/unknown keys → en, then the key).
    const template =
      readPath(messages[currentLocale.value], path) ?? readPath(messages.en, path) ?? path;
    if (typeof template !== "string" || !params) return template;
    return template.replace(/\{(\w+)\}/g, (match, key) =>
      Object.prototype.hasOwnProperty.call(params, key) ? String(params[key]) : match,
    );
  }

  return {
    availableLocales,
    locale,
    setLocale,
    t,
  };
}
