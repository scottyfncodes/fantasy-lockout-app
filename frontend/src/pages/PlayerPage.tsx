import { fmt } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading, PositionTags } from '../components/common';

export default function PlayerPage({ code, playerId }: { code: string; playerId: string }) {
  const { data, error } = useApi<any>(`/api/leagues/${code}/players/${playerId}`, code);
  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="player" />;

  const bat = data.totals?.batting;
  const pit = data.totals?.pitching;

  return (
    <>
      <h1>{data.player.name}</h1>
      <p className="small muted">
        {data.player.mlb_team} · <PositionTags positions={data.player.positions} /> · eligible at{' '}
        {data.eligible_slots.join(', ') || '—'} ·{' '}
        {data.owner ? `rostered by ${data.owner.name}` : 'free agent'}
      </p>
      <div className="banner info small">
        Everything on this page stops at <strong>{data.as_of}</strong>, the replay's current
        date. Future games and injuries that have not happened yet in the replay are not shown.
      </div>

      <div className="grid two">
        <div className="card tight">
          <h3>Real stats to date</h3>
          {bat ? (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr><th>G</th><th>AB</th><th>R</th><th>H</th><th>HR</th><th>RBI</th><th>BB</th><th>K</th><th>SB</th></tr>
                </thead>
                <tbody>
                  <tr className="mono">
                    <td>{bat.g}</td><td>{bat.ab}</td><td>{bat.r}</td><td>{bat.h}</td>
                    <td>{bat.hr}</td><td>{bat.rbi}</td><td>{bat.bb}</td><td>{bat.so}</td><td>{bat.sb}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : null}
          {pit ? (
            <div className="scroll-x">
              <table>
                <thead>
                  <tr><th>G</th><th>GS</th><th>IP</th><th>W</th><th>SV</th><th>CG</th><th>ER</th><th>K</th></tr>
                </thead>
                <tbody>
                  <tr className="mono">
                    <td>{pit.g}</td><td>{pit.gs}</td><td>{(pit.outs / 3).toFixed(1)}</td>
                    <td>{pit.w}</td><td>{pit.sv}</td><td>{pit.cg}</td><td>{pit.er}</td><td>{pit.so}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          ) : null}
          {!bat && !pit ? <p className="muted small">No games played yet in the replay.</p> : null}
        </div>

        <div className="card tight">
          <h3>Fantasy points</h3>
          <div style={{ fontSize: '2rem', fontWeight: 700 }}>
            {fmt.points(data.totals?.points ?? 0)}
          </div>
          {data.totals?.breakdown ? (
            <div className="row small" style={{ marginTop: '.35rem' }}>
              {Object.entries(data.totals.breakdown)
                .sort((a: any, b: any) => b[1] - a[1])
                .map(([k, v]: any) => (
                  <span className="tag" key={k}>{k} {v > 0 ? '+' : ''}{v.toFixed(1)}</span>
                ))}
            </div>
          ) : null}

          {data.by_week?.length ? (
            <>
              <h3 style={{ marginTop: '1rem' }}>By fantasy week (while started)</h3>
              <div className="row small">
                {data.by_week.map((w: any) => (
                  <span className="tag" key={w.week}>W{w.week}: {fmt.points(w.pts)}</span>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </div>

      {data.il_log?.length ? (
        <div className="card tight">
          <h3>Injured list history</h3>
          {data.il_log.map((s: any, i: number) => (
            <div key={i} className="small">
              {s.start_date} → {s.end_date ?? 'still out'} · {s.kind} · {s.note}
            </div>
          ))}
        </div>
      ) : null}

      {data.game_log?.length ? (
        <div className="card tight">
          <h3>Recent games</h3>
          <div className="scroll-x">
            <table>
              <thead>
                <tr><th>Date</th><th>Batting</th><th>Pitching</th></tr>
              </thead>
              <tbody>
                {[...data.game_log].reverse().map((g: any) => (
                  <tr key={g.date}>
                    <td className="mono small">{g.date}</td>
                    <td className="small">
                      {g.batting
                        ? `${g.batting.h}-${g.batting.ab}, ${g.batting.r} R, ${g.batting.hr} HR, ${g.batting.rbi} RBI`
                        : '—'}
                    </td>
                    <td className="small">
                      {g.pitching
                        ? `${(g.pitching.outs / 3).toFixed(1)} IP, ${g.pitching.er} ER, ${g.pitching.so} K`
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </>
  );
}
