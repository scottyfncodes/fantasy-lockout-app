import { useState } from 'react';
import { fmt, tokens } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading, PlayerLink } from '../components/common';

const BONUS_EMOJI: Record<string, string> = { SLAM: '💣' };

/**
 * One replayed day.
 *
 * The league moves a day a night, so this is the morning page: what your
 * starters did while you were asleep, what the bench did instead, and which
 * way the week's matchup moved because of it.
 */
export default function LastNight({ code }: { code: string }) {
  const [date, setDate] = useState<string | null>(null);
  const { data, error } = useApi<any>(
    `/api/leagues/${code}/day${date ? `?date=${date}` : ''}`, code, [date],
  );

  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="last night" />;
  if (!data.date) {
    return (
      <>
        <h1>Last night</h1>
        <p className="muted">
          Nothing has been replayed yet. The first day drops at the nightly sim.
        </p>
      </>
    );
  }

  const myTeamId = tokens.teamId(code);
  const mine = data.teams.find((t: any) => t.team_id === myTeamId);
  const myMatchup = data.matchups.find(
    (m: any) => m.home_team_id === myTeamId || m.away_team_id === myTeamId,
  );

  return (
    <>
      <div className="row between">
        <h1>{data.is_latest ? 'Last night' : fmt.date(data.date)}</h1>
        <div className="row">
          <button className="small" disabled={!data.prev} onClick={() => setDate(data.prev)}>
            ‹ prev
          </button>
          <button className="small" disabled={!data.next} onClick={() => setDate(data.next)}>
            next ›
          </button>
          <button className="small" disabled={data.is_latest} onClick={() => setDate(null)}>
            latest
          </button>
        </div>
      </div>
      <p className="small muted">
        {new Date(`${data.date}T12:00:00`).toLocaleDateString(undefined, {
          weekday: 'long', month: 'long', day: 'numeric',
        })}{' '}
        · {data.week_label} · day {data.day_number} of {data.dates_played} ·{' '}
        {fmt.points(data.league_points)} points across the league
      </p>

      {data.bonuses.length ? (
        <div className="card tight">
          <h3>Called shots</h3>
          {data.bonuses.map((b: any, i: number) => (
            <div key={i} className="small">
              {BONUS_EMOJI[b.bonus] ?? '⭐'} <strong>{b.player}</strong> {b.label} —{' '}
              {fmt.points(b.points)} bonus points for {b.team}
            </div>
          ))}
        </div>
      ) : null}

      {myMatchup ? (
        <MyMatchup m={myMatchup} myTeamId={myTeamId} />
      ) : mine ? (
        <p className="muted small">
          You have no matchup this week — the points below are for pride.
        </p>
      ) : null}

      {mine ? (
        <div className="grid two">
          <TeamDay code={code} team={mine} slots={data.active_slots} title="What you started" />
          <BenchDay code={code} team={mine} latest={data.is_latest} />
        </div>
      ) : null}

      <div className="grid two">
        <div className="card tight">
          <h3>Top scores of the day</h3>
          {data.top_performers.length ? (
            data.top_performers.map((p: any) => (
              <div key={p.player_id} className="row between small">
                <span>
                  <PlayerLink code={code} playerId={p.player_id} name={p.name} />{' '}
                  <span className="muted">{p.team}</span>
                </span>
                <span className="mono">{fmt.points(p.points)}</span>
              </div>
            ))
          ) : (
            <p className="muted small">No games on this date.</p>
          )}
        </div>

        <div className="card tight">
          <h3>Everyone&rsquo;s night</h3>
          <table>
            <tbody>
              {data.teams.map((t: any) => (
                <tr key={t.team_id}>
                  <td className="small">
                    {t.name}
                    {t.team_id === myTeamId ? <span className="tag slot">you</span> : null}
                    {t.is_bot ? <span className="tag bot">bot</span> : null}
                  </td>
                  <td className="right muted small">{t.started.length} played</td>
                  <td className="right mono">{fmt.points(t.points)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

function MyMatchup({ m, myTeamId }: { m: any; myTeamId: string }) {
  const iAmHome = m.home_team_id === myTeamId;
  const me = {
    name: iAmHome ? m.home_name : m.away_name,
    day: iAmHome ? m.home_day : m.away_day,
    week: iAmHome ? m.home_week : m.away_week,
  };
  const them = {
    name: iAmHome ? m.away_name : m.home_name,
    day: iAmHome ? m.away_day : m.home_day,
    week: iAmHome ? m.away_week : m.home_week,
  };
  const margin = me.week - them.week;
  return (
    <div className="card tight">
      <h3>Your matchup {m.stage !== 'regular' ? <span className="tag">{m.stage}</span> : null}</h3>
      <table>
        <thead>
          <tr><th /><th className="right">day</th><th className="right">week</th></tr>
        </thead>
        <tbody>
          <tr>
            <td><strong>{me.name}</strong></td>
            <td className="right mono">{fmt.points(me.day)}</td>
            <td className="right mono">{fmt.points(me.week)}</td>
          </tr>
          <tr>
            <td>{them.name}</td>
            <td className="right mono">{fmt.points(them.day)}</td>
            <td className="right mono">{fmt.points(them.week)}</td>
          </tr>
        </tbody>
      </table>
      <p className="small" style={{ marginTop: '.4rem' }}>
        {margin === 0 ? (
          <span className="muted">Dead level.</span>
        ) : (
          <>
            You are <strong>{margin > 0 ? 'up' : 'down'} {fmt.points(Math.abs(margin))}</strong>{' '}
            on the week{m.complete ? ' — final.' : '.'}
          </>
        )}
      </p>
    </div>
  );
}

function TeamDay({
  code, team, slots, title,
}: { code: string; team: any; slots: number; title: string }) {
  return (
    <div className="card tight">
      <div className="row between">
        <h3>{title}</h3>
        <span className="mono">{fmt.points(team.points)}</span>
      </div>
      <p className="small muted">
        {team.started.length} of your {slots} active slots had a game.
      </p>
      {team.started.map((p: any) => (
        <div key={p.player_id} className="row between small" style={{ padding: '.15rem 0' }}>
          <span>
            <span className="tag slot">{p.slot}</span>{' '}
            <PlayerLink code={code} playerId={p.player_id} name={p.name} />
            <br />
            <span className="muted mono" style={{ fontSize: '.72rem' }}>
              {describe(p.breakdown)}
            </span>
          </span>
          <span className="mono">{fmt.points(p.points)}</span>
        </div>
      ))}
      {!team.started.length ? <p className="muted small">Nobody in your lineup played.</p> : null}
    </div>
  );
}

function BenchDay({ code, team, latest }: { code: string; team: any; latest: boolean }) {
  // "Regret" is only real if starting him would have helped — beating your
  // *worst* starter is the bar, since that is the slot he'd have taken. An
  // injured player is never regret: you were not allowed to start him.
  const best = team.bench.find((p: any) => p.slot !== 'IL');
  const worstStarter = team.started.length
    ? Math.min(...team.started.map((p: any) => p.points))
    : null;
  const regret = best && worstStarter !== null && best.points > worstStarter;
  return (
    <div className="card tight">
      <h3>On your bench</h3>
      {best ? (
        <p className="small">
          {regret ? (
            <>
              <strong>{best.name}</strong> put up {fmt.points(best.points)} while you sat him —
              more than {team.started.filter((p: any) => p.points < best.points).length} of the{' '}
              {team.started.length} starters who played.
            </>
          ) : (
            <span className="muted">Your bench produced nothing your lineup didn&rsquo;t beat.</span>
          )}
        </p>
      ) : (
        <p className="muted small">No benched player had a game.</p>
      )}
      {team.bench.map((p: any) => (
        <div key={p.player_id} className="row between small" style={{ padding: '.15rem 0' }}>
          <span>
            <span className={p.slot === 'IL' ? 'tag il' : 'tag bench'}>{p.slot}</span>{' '}
            <PlayerLink code={code} playerId={p.player_id} name={p.name} />
          </span>
          <span className="mono">{fmt.points(p.points)}</span>
        </div>
      ))}
      {!latest ? (
        <p className="small muted" style={{ marginTop: '.5rem' }}>
          Bench lines are read off your current roster, so a player you have since dropped
          will not appear on an older day.
        </p>
      ) : null}
    </div>
  );
}

/** The scoring breakdown is stored as points per category, so sign every one. */
function describe(breakdown: Record<string, number>): string {
  return Object.entries(breakdown)
    .filter(([, v]) => v)
    .map(([k, v]) => `${k} ${v > 0 ? '+' : '−'}${Math.abs(v).toFixed(1)}`)
    .join(' · ');
}
