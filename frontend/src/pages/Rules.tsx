import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading } from '../components/common';

export default function Rules({ code }: { code: string }) {
  const { data, error } = useApi<any>(`/api/leagues/${code}`, code);
  const { data: coverage } = useApi<any>('/api/meta/coverage');

  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="rules" />;

  const cfg = data.config;
  const scoring = data.scoring;

  return (
    <>
      <h1>League rules</h1>

      <div className="banner">
        <strong>Honour-system note.</strong> Every one of these games already happened. The app
        shows you nothing but stats through the current replay date, and waiver bids are blind —
        but none of that erases what you personally remember about this season. Blind FAAB removes
        the app's information advantage; it cannot remove yours. If the league decides
        memory-based sniping is a problem, rosters already freeze once the playoffs begin —
        the bracket is decided by the team you built.
      </div>

      {cfg.roster_discrepancy ? <div className="banner">{cfg.roster_discrepancy.message}</div> : null}

      <div className="grid two">
        <div className="card tight">
          <h3>Format</h3>
          <ul className="small" style={{ paddingLeft: '1.1rem' }}>
            <li>{cfg.team_count} teams, weekly head-to-head, Monday–Sunday.</li>
            <li>
              {cfg.regular_season_weeks}-week regular season, then {cfg.playoff_weeks} playoff
              weeks ({cfg.total_weeks} total). The All-Star break week is skipped entirely.
            </li>
            <li>Standings by record; total points break ties.</li>
            <li>
              Top {cfg.playoff_teams} make the playoffs — single elimination, no byes, and the
              final is two weeks on combined points.
            </li>
            <li>Lineups lock when the week starts. One day of games processes each night at 8:00 PM Central.</li>
            <li>
              {cfg.draft_pick_seconds
                ? `Draft picks are on a ${cfg.draft_pick_seconds}-second clock — miss it and the room picks for you.`
                : 'The draft has no pick clock; the room waits for each manager.'}
            </li>
            <li>Season year drawn at random from cached seasons ({cfg.eligible_year_min}+).</li>
          </ul>
        </div>

        <div className="card tight">
          <h3>Roster — {cfg.roster_size} players</h3>
          <div className="row small">
            {Object.entries(cfg.active_slots).map(([slot, n]: any) => (
              <span className="tag slot" key={slot}>{slot} ×{n}</span>
            ))}
            <span className="tag">Bench ×{cfg.bench_size}</span>
            <span className="tag il">IL ×{cfg.il_size}</span>
          </div>
          <p className="small muted">
            Eligibility follows the positions a player actually played that season, so a
            multi-position player can fill any of them. UTIL takes any batter, P takes any
            pitcher. An IL slot only accepts a player the real transaction log has on the
            injured list that week; unused IL slots hold bench players.
          </p>
          <p className="small muted">
            FAAB budget: {cfg.faab_budget} · waivers clear after {cfg.waiver_clear_days} days ·{' '}
            {cfg.freeze_adds_in_playoffs
              ? 'rosters freeze once the playoffs begin'
              : 'adds stay open through the playoffs'}
            {cfg.freeze_adds_final_weeks
              ? `, and for the final ${cfg.freeze_adds_final_weeks} weeks of the regular season`
              : ''}.
          </p>
        </div>
      </div>

      <div className="grid two">
        <div className="card tight">
          <h3>Batting</h3>
          <ScoreTable values={scoring.batting} />
        </div>
        <div className="card tight">
          <h3>Pitching</h3>
          <ScoreTable values={scoring.pitching} />
        </div>
      </div>

      {coverage ? (
        <div className="card tight">
          <h3>Where the data comes from</h3>
          <p className="small muted">
            Some scoring categories are not in a standard box score. This is what each source
            can actually supply — anything marked <em>missing</em> will never score.
          </p>
          {Object.values(coverage.sources).map((s: any) => (
            <div key={s.source} style={{ marginBottom: '.75rem' }}>
              <strong className="small">{s.label}</strong>
              <div className="small muted">{s.notes}</div>
              {s.needs_attention?.length ? (
                <div className="row small" style={{ marginTop: '.25rem' }}>
                  {s.needs_attention.map((stat: string) => (
                    <span className="tag" key={stat}>
                      {stat} — {s.levels[stat]}
                    </span>
                  ))}
                </div>
              ) : (
                <span className="tag win">everything supported</span>
              )}
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}

function ScoreTable({ values }: { values: Record<string, number> }) {
  return (
    <table>
      <tbody>
        {Object.entries(values).map(([k, v]) => (
          <tr key={k}>
            <td>{k}</td>
            <td className="right mono">{v > 0 ? '+' : ''}{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
