import { useMemo, useState } from 'react';
import { api, tokens } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, IlBadge, Loading, PlayerLink, PositionTags } from '../components/common';

const POSITIONS = ['', 'C', '1B', '2B', '3B', 'SS', 'OF', 'UTIL', 'SP', 'RP', 'P'];

export default function Waivers({ code }: { code: string }) {
  const myTeamId = tokens.teamId(code);
  const [search, setSearch] = useState('');
  const [position, setPosition] = useState('');
  const [version, setVersion] = useState(0);
  const [target, setTarget] = useState<any>(null);
  const [amount, setAmount] = useState(1);
  const [dropId, setDropId] = useState('');
  const [msg, setMsg] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  const query = useMemo(() => {
    const params = new URLSearchParams({ limit: '60' });
    if (search) params.set('search', search);
    if (position) params.set('position', position);
    return `/api/leagues/${code}/free-agents?${params.toString()}`;
  }, [code, search, position]);

  const { data: pool, error } = useApi<any>(query, code, [version]);
  const { data: bids, reload: reloadBids } = useApi<any>(
    myTeamId ? `/api/leagues/${code}/waivers/bids` : null, code, [version],
  );
  const { data: roster } = useApi<any>(
    myTeamId ? `/api/leagues/${code}/teams/${myTeamId}/lineup` : null, code, [version],
  );
  const { data: results } = useApi<any>(`/api/leagues/${code}/waivers/results`, code, [version]);

  if (error) return <ErrorBanner message={error} />;
  if (!pool) return <Loading what="free agents" />;

  async function submit() {
    setMsg(null);
    setOk(null);
    try {
      const res = await api.post(`/api/leagues/${code}/waivers/bids`, code, {
        add_player_id: target.player_id,
        amount,
        drop_player_id: dropId || null,
      });
      setOk(`Bid placed on ${target.name}. It processes with week ${res.processes_week}.`);
      setTarget(null);
      setVersion((v) => v + 1);
      reloadBids();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  async function cancel(id: number) {
    try {
      await api.del(`/api/leagues/${code}/waivers/bids/${id}`, code);
      setVersion((v) => v + 1);
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  return (
    <>
      <h1>Waivers</h1>
      <div className="banner info">
        Blind FAAB. Bids are invisible to everyone until they process at the week rollover —
        highest bid wins, ties go to the worse record. Stats below are through{' '}
        <strong>{pool.as_of}</strong> only; the app never shows you full-season or future
        numbers for a free agent.
      </div>
      {pool.adds_frozen ? (
        <div className="banner">
          Adds are closed — {pool.frozen_reason ?? 'free-agent moves are frozen'}.
        </div>
      ) : null}

      <ErrorBanner message={msg} />
      {ok ? <div className="banner info">{ok}</div> : null}

      <div className="grid sidebar">
        <div className="card tight">
          <div className="row">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search free agents"
              style={{ flex: 1 }}
            />
            <select value={position} onChange={(e) => setPosition(e.target.value)}>
              {POSITIONS.map((p) => <option key={p} value={p}>{p || 'All'}</option>)}
            </select>
          </div>

          <div className="pick-list" style={{ marginTop: '.5rem' }}>
            {pool.players.map((p: any) => (
              <div className="player-row" key={p.player_id}>
                <span style={{ flex: 1 }}>
                  <PlayerLink code={code} playerId={p.player_id} name={p.name} />{' '}
                  <span className="small muted">{p.mlb_team}</span>
                  <br />
                  <PositionTags positions={p.positions} /> <IlBadge il={p.il} />
                  {p.on_waivers_until ? (
                    <span className="tag" title="dropped recently; still clearing waivers">
                      clears {p.on_waivers_until}
                    </span>
                  ) : null}
                </span>
                <span className="right mono small" style={{ width: '5rem' }}>
                  {p.points.toFixed(0)} pts
                  <br />
                  <span className="muted">{p.games} g · {p.points_per_game}/g</span>
                </span>
                <button
                  className="small"
                  disabled={pool.adds_frozen}
                  title={pool.adds_frozen ? pool.frozen_reason ?? 'adds are closed' : undefined}
                  onClick={() => { setTarget(p); setAmount(1); }}
                >
                  Bid
                </button>
              </div>
            ))}
          </div>
        </div>

        <div>
          {target ? (
            <div className="card tight">
              <h3>Bid on {target.name}</h3>
              <label className="field">
                FAAB amount (you have {bids?.faab_remaining ?? '—'})
                <input
                  type="number"
                  min={0}
                  max={bids?.faab_remaining ?? 100}
                  value={amount}
                  onChange={(e) => setAmount(Number(e.target.value))}
                />
              </label>
              <label className="field" style={{ marginTop: '.5rem' }}>
                Drop (required if your roster is full)
                <select value={dropId} onChange={(e) => setDropId(e.target.value)}>
                  <option value="">— nobody —</option>
                  {(roster?.players ?? []).map((p: any) => (
                    <option key={p.player_id} value={p.player_id}>
                      {p.name} ({p.positions})
                    </option>
                  ))}
                </select>
              </label>
              <div className="row" style={{ marginTop: '.75rem' }}>
                <button className="primary" onClick={submit}>Place blind bid</button>
                <button onClick={() => setTarget(null)}>Cancel</button>
              </div>
              <p className="small muted">
                A dropped player goes on waivers before becoming a free agent, so nobody can
                drop and instantly re-add to dodge a rival's bid.
              </p>
            </div>
          ) : null}

          <div className="card tight">
            <h3>My pending bids</h3>
            {(bids?.bids ?? []).filter((b: any) => b.status === 'pending').length ? (
              (bids?.bids ?? [])
                .filter((b: any) => b.status === 'pending')
                .map((b: any) => (
                  <div key={b.id} className="row between small" style={{ padding: '.2rem 0' }}>
                    <span>{b.add_name} — {b.amount}</span>
                    <button className="small danger" onClick={() => cancel(b.id)}>cancel</button>
                  </div>
                ))
            ) : (
              <p className="muted small">No pending bids.</p>
            )}
          </div>

          <div className="card tight">
            <h3>Last processed week</h3>
            {(results?.results ?? []).length ? (
              <table>
                <tbody>
                  {results.results.map((r: any) => (
                    <tr key={r.id}>
                      <td className="small">{r.team_name}</td>
                      <td className="small">{r.add_name}</td>
                      <td className="right mono small">{r.amount}</td>
                      <td className="right">
                        <span className={`tag ${r.status === 'won' ? 'win' : 'loss'}`}>{r.status}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted small">Nothing processed yet.</p>
            )}
          </div>
        </div>
      </div>

      <p className="footer-note">
        Honour-system note: blind bidding removes the app's information advantage, but it
        cannot remove your memory of the season. Rosters freeze once the playoffs begin,
        so the bracket is decided by the team you built.
      </p>
    </>
  );
}
