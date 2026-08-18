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


/**
 * Season-cache progress.
 *
 * A fresh deployment downloads and parses every configured season, which for a
 * wide range runs to tens of minutes. Without this the app looks broken —
 * leagues cannot be created and nothing says why.
 */
export function WarmupBar({ warmup }: { warmup: any }) {
  if (!warmup || warmup.complete) return null;
  const { percent, done, requested, remaining, stalled } = warmup;
  return (
    <div className={stalled ? 'banner error' : 'banner info'}>
      <strong>
        {stalled ? 'Season data stopped partway' : 'Getting the seasons ready'}
      </strong>{' '}
      — {done.length} of {requested.length} cached ({percent}%).
      <div className="progress" style={{ marginTop: '.5rem' }}>
        <div className="progress-fill" style={{ width: `${Math.max(2, percent)}%` }} />
      </div>
      <p className="small muted" style={{ marginTop: '.4rem' }}>
        {stalled ? (
          <>
            {remaining.length} season{remaining.length === 1 ? '' : 's'} did not finish
            ({remaining.slice(0, 6).join(', ')}
            {remaining.length > 6 ? '…' : ''}). A redeploy retries the ones that are
            missing.
          </>
        ) : (
          <>
            Each season is a full year of real box scores, about a minute and a half
            apiece. You can leave this page — leagues can be created as soon as one
            season is in.
          </>
        )}
      </p>
    </div>
  );
}
