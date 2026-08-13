import { useEffect, useMemo, useRef, useState } from 'react';
import { tokens } from '../lib/api';
import { useApi, useSocket } from '../lib/hooks';
import { useRouter } from '../lib/router';
import { ErrorBanner, Loading, PositionTags } from '../components/common';

const POSITIONS = ['', 'C', '1B', '2B', '3B', 'SS', 'OF', 'UTIL', 'SP', 'RP', 'P'];

export default function Draft({ code }: { code: string }) {
  const { navigate } = useRouter();
  const myTeamId = tokens.teamId(code);
  const isCommissioner = !!tokens.commissioner(code);
  const [state, setState] = useState<any>(null);
  const [feed, setFeed] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [position, setPosition] = useState('');
  const [version, setVersion] = useState(0);

  // The server owns the clock; this only renders it, ticking down from the
  // value that arrived with the last state so we aren't broadcasting a frame
  // every second to every client.
  const [remaining, setRemaining] = useState<number | null>(null);
  const clockRef = useRef<number | null>(null);
  useEffect(() => {
    const id = window.setInterval(() => {
      setRemaining((r) => (r === null ? null : Math.max(0, r - 1)));
    }, 1000);
    return () => window.clearInterval(id);
  }, []);

  const { state: wsState, send } = useSocket(code, 'draft', (m) => {
    if (m.type === 'draft_state') {
      setState(m);
      if (m.on_clock?.overall !== clockRef.current) {
        clockRef.current = m.on_clock?.overall ?? null;
      }
      setRemaining(m.seconds_remaining ?? null);
    }
    else if (m.type === 'pick_made') {
      setFeed((f) => [
        `R${m.round}.${m.pick_in_round} — ${m.player_name} (${m.positions})${m.auto ? ' [auto]' : ''}`,
        ...f,
      ].slice(0, 12));
      setVersion((v) => v + 1);
    } else if (m.type === 'draft_error' && m.team_id === myTeamId) setError(m.message);
    else if (m.type === 'draft_complete') navigate(`/l/${code}/team`);
  });

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: '60' });
    if (search) params.set('search', search);
    if (position) params.set('position', position);
    return `/api/leagues/${code}/draft/available?${params.toString()}`;
  }, [code, search, position]);

  const { data: pool, reload: reloadPool } = useApi<any>(query, code, [version]);
  const { data: roster, reload: reloadRoster } = useApi<any>(
    myTeamId ? `/api/leagues/${code}/teams/${myTeamId}/lineup?week=1` : null,
    code,
    [version],
  );

  useEffect(() => {
    reloadPool();
    reloadRoster();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [version]);

  if (!state) return <Loading what="draft room" />;

  const onClock = state.on_clock;
  const myPick = onClock?.team_id === myTeamId;
  const complete = state.progress.complete;

  // Once the board is full there is nothing to draft, so stop showing a player
  // pool with dead buttons and point people at the season instead.
  if (complete) {
    return (
      <>
        <div className="clock">
          <strong>Draft complete</strong>
          <div className="small muted">
            All {state.progress.total} picks are in. Rosters are set and week 1 lineups
            have been filled in for you.
          </div>
          <div className="row" style={{ marginTop: '.75rem' }}>
            <button className="primary" onClick={() => navigate(`/l/${code}/team`)}>
              Set my lineup
            </button>
            <button onClick={() => navigate(`/l/${code}/standings`)}>Standings</button>
          </div>
        </div>
        <div className="card tight">
          <h3>Last picks</h3>
          <ul className="small" style={{ paddingLeft: '1rem', margin: 0 }}>
            {state.recent.map((p: any) => (
              <li key={p.overall}>
                R{p.round}.{p.pick_in_round} — {p.player_name} ({p.team_name})
              </li>
            ))}
          </ul>
        </div>
      </>
    );
  }

  return (
    <>
      <ErrorBanner message={error} />

      <div className={`clock ${myPick ? 'mine' : ''}`}>
        <div className="row between">
          <div>
            {onClock ? (
              <>
                <strong>{myPick ? 'You are on the clock' : `${onClock.team_name} is picking`}</strong>
                <div className="small muted">
                  Round {onClock.round}, pick {onClock.pick_in_round} (#{onClock.overall} overall)
                  {onClock.is_bot ? ' · bot' : ''}
                </div>
                {remaining !== null && !onClock.is_bot ? (
                  <div className={remaining <= 15 ? 'countdown-warn' : 'small muted'}>
                    {remaining > 0
                      ? `${Math.ceil(remaining)}s to pick`
                      : 'out of time — auto-picking'}
                  </div>
                ) : null}
              </>
            ) : (
              <strong>Draft complete</strong>
            )}
          </div>
          <div className="right small muted">
            {state.progress.made} / {state.progress.total} picks
            <br />
            <span className="tag">{wsState === 'open' ? 'live' : wsState}</span>
          </div>
        </div>
        {isCommissioner && onClock && !onClock.is_bot ? (
          <button className="small" style={{ marginTop: '.5rem' }} onClick={() => send({ type: 'force_pick' })}>
            Auto-pick for {onClock.team_name}
          </button>
        ) : null}
      </div>

      <div className="grid sidebar">
        <div className="card tight">
          <div className="row">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search players"
              style={{ flex: 1 }}
            />
            <select value={position} onChange={(e) => setPosition(e.target.value)}>
              {POSITIONS.map((p) => (
                <option key={p} value={p}>{p || 'All'}</option>
              ))}
            </select>
          </div>
          <div className="pick-list" style={{ marginTop: '.5rem' }}>
            {(pool?.players ?? []).map((p: any) => (
              <div className="player-row" key={p.player_id}>
                <span className="muted mono small" style={{ width: '2.5rem' }}>#{p.rank}</span>
                <span style={{ flex: 1 }}>
                  <span className="player-name">{p.name}</span>{' '}
                  <span className="small muted">{p.mlb_team}</span>
                  <br />
                  <PositionTags positions={p.positions} />
                </span>
                <span className="mono right small" style={{ width: '4rem' }}>{p.points.toFixed(0)}</span>
                <button
                  className="small primary"
                  disabled={!myPick}
                  onClick={() => {
                    setError(null);
                    send({ type: 'pick', player_id: p.player_id });
                  }}
                >
                  Draft
                </button>
              </div>
            ))}
            {!pool?.players?.length ? <p className="muted small">No players match.</p> : null}
          </div>
          <p className="small muted" style={{ marginTop: '.5rem' }}>
            Ranked by total fantasy points that season. Full-season numbers are shown here
            because the draft happens before the replay starts — every manager sees the same
            finished season. Once the season begins, stats are capped at the current replay date.
          </p>
        </div>

        <div>
          <div className="card tight">
            <h3>Recent picks</h3>
            {feed.length ? (
              <ul className="small" style={{ paddingLeft: '1rem', margin: 0 }}>
                {feed.map((line, i) => <li key={i}>{line}</li>)}
              </ul>
            ) : (
              <ul className="small" style={{ paddingLeft: '1rem', margin: 0 }}>
                {state.recent.map((p: any) => (
                  <li key={p.overall}>
                    R{p.round}.{p.pick_in_round} — {p.player_name} ({p.team_name})
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="card tight">
            <h3>Up next</h3>
            <ol className="small muted" style={{ paddingLeft: '1.2rem', margin: 0 }}>
              {state.upcoming.map((p: any) => (
                <li key={p.overall}>{p.team_name}</li>
              ))}
            </ol>
          </div>

          <div className="card tight">
            <h3>My roster ({roster?.players?.length ?? 0})</h3>
            <div className="small">
              {(roster?.players ?? []).map((p: any) => (
                <div key={p.player_id} className="row between" style={{ padding: '.15rem 0' }}>
                  <span>{p.name}</span>
                  <PositionTags positions={p.positions} />
                </div>
              ))}
              {!roster?.players?.length ? <p className="muted">No picks yet.</p> : null}
            </div>
          </div>
        </div>
      </div>
    </>
  );
}
