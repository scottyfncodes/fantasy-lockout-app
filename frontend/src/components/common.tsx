import type { ReactNode } from 'react';
import { Link } from '../lib/router';

export function Loading({ what = 'data' }: { what?: string }) {
  return <div className="card muted">Loading {what}…</div>;
}

export function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return <div className="banner error">{message}</div>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="muted small">{children}</p>;
}

export function PositionTags({ positions }: { positions: string }) {
  return (
    <>
      {positions.split(',').filter(Boolean).map((p) => (
        <span key={p} className="tag">{p}</span>
      ))}
    </>
  );
}

export function PlayerLink({
  code,
  playerId,
  name,
}: {
  code: string;
  playerId: string;
  name: string;
}) {
  return (
    <Link to={`/l/${code}/players/${playerId}`} className="player-name">
      {name}
    </Link>
  );
}

/** IL badge with the historical stint that caused it. */
export function IlBadge({ il }: { il: any }) {
  if (!il) return null;
  return (
    <span className="tag il" title={`${il.kind}: ${il.note ?? ''} (from ${il.start_date})`}>
      {il.kind}
    </span>
  );
}
