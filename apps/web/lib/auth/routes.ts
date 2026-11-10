export const LOGIN_PATH = "/login";
export const REGISTER_PATH = "/register";
export const DASHBOARD_PATH = "/dashboard";

const AUTH_PAGES = [LOGIN_PATH, REGISTER_PATH];

export function isProtectedPath(pathname: string): boolean {
  return pathname === DASHBOARD_PATH || pathname.startsWith(`${DASHBOARD_PATH}/`);
}

/**
 * Where to go after signing in. Only paths inside the dashboard are accepted, so a crafted
 * `?next=https://evil.example` or `?next=//evil.example` link cannot send a user off-site.
 */
export function safeNextPath(next: string | null | undefined): string {
  if (!next || !isProtectedPath(next.split(/[?#]/)[0] ?? "")) return DASHBOARD_PATH;
  if (next.includes("\\") || next.includes("//")) return DASHBOARD_PATH;
  return next;
}

/**
 * The redirect a request needs, or null to let it through:
 * signed-out users are kept out of the dashboard, signed-in users skip the sign-in pages.
 */
export function resolveAuthRedirect(pathname: string, isAuthenticated: boolean): string | null {
  if (isProtectedPath(pathname) && !isAuthenticated) {
    return `${LOGIN_PATH}?next=${encodeURIComponent(pathname)}`;
  }
  if (AUTH_PAGES.includes(pathname) && isAuthenticated) {
    return DASHBOARD_PATH;
  }
  return null;
}
