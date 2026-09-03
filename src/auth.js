import { API_BASE_URL } from "./api";

const TOKEN_KEY = "dashboard_token";

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// Fired whenever a request comes back unauthenticated (missing/invalid/expired token),
// so the app can react (e.g. show the login screen again) without prop-drilling a
// callback through every component that fetches data.
export function onSessionExpired(callback) {
  window.addEventListener("auth:session-expired", callback);
  return () => window.removeEventListener("auth:session-expired", callback);
}

/**
 * fetch() wrapper that attaches the login token to every request, picks up the
 * renewed token FastAPI sends back on each authenticated call (sliding expiration -
 * as long as you keep using the dashboard, the session keeps extending itself), and
 * clears the session + notifies the app if the backend ever rejects the token.
 */
export async function authFetch(path, options = {}) {
  const token = getToken();
  const headers = { ...(options.headers || {}) };
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });

  const newToken = res.headers.get("X-New-Token");
  if (newToken) setToken(newToken);

  if (res.status === 401) {
    clearToken();
    window.dispatchEvent(new Event("auth:session-expired"));
  }

  return res;
}
