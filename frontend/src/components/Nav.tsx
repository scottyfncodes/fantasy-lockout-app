import { Link, useRouter } from '../lib/router';
import { tokens } from '../lib/api';
import { useApi } from '../lib/hooks';

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
  const { data } = useApi<any>(`/api/leagues/${code}`, code);
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
