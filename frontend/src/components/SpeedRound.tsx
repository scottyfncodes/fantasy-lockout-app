/**
 * The draft-order mini-game.
 *
 * The server owns the clock, the ball's position and the tap counts; this
 * component renders what it broadcasts and sends taps back. Everyone sees the
 * same ball in the same place at the same moment, and a client cannot report
 * its own score — only taps, which the server rate-limits.
 */

export default function SpeedRound({
  state,
  onTap,
  myTeamId,
}: {
  state: any;
  onTap: () => void;
  myTeamId: string;
}) {
  const { target, standings = [], remaining, countdown } = state;
  const running = state.state === 'running';

  return (
    <div className="grid sidebar">
      <div className="card tight">
        <div className="row between">
          <h3 style={{ margin: 0 }}>Speed Round</h3>
          <span className="mono" style={{ fontSize: '1.4rem' }}>
            {running ? `${remaining.toFixed(1)}s` : state.state}
          </span>
        </div>

        <div
          className="arena"
          onPointerDown={(e) => {
            // Taps anywhere register; hitting the ball is the fun part, not a
            // hit-test the server has to adjudicate.
            e.preventDefault();
            if (running) onTap();
          }}
        >
          {target ? (
            <div
              className="ball"
              style={{
                left: `${target.x * 100}%`,
                top: `${target.y * 100}%`,
                width: `${target.size * 100}%`,
                aspectRatio: '1',
                fontSize: `${target.size * 60}px`,
              }}
            >
              ⚾
            </div>
          ) : null}

          {state.state === 'countdown' ? (
            <div className="arena-overlay">
              <span className="countdown">{Math.ceil(countdown)}</span>
            </div>
          ) : null}
          {state.state === 'ended' || state.state === 'finished' ? (
            <div className="arena-overlay">Time!</div>
          ) : null}
          {state.state === 'waiting' ? (
            <div className="arena-overlay">
              <span style={{ fontSize: '1rem' }}>Waiting for the commissioner…</span>
            </div>
          ) : null}
        </div>
        <p className="small muted">Tap as fast as you can. Highest count drafts first.</p>
      </div>

      <div className="card tight">
        <h3>Live scores</h3>
        <table>
          <tbody>
            {standings.map((s: any) => (
              <tr key={s.team_id}>
                <td style={{ width: '1.5rem' }} className="muted mono">{s.pick}</td>
                <td>
                  {s.name} {s.is_bot ? <span className="tag bot">bot</span> : null}
                  {s.team_id === myTeamId ? <span className="tag">you</span> : null}
                </td>
                <td className="right mono" style={{ fontWeight: 700 }}>{s.score}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
