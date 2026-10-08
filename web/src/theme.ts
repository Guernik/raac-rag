import { useState } from "react";

export type Theme = "light" | "dark";

// index.html applies the stored theme before first paint, under the same key.
const KEY = "raac-theme";

function current(): Theme {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** The page theme: light unless the viewer picked dark, remembered per browser. */
export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(current);
  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    const paper = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim();
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", paper);
    try {
      localStorage.setItem(KEY, next);
    } catch {
      // Storage blocked: the choice lasts until reload.
    }
    setTheme(next);
  };
  return [theme, toggle];
}
