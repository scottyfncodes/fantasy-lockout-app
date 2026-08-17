import { RouterProvider, matchRoute, useRouter } from './lib/router';
import Home from './pages/Home';
import Lobby from './pages/Lobby';
import Draft from './pages/Draft';
import Team from './pages/Team';
import Waivers from './pages/Waivers';
import Standings from './pages/Standings';
import Matchups from './pages/Matchups';
import LastNight from './pages/LastNight';
import PlayerPage from './pages/PlayerPage';
import Rules from './pages/Rules';
import Commissioner from './pages/Commissioner';
import Nav from './components/Nav';

const ROUTES: [string, (p: Record<string, string>) => JSX.Element][] = [
  ['/', () => <Home />],
  ['/join/:code', (p) => <Lobby code={p.code} />],
  ['/l/:code', (p) => <Lobby code={p.code} />],
  ['/l/:code/draft', (p) => <Draft code={p.code} />],
  ['/l/:code/team', (p) => <Team code={p.code} />],
  ['/l/:code/team/:teamId', (p) => <Team code={p.code} teamId={p.teamId} />],
  ['/l/:code/waivers', (p) => <Waivers code={p.code} />],
  ['/l/:code/standings', (p) => <Standings code={p.code} />],
  ['/l/:code/matchups', (p) => <Matchups code={p.code} />],
  ['/l/:code/day', (p) => <LastNight code={p.code} />],
  ['/l/:code/players/:playerId', (p) => <PlayerPage code={p.code} playerId={p.playerId} />],
  ['/l/:code/rules', (p) => <Rules code={p.code} />],
  ['/l/:code/commissioner', (p) => <Commissioner code={p.code} />],
];

function Routes() {
  const { path } = useRouter();
  for (const [pattern, render] of ROUTES) {
    const params = matchRoute(pattern, path);
    if (params) {
      return (
        <div className="app">
          {params.code ? <Nav code={params.code} /> : null}
          {render(params)}
        </div>
      );
    }
  }
  return (
    <div className="app">
      <div className="card">
        <h1>Not found</h1>
        <p className="muted">
          Nothing lives at <code>{path}</code>. <a href="/">Back to the start</a>.
        </p>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <RouterProvider>
      <Routes />
    </RouterProvider>
  );
}
