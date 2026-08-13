/**
 * API client.
 *
 * Identity is two opaque tokens kept in local storage: a manager token per
 * league, and (for whoever created it) a commissioner token. No accounts, no
 * passwords — enough for a friend league.
 */

export type Json = Record<string, any>;

const MANAGER_KEY = (code: string) => `rsr:manager:${code}`;
const COMMISH_KEY = (code: string) => `rsr:commish:${code}`;
const TEAM_KEY = (code: string) => `rsr:team:${code}`;

export const tokens = {
  manager: (code: string) => localStorage.getItem(MANAGER_KEY(code)) ?? '',
  commissioner: (code: string) => localStorage.getItem(COMMISH_KEY(code)) ?? '',
  teamId: (code: string) => localStorage.getItem(TEAM_KEY(code)) ?? '',
  setManager: (code: string, token: string, teamId: string) => {
    localStorage.setItem(MANAGER_KEY(code), token);
    localStorage.setItem(TEAM_KEY(code), teamId);
  },
  setCommissioner: (code: string, token: string) => localStorage.setItem(COMMISH_KEY(code), token),
  forget: (code: string) => {
    localStorage.removeItem(MANAGER_KEY(code));
    localStorage.removeItem(COMMISH_KEY(code));
    localStorage.removeItem(TEAM_KEY(code));
  },
  knownLeagues: (): string[] => {
    const out: string[] = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key?.startsWith('rsr:manager:')) out.push(key.slice('rsr:manager:'.length));
    }
    return out;
  },
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request(method: string, path: string, code?: string, body?: Json): Promise<any> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (code) {
    const manager = tokens.manager(code);
    const commish = tokens.commissioner(code);
    if (manager) headers['X-Manager-Token'] = manager;
    if (commish) headers['X-Commissioner-Token'] = commish;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    throw new ApiError(res.status, data?.detail ?? `${method} ${path} failed (${res.status})`);
  }
  return data;
}

export const api = {
  get: (path: string, code?: string) => request('GET', path, code),
  post: (path: string, code?: string, body?: Json) => request('POST', path, code, body ?? {}),
  put: (path: string, code?: string, body?: Json) => request('PUT', path, code, body ?? {}),
  patch: (path: string, code?: string, body?: Json) => request('PATCH', path, code, body ?? {}),
  del: (path: string, code?: string) => request('DELETE', path, code),
};

export function socketUrl(code: string, room: 'lobby' | 'draft'): string {
  const proto = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const params = new URLSearchParams();
  const manager = tokens.manager(code);
  const commish = tokens.commissioner(code);
  if (manager) params.set('token', manager);
  if (commish) params.set('commish', commish);
  return `${proto}://${window.location.host}/ws/${code}/${room}?${params.toString()}`;
}

export const fmt = {
  points: (n: number | null | undefined) => (n ?? 0).toFixed(1),
  record: (t: Json) => `${t.wins}-${t.losses}${t.ties ? `-${t.ties}` : ''}`,
  date: (iso: string) =>
    new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { month: 'short', day: 'numeric' }),
};
