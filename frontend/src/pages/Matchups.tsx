import { useState } from 'react';
import { fmt } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading } from '../components/common';
import { Link } from '../lib/router';

const BONUS_EMOJI: Record<string, string> = { CYC: '🔄', SLAM: '💣', NH: '🚫', PG: '💎' };

export default function Matchups({ code }: { code: string }) {
  const { data: league } = useApi<any>(`/api/leagues/${code}`, code);
  const currentWeek = league?.timeline?.current_week ?? 1;
  const [week, setWeek] = useState<number | null>(null);
  const activeWeek = week ?? currentWeek;
  const { data, error } = useApi<any>(`/api/leagues/${code}/recap?week=${activeWeek}`, code, [activeWeek]);

  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="matchups" />;

  return (
    <>
      <div className="row between">
        <h1>{data.label}</h1>
        <label className="field">
          Week
          <select value={activeWeek} onChange={(e) => setWeek(Number(e.target.value))}>
            {(league?.timeline?.weeks ?? []).map((w: any) => (
              <option key={w.week} value={w.week} disabled={w.week > currentWeek}>
                {w.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p className="small muted">{fmt.date(data.start)} – {fmt.date(data.end)}</p>

      {data.bonuses.length ? (
        <div className="card tight">
          <h3>Called shots</h3>
          {data.bonuses.map((b: any, i: number) => (
            <div key={i} className="small">
              {BONUS_EMOJI[b.bonus] ?? '⭐'} <strong>{b.player}</strong> {b.label} on{' '}
              {fmt.date(b.date)} — {fmt.points(b.points)} bonus points for {b.team}
            </div>
          ))}
        </div>
      ) : null}

      <div className="grid two">
        {data.matchups.map((m: any) => {
          // Whoever is ahead reads bright, final or not — a live matchup is
          // the thing managers check most, and it should be scannable.
          const leader =
            m.home_points === m.away_points ? null : m.home_points > m.away_points ? 'home' : 'away';
          return (
            <div className="card tight" key={m.slot}>
              <div className="row between">
                <strong className={leader === 'away' ? 'muted' : ''}>{m.home_name}</strong>
                <span className="mono">{fmt.points(m.home_points)}</span>
              </div>
              <div className="row between">
                <strong className={leader === 'home' ? 'muted' : ''}>{m.away_name}</strong>
                <span className="mono">{fmt.points(m.away_points)}</span>
              </div>
              <div className="row" style={{ marginTop: '.35rem' }}>
                <span className={`tag ${m.complete ? 'win' : ''}`}>
                  {m.complete ? 'final' : 'in progress'}
                </span>
                {m.stage !== 'regular' ? <span className="tag">{m.stage}</span> : null}
              </div>

              <div className="grid two" style={{ marginTop: '.5rem' }}>
                <TopList code={code} title={m.home_name} rows={m.home_top} />
                <TopList code={code} title={m.away_name} rows={m.away_top} />
              </div>
            </div>
          );
        })}
      </div>

      {!data.matchups.length ? (
        <p className="muted">No matchups scheduled for this week.</p>
      ) : null}
    </>
  );
}

function TopList({ code, title, rows }: { code: string; title: string; rows: any[] }) {
  return (
    <div>
      <h3>{title}</h3>
      {rows.length ? rows.map((r) => (
        <div key={r.player_id} className="row between small">
          <Link to={`/l/${code}/players/${r.player_id}`}>{r.name}</Link>
          <span className="mono">{fmt.points(r.points)}</span>
        </div>
      )) : <p className="muted small">No points yet.</p>}
    </div>
  );
}
