import { fmt } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading } from '../components/common';
import { Link } from '../lib/router';

export default function Standings({ code }: { code: string }) {
  const { data, error } = useApi<any>(`/api/leagues/${code}/standings`, code);
  const { data: leaders } = useApi<any>(`/api/leagues/${code}/leaders?limit=15`, code);

  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="standings" />;

  return (
    <>
      <h1>Standings</h1>
      {data.champion ? (
        <div className="banner info">
          🏆 <strong>{data.champion.name}</strong> wins it — {fmt.points(data.champion.points)} to{' '}
          {fmt.points(data.champion.runner_up_points)} over {data.champion.runner_up} across the
          two-week final.
        </div>
      ) : null}

      <div className="card tight scroll-x">
        <table>
          <thead>
            <tr>
              <th>#</th>
              <th>Team</th>
              <th className="right">W-L</th>
              <th className="right">Points for</th>
              <th className="right">Against</th>
              <th className="right">FAAB</th>
            </tr>
          </thead>
          <tbody>
            {data.standings.map((t: any) => (
              <tr key={t.id}>
                <td className="mono muted">{t.rank}</td>
                <td>
                  <Link to={`/l/${code}/team/${t.id}`}>{t.name}</Link>{' '}
                  {t.is_bot ? <span className="tag bot">bot</span> : null}
                </td>
                <td className="right mono">{fmt.record(t)}</td>
                <td className="right mono">{fmt.points(t.points_for)}</td>
                <td className="right mono muted">{fmt.points(t.points_against)}</td>
                <td className="right mono muted">{t.faab_remaining}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="small muted">Ranked by record; total points break ties.</p>

      {data.bracket ? <Bracket bracket={data.bracket} /> : null}

      {leaders?.leaders?.length ? (
        <div className="card tight">
          <h3>Fantasy scoring leaders (through {leaders.as_of})</h3>
          <div className="scroll-x">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Fantasy team</th>
                  <th className="right">Pts</th>
                </tr>
              </thead>
              <tbody>
                {leaders.leaders.map((l: any) => (
                  <tr key={l.player_id}>
                    <td>
                      <Link to={`/l/${code}/players/${l.player_id}`}>{l.name}</Link>{' '}
                      <span className="small muted">{l.positions}</span>
                    </td>
                    <td className="small muted">{l.team_name}</td>
                    <td className="right mono">{fmt.points(l.pts)}</td>
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

function Bracket({ bracket }: { bracket: any }) {
  return (
    <div className="card tight">
      <h3>Playoff bracket — top {bracket.playoff_teams} seeds</h3>
      <div className="bracket">
        {bracket.rounds.map((round: any) => (
          <div key={round.stage}>
            <h3>{round.stage} · week {round.weeks.join('+')}</h3>
            {round.series.length ? round.series.map((s: any) => (
              <div className="series" key={`${round.stage}-${s.slot}`}>
                {['home', 'away'].map((side) => (
                  <div
                    key={side}
                    className={`side ${s.winner && s.winner === s[side].id ? 'won' : ''}`}
                  >
                    <span>
                      {s[side].seed ? <span className="muted mono">{s[side].seed} </span> : null}
                      {s[side].name ?? 'TBD'}
                    </span>
                    <span className="mono">{fmt.points(s[side].points)}</span>
                  </div>
                ))}
              </div>
            )) : <p className="muted small">Not set yet.</p>}
          </div>
        ))}
      </div>
      <p className="small muted">
        Single elimination, no byes. The final is two weeks, decided on combined points.
      </p>
    </div>
  );
}
