import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { api, ApiError, BASE, setCsrf } from "./api";

export interface Me {
  id: string;
  email: string;
  display_name: string;
  auth_mode: "oidc" | "local" | "dev";
  csrf_token: string;
  roles: { role: string; environment: string; namespace: string }[];
  permissions: string[];
}

interface AuthState {
  me: Me | null;
  loading: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
  can: (perm: string) => boolean;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const m = await api<Me>("/auth/me");
      setCsrf(m.csrf_token);
      setMe(m);
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 401)) console.error(e);
      setCsrf(null);
      setMe(null);
    } finally {
      setLoading(false);
    }
  }, []);

  const logout = useCallback(async () => {
    const r = await api<{ redirect: string }>("/auth/logout", { method: "POST" }).catch(() => ({ redirect: `${BASE}/login` }));
    setCsrf(null);
    setMe(null);
    window.location.assign(r.redirect);
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Display-only check. The backend enforces every permission and scope on its own.
  const can = useCallback((perm: string) => !!me?.permissions.includes(perm), [me]);

  return <Ctx.Provider value={{ me, loading, refresh, logout, can }}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}
