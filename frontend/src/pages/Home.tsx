import { useState } from 'react';
import { api, tokens } from '../lib/api';
import { useApi, useInterval } from '../lib/hooks';
import { useRouter } from '../lib/router';
import { ErrorBanner, WarmupBar } from '../components/common';

export default function Home() {
  const { navigate } = useRouter();
  const { data: defaults } = useApi<any>('/api/meta/defaults');
  const { data: seasons, reload: reloadSeasons } = useApi<any>('/api/meta/seasons');
  const { data: warmup, reload: reloadWarmup } = useApi<any>('/api/meta/warmup');
  // Caching a wide range of seasons runs for tens of minutes, so the page keeps
  // itself current rather than asking anyone to sit there reloading.
  useInterval(() => {
    reloadWarmup();
    reloadSeasons();
  }, warmup && !warmup.complete ? 5000 : null);
  const [name, setName] = useState('Lockout League');
  const [teamCount, setTeamCount] = useState(12);
  const [joinCode, setJoinCode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const eligible = (seasons?.seasons ?? []).filter((s: any) => s.eligible);
  const room = defaults?.capacity;
  const discrepancy = defaults?.config?.roster_discrepancy;

  async function create() {
    setBusy(true);
    setError(null);
    try {
      const league = await api.post('/api/leagues', undefined, {
        name,
        config: { team_count: teamCount },
      });
      tokens.setCommissioner(league.code, league.commissioner_token);
      navigate(`/join/${league.code}`);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div style={{ paddingTop: '2rem' }}>
      <WarmupBar warmup={warmup} />
      {room?.full ? (
        <div className="banner error">
          <strong>This server is full</strong> — {room.used} of {room.max} leagues.
          A commissioner can delete a finished league from its commissioner page
          to free a slot.
        </div>
      ) : null}
      <h1>Retro Season Replay</h1>
      <p className="muted">
        Draft a real MLB season that already happened and replay it day by day. Points come
        from the actual box scores; the injured list comes from the actual transaction log.
      </p>

      <ErrorBanner message={error} />

      {eligible.length === 0 ? (
        <div className="banner">
          No seasons are cached yet. Run{' '}
          <code>python -m app.pipeline.build --years 2000-2019</code> in the backend before
          starting a league.
        </div>
      ) : null}

      {discrepancy ? <div className="banner">{discrepancy.message}</div> : null}

      <div className="grid two">
        <div className="card">
          <h2>Start a league</h2>
          <p className="small muted">
            You become the commissioner. Share the join link, and once everyone locks in the
            season year is drawn at random — nobody picks it.
          </p>
          <div className="row" style={{ marginTop: '.75rem' }}>
            <label className="field" style={{ flex: '2 1 12rem' }}>
              League name
              <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} />
            </label>
            <label className="field" style={{ flex: '1 1 6rem' }}>
              Teams
              <select value={teamCount} onChange={(e) => setTeamCount(Number(e.target.value))}>
                {[8, 10, 12, 14].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>
          </div>
          {room && !room.full && room.remaining <= 5 ? (
            <p className="small muted">
              {room.remaining} league{room.remaining === 1 ? '' : 's'} left on this
              server ({room.used} of {room.max} used).
            </p>
          ) : null}
          <button
            className="primary"
            onClick={create}
            disabled={busy || !!room?.full}
            title={room?.full ? 'this server is full' : undefined}
            style={{ marginTop: '.75rem' }}
          >
            {busy ? 'Creating…' : 'Create league'}
          </button>
        </div>

        <div className="card">
          <h2>Join a league</h2>
          <p className="small muted">Enter the six-character code from your commissioner.</p>
          <div className="row" style={{ marginTop: '.75rem' }}>
            <input
              value={joinCode}
              onChange={(e) => setJoinCode(e.target.value.toUpperCase())}
              placeholder="ABC123"
              maxLength={6}
              style={{ flex: 1, letterSpacing: '.2em', textTransform: 'uppercase' }}
            />
            <button onClick={() => joinCode && navigate(`/join/${joinCode}`)}>Go</button>
          </div>

          {tokens.knownLeagues().length ? (
            <>
              <h3 style={{ marginTop: '1rem' }}>Your leagues</h3>
              <div className="row">
                {tokens.knownLeagues().map((code) => (
                  <button key={code} className="small" onClick={() => navigate(`/l/${code}`)}>
                    {code}
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </div>

      <div className="card">
        <h3>Seasons available for the draw</h3>
        {eligible.length ? (
          <p className="small">
            {eligible.map((s: any) => s.year).join(', ')} — cached from{' '}
            {[...new Set(eligible.map((s: any) => s.source))].join(', ')}. Seasons whose data
            has gaps are excluded from the draw rather than replayed with holes in them.
          </p>
        ) : (
          <p className="small muted">None cached.</p>
        )}
        {(seasons?.seasons ?? []).some((s: any) => !s.eligible) ? (
          <p className="small muted">
            Excluded:{' '}
            {(seasons?.seasons ?? [])
              .filter((s: any) => !s.eligible)
              .map((s: any) => `${s.year} (${s.ineligible_reason})`)
              .join('; ')}
          </p>
        ) : null}
      </div>
    </div>
  );
}
