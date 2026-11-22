import { create } from "zustand";

/** What the user chose. `system` follows the operating system setting. */
export type ThemePreference = "light" | "dark" | "system";
/** What is actually shown. */
export type ResolvedTheme = "light" | "dark";

/** localStorage key; public/theme-init.js reads the same key before first paint. */
export const THEME_STORAGE_KEY = "nuramesh-theme";
const DARK_QUERY = "(prefers-color-scheme: dark)";

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

/** The theme to show for a preference, given whether the system prefers dark. */
export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (preference === "system") return systemDark ? "dark" : "light";
  return preference;
}

/** Reads the saved preference; storage may be unavailable, which means `system`. */
export function readPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return isThemePreference(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

function writePreference(preference: ThemePreference): void {
  try {
    window.localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // Storage blocked: the choice still applies for this session.
  }
}

function systemPrefersDark(): boolean {
  return window.matchMedia(DARK_QUERY).matches;
}

function apply(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference, systemPrefersDark());
  const root = document.documentElement;
  root.classList.toggle("dark", resolved === "dark");
  root.dataset.themePreference = preference;
  return resolved;
}

interface ThemeState {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
}

/** Theme preference store. Applies the theme to <html> and saves the choice. */
export const useTheme = create<ThemeState>()((set) => {
  const preference = readPreference();
  return {
    preference,
    resolved: resolveTheme(preference, systemPrefersDark()),
    setPreference: (next) => {
      writePreference(next);
      set({ preference: next, resolved: apply(next) });
    },
  };
});

/** Keeps the page in step with the OS setting while the preference is `system`. Returns cleanup. */
export function watchSystemTheme(): () => void {
  const media = window.matchMedia(DARK_QUERY);
  const onChange = (): void => {
    const { preference } = useTheme.getState();
    if (preference === "system") useTheme.setState({ resolved: apply("system") });
  };
  media.addEventListener("change", onChange);
  return () => {
    media.removeEventListener("change", onChange);
  };
}
