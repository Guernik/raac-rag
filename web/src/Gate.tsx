// Beta gate screens. #26 decides when to show them; they share the app's header and notice.
import { type FormEvent, useState } from "react";
import { Header } from "./Header";
import { strings } from "./strings";

export function InviteScreen({ onSubmit, invalid }: { onSubmit: (code: string) => void; invalid: boolean }) {
  const [code, setCode] = useState("");
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (code.trim()) onSubmit(code.trim());
  };
  return (
    <div className="gate">
      <Header />
      <main className="gate-body">
        <h1>{strings.inviteTitle}</h1>
        <p>{strings.inviteBody}</p>
        <form className="invite" onSubmit={submit}>
          <label htmlFor="invite-code">{strings.inviteCode}</label>
          <div className="invite-row">
            <input
              id="invite-code"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              aria-invalid={invalid}
              aria-describedby={invalid ? "invite-error" : undefined}
            />
            <button type="submit" className="send" disabled={!code.trim()}>
              {strings.inviteSubmit}
            </button>
          </div>
          {invalid && (
            <p id="invite-error" className="field-error" role="alert">
              {strings.inviteInvalid}
            </p>
          )}
        </form>
      </main>
    </div>
  );
}

export function ClosedScreen() {
  return (
    <div className="gate">
      <Header />
      <main className="gate-body">
        <h1>{strings.closedTitle}</h1>
        <p>{strings.closedBody}</p>
      </main>
    </div>
  );
}
