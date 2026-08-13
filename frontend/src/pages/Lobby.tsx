import { useEffect, useRef, useState } from 'react';
import { api, tokens } from '../lib/api';
import { useApi, useSocket } from '../lib/hooks';
import { useRouter } from '../lib/router';
import { ErrorBanner, Loading } from '../components/common';
import SpeedRound from '../components/SpeedRound';

export default function Lobby({ code }: { code: string }) {
  const { navigate } = useRouter();
  const { data, error, reload } = useApi<any>(`/api/leagues/${code}`, code);
  const [lobby, setLobby] = useState<any>(null);
  const [minigame, setMinigame] = useState<any>(null);
  const [order, setOrder] = useState<any[] | null>(null);
  const [countdown, setCountdown] = useState<number | null>(null);
  const [teamName, setTeamName] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const joined = !!tokens.manager(code);
  const isCommissioner = !!tokens.commissioner(code);
  const phaseRef = useRef<string | null>(null);

  const { state: wsState, send } = useSocket(code, 'lobby', (m) => {
    if (m.type === 'lobby_state') setLobby(m);
    else if (m.type === 'lobby_countdown') setCountdown(m.remaining);
    else if (m.type === 'minigame_state') setMinigame(m);
    else if (m.type === 'draft_order') setOrder(m.order);
    else if (m.type === 'phase' && m.phase === 'draft') navigate(`/l/${code}/draft`);
  });

  const view = lobby ?? data;
  const phase = view?.phase;

  useEffect(() => {
    if (phase && phase !== phaseRef.current) {
      phaseRef.current = phase;
      reload();
    }
  }, [phase, reload]);

  useEffect(() => {
    if (view?.season_year) {
      const timer = window.setTimeout(() => setRevealed(true), 1200);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [view?.season_year]);

  if (error) return <ErrorBanner message={error} />;
  if (!view) return <Loading what="league" />;

  async function join() {
    setMsg(null);
    try {
      const res = await api.post(`/api/leagues/${code}/join`, code, { team_name: teamName });
      tokens.setManager(code, res.manager_token, res.team_id);
      send({ type: 'refresh' });
      reload();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  async function start() {
    setMsg(null);
    try {
      await api.post(`/api/leagues/${code}/start`, code);
      send({ type: 'refresh' });
      reload();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  const shareUrl = `${window.location.origin}/join/${code}`;
  const myTeamId = tokens.teamId(code);

  return (
    <>
      <ErrorBanner message={msg} />

      {phase === 'lobby' ? (
        <div className="card">
          <div className="row between">
            <h1>Lobby</h1>
            <span className="tag">{wsState === 'open' ? 'live' : wsState}</span>
          </div>
          <p className="small muted">
            Up to {view.max_teams} managers can join. When everyone present locks in, empty
            seats are filled with bots and the league starts at{' '}
            <strong>{view.final_size_if_started} teams</strong>.
          </p>

          <div className="row">
            <input readOnly value={shareUrl} style={{ flex: '1 1 16rem' }} />
            <button onClick={() => navigator.clipboard?.writeText(shareUrl)}>Copy link</button>
          </div>

          {!joined ? (
            <div className="row" style={{ marginTop: '1rem' }}>
              <input
                value={teamName}
                onChange={(e) => setTeamName(e.target.value)}
                placeholder="Your team name"
                maxLength={40}
                style={{ flex: '1 1 12rem' }}
              />
              <button className="primary" onClick={join} disabled={!teamName.trim()}>
                Join
              </button>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="card">
        <h3>Managers ({view.teams.length})</h3>
        <table>
          <tbody>
            {view.teams.map((t: any) => (
              <tr key={t.id}>
                <td>
                  {t.name} {t.id === myTeamId ? <span className="tag">you</span> : null}{' '}
                  {t.is_bot ? <span className="tag bot">bot</span> : null}
                </td>
                <td className="right">
                  {t.draft_slot ? (
                    <span className="tag slot">pick {t.draft_slot}</span>
                  ) : t.locked_in ? (
                    <span className="tag win">locked in</span>
                  ) : (
                    <span className="tag">waiting</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        {joined && phase === 'lobby' ? (
          <button
            style={{ marginTop: '.75rem' }}
            onClick={() => send({ type: 'lock_in', value: true })}
          >
            I'm ready — lock me in
          </button>
        ) : null}

        {isCommissioner && phase === 'lobby' ? (
          <div className="row" style={{ marginTop: '.75rem' }}>
            <button className="primary" onClick={start}>
              Close lobby, fill bots &amp; draw the year
            </button>
            {countdown === null || countdown === 0 ? (
              <button
                onClick={() =>
                  send({ type: 'start_countdown', seconds: view.config?.lobby_timeout_seconds })
                }
              >
                Start countdown instead
              </button>
            ) : (
              <button className="danger" onClick={() => send({ type: 'cancel_countdown' })}>
                Cancel countdown
              </button>
            )}
          </div>
        ) : null}

        {countdown ? (
          <div className="banner">
            Lobby closes in {countdown}s — empty seats become bots and the season year is drawn.
          </div>
        ) : null}
      </div>

      {view.season_year ? (
        <div className="card center">
          <h3>The season has been drawn</h3>
          <div style={{ fontSize: '3.5rem', fontWeight: 800, letterSpacing: '-.03em' }}>
            {revealed ? view.season_year : '????'}
          </div>
          <p className="small muted">
            Randomly drawn from the eligible pool — no one chose it.
            {view.pool_check ? ` ${view.pool_check.message}` : ''}
          </p>
          {view.pool_check && !view.pool_check.ok ? (
            <div className="banner">
              The player pool is thin for this league size: only{' '}
              {view.pool_check.free_agents_after_draft} free agents would remain after the draft.
            </div>
          ) : null}
        </div>
      ) : null}

      {phase === 'year_reveal' && isCommissioner ? (
        <div className="card">
          <h3>Draft order</h3>
          <p className="small muted">
            Every manager gets the same {view.config?.speed_round_seconds ?? 10}-second window to
            tap the ball. Most taps picks first; bots play too.
          </p>
          <button className="primary" onClick={() => send({ type: 'start_minigame' })}>
            Start the Speed Round
          </button>
        </div>
      ) : null}

      {minigame && !order ? (
        <SpeedRound state={minigame} onTap={() => send({ type: 'tap' })} myTeamId={myTeamId} />
      ) : null}

      {order ? (
        <div className="card">
          <h3>Draft order set</h3>
          <table>
            <thead>
              <tr>
                <th>Pick</th>
                <th>Team</th>
                <th className="right">Taps</th>
              </tr>
            </thead>
            <tbody>
              {order.map((o) => (
                <tr key={o.team_id}>
                  <td>{o.pick}</td>
                  <td>
                    {o.name} {o.is_bot ? <span className="tag bot">bot</span> : null}
                  </td>
                  <td className="right mono">{o.score}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button className="primary" style={{ marginTop: '.75rem' }} onClick={() => navigate(`/l/${code}/draft`)}>
            Enter the draft room
          </button>
        </div>
      ) : null}

      {phase && !['lobby', 'year_reveal', 'minigame'].includes(phase) ? (
        <div className="card">
          <p>
            This league is in the <strong>{phase}</strong> phase.
          </p>
          <div className="row">
            <button onClick={() => navigate(`/l/${code}/team`)}>My team</button>
            <button onClick={() => navigate(`/l/${code}/standings`)}>Standings</button>
            {phase === 'draft' ? (
              <button className="primary" onClick={() => navigate(`/l/${code}/draft`)}>
                Draft room
              </button>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  );
}
