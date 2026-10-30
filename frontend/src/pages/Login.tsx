import { useEffect, useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, BASE } from "../api";
import { useAuth } from "../auth";
import { ErrorBox } from "../components";

const ERRORS: Record<string, string> = {
  expired: "The sign-in attempt expired. Please try again.",
  state: "The sign-in response did not match this browser session.",
  token: "The identity provider did not issue tokens.",
  id_token: "The identity token could not be verified.",
  email: "Your identity provider did not supply a verified email address.",
  domain: "Your email domain is not allowed to sign in.",
  disabled: "Your MOKS account is disabled.",
  account_conflict: "An account with this email uses a different sign-in method.",
};

export default function Login() {
  const { me, refresh } = useAuth();
  const [params] = useSearchParams();
  const nav = useNavigate();
  const [mode, setMode] = useState<string>();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<unknown>(params.get("error") ? new Error(ERRORS[params.get("error")!] ?? "Sign-in failed.") : null);
  const [busy, setBusy] = useState(false);
  const next = params.get("next") ?? `${BASE}/`;
  const internalNext = next.startsWith(`${BASE}/`) ? next.slice(BASE.length) : "/";

  useEffect(() => {
    api<{ mode: string }>("/auth/config").then((c) => setMode(c.mode), setError);
  }, []);
  useEffect(() => {
    if (me) nav(internalNext, { replace: true });
  }, [me, nav, internalNext]);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api(mode === "dev" ? "/auth/dev-login" : "/auth/login", { method: "POST", body: mode === "dev" ? { email } : { email, password } });
      await refresh();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <div className="card stack">
        <div>
          <h1>MOKS</h1>
          <p className="muted" style={{ margin: 0 }}>AI-assisted production operations</p>
        </div>
        <ErrorBox error={error} />
        {mode === "oidc" && (
          <button onClick={() => window.location.assign(`${BASE}/api/auth/oidc/login?next=${encodeURIComponent(next)}`)}>
            Sign in with single sign-on
          </button>
        )}
        {(mode === "local" || mode === "dev") && (
          <form className="stack" onSubmit={submit}>
            <label>Email<input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} /></label>
            {mode === "local" && (
              <label>Password<input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} /></label>
            )}
            <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
            {mode === "dev" && <div className="alert warn small">Development sign-in: no password. Never enabled in production.</div>}
          </form>
        )}
        <p className="small muted" style={{ margin: 0 }}>New accounts have no access until an administrator grants a role.</p>
      </div>
    </div>
  );
}
