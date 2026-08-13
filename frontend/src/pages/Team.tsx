import { useEffect, useMemo, useState } from 'react';
import { api, fmt, tokens } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, IlBadge, Loading, PlayerLink, PositionTags } from '../components/common';

export default function Team({ code, teamId }: { code: string; teamId?: string }) {
  const myTeamId = teamId ?? tokens.teamId(code);
  const { data: league } = useApi<any>(`/api/leagues/${code}`, code);
  const currentWeek = league?.timeline?.current_week ?? 1;
  const [week, setWeek] = useState<number | null>(null);
  const activeWeek = week ?? currentWeek;

  const { data, error, reload, setData } = useApi<any>(
    myTeamId ? `/api/leagues/${code}/teams/${myTeamId}/lineup?week=${activeWeek}` : null,
    code,
    [activeWeek],
  );
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);

  useEffect(() => {
    if (data?.players) {
      setDraft(Object.fromEntries(data.players.map((p: any) => [p.player_id, p.slot])));
      setMsg(null);
      setOk(null);
    }
  }, [data]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    Object.values(draft).forEach((slot) => { c[slot] = (c[slot] ?? 0) + 1; });
    return c;
  }, [draft]);

  if (!myTeamId) {
    return <div className="banner">You have not joined this league on this device.</div>;
  }
  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="roster" />;

  const slotOptions = (p: any): string[] => {
    const opts = [...p.eligible_slots];
    if (p.il) opts.length = 0; // an injured player can only sit
    opts.push('BENCH');
    if (p.il) opts.push('IL');
    return opts;
  };

  const ilUsed = counts.IL ?? 0;
  const benchCapacity = data.bench_size + (data.il_size - ilUsed);
  const activeCount = Object.entries(counts)
    .filter(([slot]) => slot !== 'BENCH' && slot !== 'IL')
    .reduce((n, [, v]) => n + v, 0);
  const activeCapacity = (Object.values(data.active_slots) as number[])
    .reduce((a, b) => a + b, 0);

  async function save() {
    setMsg(null);
    setOk(null);
    try {
      const res = await api.put(`/api/leagues/${code}/teams/${myTeamId}/lineup`, code, {
        week: activeWeek,
        assignment: draft,
      });
      setData(res.lineup);
      setOk(`Week ${activeWeek} lineup saved.`);
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  async function autofill() {
    setMsg(null);
    try {
      await api.post(`/api/leagues/${code}/teams/${myTeamId}/lineup/autofill?week=${activeWeek}`, code);
      reload();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  const sorted = [...data.players].sort((a: any, b: any) => {
    const rank = (s: string) => (s === 'BENCH' ? 1 : s === 'IL' ? 2 : 0);
    return rank(draft[a.player_id] ?? a.slot) - rank(draft[b.player_id] ?? b.slot)
      || a.name.localeCompare(b.name);
  });

  return (
    <>
      <div className="row between">
        <h1>Lineup — {data.label}</h1>
        <label className="field">
          Week
          <select value={activeWeek} onChange={(e) => setWeek(Number(e.target.value))}>
            {(league?.timeline?.weeks ?? []).map((w: any) => (
              <option key={w.week} value={w.week} disabled={w.week < currentWeek}>
                {w.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="small muted">
        {fmt.date(data.week_start)} – {fmt.date(data.week_end)} · stats shown through{' '}
        {data.stats_through} · lineups lock Sunday night, when the week begins.
      </p>

      <ErrorBanner message={msg} />
      {ok ? <div className="banner info">{ok}</div> : null}
      {data.locked ? (
        <div className="banner">This week is locked — the deadline has passed.</div>
      ) : null}

      <div className="card tight">
        <div className="row between">
          <h3 style={{ margin: 0 }}>
            Active {activeCount} / {activeCapacity}
          </h3>
          <span className="small muted">
            bench {counts.BENCH ?? 0}/{benchCapacity} · IL {ilUsed}/{data.il_size}
          </span>
        </div>

        <div className="scroll-x">
          <table>
            <thead>
              <tr>
                <th>Slot</th>
                <th>Player</th>
                <th className="right">Pts</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((p: any) => (
                <tr key={p.player_id}>
                  <td>
                    <select
                      value={draft[p.player_id] ?? p.slot}
                      disabled={data.locked}
                      onChange={(e) => setDraft({ ...draft, [p.player_id]: e.target.value })}
                    >
                      {slotOptions(p).map((s) => (
                        <option key={s} value={s}>{s}</option>
                      ))}
                    </select>
                  </td>
                  <td>
                    <PlayerLink code={code} playerId={p.player_id} name={p.name} />{' '}
                    <span className="small muted">{p.mlb_team}</span>
                    <br />
                    <PositionTags positions={p.positions} /> <IlBadge il={p.il} />
                  </td>
                  <td className="right mono">
                    {p.points_to_date.toFixed(0)}
                    <br />
                    <span className="small muted">{p.games_to_date} g</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {!data.locked ? (
          <div className="row" style={{ marginTop: '.75rem' }}>
            <button className="primary" onClick={save}>Save lineup</button>
            <button onClick={autofill}>Auto-fill</button>
          </div>
        ) : null}
      </div>

      <p className="footer-note">
        Players the historical transaction log has on the injured list cannot be started. That
        is not a setting — the replay follows what actually happened.
      </p>
    </>
  );
}
