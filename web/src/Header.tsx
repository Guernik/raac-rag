import { strings } from "./strings";
import { useTheme } from "./theme";

export function Header() {
  const [theme, toggleTheme] = useTheme();
  return (
    <header className="top">
      <p className="brand">
        {strings.appTitle} <small>{strings.beta}</small>
      </p>
      <p className="notice" role="note">
        {strings.notice}
      </p>
      <button
        type="button"
        className="icon-button theme-toggle"
        onClick={toggleTheme}
        aria-label={theme === "dark" ? strings.themeToLight : strings.themeToDark}
        title={theme === "dark" ? strings.themeToLight : strings.themeToDark}
      >
        {theme === "dark" ? (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z" />
          </svg>
        )}
      </button>
    </header>
  );
}
