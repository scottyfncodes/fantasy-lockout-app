import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, socketUrl } from './api';

/** Fetch on mount (and whenever `deps` change), with a manual `reload`. */
export function useApi<T = any>(path: string | null, code?: string, deps: any[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!!path);

  const load = useCallback(async () => {
    if (!path) return;
    setLoading(true);
    try {
      setData(await api.get(path, code));
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, code, ...deps]);

  useEffect(() => {
    load();
  }, [load]);

  return { data, error, loading, reload: load, setData };
}

type WsState = 'connecting' | 'open' | 'closed';

/**
 * A WebSocket that reconnects with backoff.
 *
 * Used only for the mini-game and the draft room — the two places managers act
 * at the same moment.
 */
export function useSocket(
  code: string | null,
  room: 'lobby' | 'draft',
  onMessage: (msg: any) => void,
) {
  const [state, setState] = useState<WsState>('connecting');
  const socket = useRef<WebSocket | null>(null);
  const handler = useRef(onMessage);
  handler.current = onMessage;

  useEffect(() => {
    if (!code) return undefined;
    let closed = false;
    let attempt = 0;
    let timer: number | undefined;

    const connect = () => {
      if (closed) return;
      setState('connecting');
      const ws = new WebSocket(socketUrl(code, room));
      socket.current = ws;
      ws.onopen = () => {
        attempt = 0;
        setState('open');
      };
      ws.onmessage = (event) => {
        try {
          handler.current(JSON.parse(event.data));
        } catch {
          /* ignore malformed frames */
        }
      };
      ws.onclose = () => {
        setState('closed');
        if (closed) return;
        attempt += 1;
        timer = window.setTimeout(connect, Math.min(8000, 400 * 2 ** attempt));
      };
      ws.onerror = () => ws.close();
    };

    connect();
    return () => {
      closed = true;
      if (timer) window.clearTimeout(timer);
      socket.current?.close();
    };
  }, [code, room]);

  const send = useCallback((payload: any) => {
    if (socket.current?.readyState === WebSocket.OPEN) {
      socket.current.send(JSON.stringify(payload));
    }
  }, []);

  return { state, send };
}

export function useInterval(fn: () => void, ms: number | null) {
  const saved = useRef(fn);
  saved.current = fn;
  useEffect(() => {
    if (ms === null) return undefined;
    const id = window.setInterval(() => saved.current(), ms);
    return () => window.clearInterval(id);
  }, [ms]);
}
