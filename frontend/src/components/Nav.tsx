import { Link, useRouter } from '../lib/router';
import { tokens } from '../lib/api';
import { useApi, useInterval } from '../lib/hooks';

const TABS = [
  ['', 'Lobby'],
  ['/team', 'My Team'],
  ['/day', 'Last Night'],
  ['/matchups', 'Matchups'],
  ['/standings', 'Standings'],
  ['/waivers', 'Waivers'],
  ['/draft', 'Draft'],
  ['/rules', 'Rules'],
];

export default function Nav({ code }: { code: string }) {
  const { path } = useRouter();
  // Refetch on every navigation. The header mounts once and outlives every
  // page, so a single fetch left it reading "season pending" for the rest of
  // the visit — including on the draft page for a season already drawn.
  const { data, reload } = useApi<any>(`/api/leagues/${code}`, code, [path]);
  // Until the year is drawn there is nothing to show but "season pending", and
  // the draw happens on a page that never navigates — so check back until it
  // lands, then stop.
  useInterval(reload, data && !data.season_year ? 5000 : null);
  const isCommissioner = !!tokens.commissioner(code);
  const base = `/l/${code}`;

  return (
    <header className="topbar">
      <div className="topbar-row">
        <div className="brand">
          {data?.name ?? 'Retro Season Replay'}
          <small>
            {data?.season_year ? `${data.season_year} replay` : 'season pending'} · {code}
          </small>
        </div>
        {data?.timeline ? (
          <div className="small muted right">
            {data.timeline.label}
            <br />
            through {data.timeline.as_of}
          </div>
        ) : null}
      </div>
      <nav className="nav">
        {TABS.map(([suffix, label]) => {
          const to = `${base}${suffix}`;
          const active = suffix === '' ? path === to || path === `/join/${code}` : path.startsWith(to);
          return (
            <Link key={label} to={to} className={active ? 'active' : ''}>
              {label}
            </Link>
          );
        })}
        {isCommissioner ? (
          <Link to={`${base}/commissioner`} className={path.endsWith('/commissioner') ? 'active' : ''}>
            Commissioner
          </Link>
        ) : null}
      </nav>
    </header>
  );
}
