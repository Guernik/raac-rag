import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ClosedScreen, InviteScreen } from "./Gate";
import "./tokens.css";
import "./styles.css";

// Until #26 wires the beta gate, `pnpm dev` previews its screens at #invitacion and #manana.
const preview = import.meta.env.DEV ? location.hash : "";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {preview === "#invitacion" ? (
      <InviteScreen onSubmit={() => {}} invalid={false} />
    ) : preview === "#manana" ? (
      <ClosedScreen />
    ) : (
      <App />
    )}
  </StrictMode>,
);
