import { useEffect, useRef, useState } from 'react';
import { api, tokens } from '../lib/api';
import { useApi, useInterval, useSocket } from '../lib/hooks';
import { useRouter } from '../lib/router';
import { ErrorBanner, Loading } from '../components/common';
import SpeedRound from '../components/SpeedRound';

export default function Lobby({ code }: { code: string }) {
  const { navigate } = useRouter();
  const { data, error, reload } = useApi<any>(`/api/leagues/${code}`, code);
  const [lobby, setLobby] = useState<any>(null);
  const [minigame, setMinigame] = useState<any>(null);
  const [tapResult, setTapResult] = useState<any>(null);
  const [redo, setRedo] = useState<any>(null);
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
    else if (m.type === 'lobby_error') setMsg(m.message);
    else if (m.type === 'lobby_countdown') setCountdown(m.remaining);
    else if (m.type === 'minigame_state') setMinigame(m);
    else if (m.type === 'tap_result') setTapResult(m);
    else if (m.type === 'redo_vote') setRedo(m);
    else if (m.type === 'redo_granted') { setOrder(null); setTapResult(null); setRedo(null); }
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
    // Whatever told us the phase — the socket, a refetch, or just loading this
    // page late — draft means the draft room. Relying on the one broadcast
    // meant a manager who reloaded at the wrong moment sat on a dead lobby.
    if (phase === 'draft') navigate(`/l/${code}/draft`);
  }, [phase, reload, navigate, code]);

  // The season is fetched by a background thread with no way to push, and the
  // page prefers whatever the socket last said — so a plain refetch would be
  // ignored. Ask the socket to re-send instead, which carries the new status.
  useInterval(
    () => {
      send({ type: 'refresh' });
      reload();
    },
    view?.season_year && view?.season && !view.season.ready ? 3000 : null,
  );

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
          {(view.season_caveats ?? []).map((caveat: string) => (
            <div className="banner" key={caveat}>{caveat}</div>
          ))}
          {view.season && !view.season.ready && revealed ? (
            <div className={view.season.state === 'failed' ? 'banner error' : 'banner info'}>
              {view.season.state === 'failed' || view.season.state === 'unusable' ? (
                <>
                  <strong>{view.season.year} could not be loaded</strong> —{' '}
                  {view.season.error ?? 'the ingest failed'}. The commissioner can
                  delete this league and start another to draw a different year.
                </>
              ) : (
                <>
                  <strong>Fetching the {view.season.year} season…</strong> Real box
                  scores for every game that year, about ninety seconds. Nobody else
                  will ever wait for {view.season.year} again — the next league to
                  draw it gets it instantly. The draft opens as soon as it lands.
                  <div className="progress" style={{ marginTop: '.5rem' }}>
                    <div className="progress-fill indeterminate" />
                  </div>
                </>
              )}
            </div>
          ) : null}
        </div>
      ) : null}

      {phase === 'year_reveal' && isCommissioner ? (
        <div className="card">
          <h3>Draft order</h3>
          <p className="small muted">
            Everyone watches the same pad. It counts down from three, holds for
            a moment nobody can predict, then turns green — first to tap after
            that picks first. Tap early and you go to the back. Bots line up
            behind every manager.
          </p>
          <button
            className="primary"
            disabled={!view.season?.ready}
            title={view.season?.ready ? undefined : 'the season is still loading'}
            onClick={() => send({ type: 'start_minigame' })}
          >
            {view.season?.ready ? 'Start the Green Light' : 'Waiting for the season…'}
          </button>
        </div>
      ) : null}

      {order ? (
        <div className="card tight">
          <h3>Not happy with that?</h3>
          <p className="small muted">
            A majority of managers can ask for a rerun, and the commissioner has
            to agree — neither alone is enough.
          </p>
          <div className="row">
            <button
              onClick={() => send({ type: 'vote_redo', value: true })}
              disabled={!joined}
            >
              Ask for a redo
            </button>
            {redo ? (
              <span className="small muted">
                {redo.votes} of {redo.managers} asking · {redo.needed} needed
              </span>
            ) : null}
            {isCommissioner ? (
              <button
                className="primary"
                disabled={!redo?.carried}
                title={redo?.carried ? undefined : 'a majority has to ask first'}
                onClick={() => send({ type: 'grant_redo' })}
              >
                Run it again
              </button>
            ) : null}
          </div>
        </div>
      ) : null}

      {minigame && !order ? (
        <SpeedRound
          state={minigame}
          result={tapResult}
          onTap={() => send({ type: 'tap' })}
        />
      ) : null}

      {order ? (
        <div className="card">
          <h3>Draft order set</h3>
          <table>
            <thead>
              <tr>
                <th>Pick</th>
                <th>Team</th>
                <th className="right">Reaction</th>
              </tr>
            </thead>
            <tbody>
              {order.map((o) => (
                <tr key={o.team_id}>
                  <td>{o.pick}</td>
                  <td>
                    {o.name} {o.is_bot ? <span className="tag bot">bot</span> : null}
                    {o.false_start ? <span className="tag loss">jumped</span> : null}
                    {o.no_show ? <span className="tag">no tap</span> : null}
                  </td>
                  <td className="right mono">
                    {o.reaction ? `${Math.round(o.reaction * 1000)} ms` : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {isCommissioner ? (
            <button
              className="primary"
              style={{ marginTop: '.75rem' }}
              onClick={() => send({ type: 'open_draft' })}
            >
              Looks right — open the draft
            </button>
          ) : (
            <p className="small muted" style={{ marginTop: '.5rem' }}>
              Waiting for the commissioner to open the draft room.
            </p>
          )}
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
