import { useEffect, useState } from 'react';

/**
 * The Green Light.
 *
 * Three, two, one, green — the count runs straight into the light with nothing
 * in between. First to tap after green picks first; tap early and you go to
 * the back.
 *
 * The pad still waits to be told: green comes from the server, not from a
 * timer running here, so every client turns on the same tick rather than on
 * whatever its own clock believes.
 */
export default function SpeedRound({
  state,
  onTap,
  result,
}: {
  state: any;
  onTap: () => void;
  result: { false_start?: boolean; reaction?: number; early_by?: number } | null;
}) {
  const green = !!state?.green;
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (green) {
      setFlash(true);
      const t = window.setTimeout(() => setFlash(false), 250);
      return () => window.clearTimeout(t);
    }
    return undefined;
  }, [green]);

  const done = !!result;
  const countdown = state?.counts_down;

  let label = 'Get ready…';
  if (done && result?.false_start) label = 'Too early!';
  else if (done) label = `${Math.round((result?.reaction ?? 0) * 1000)} ms`;
  else if (green) label = 'TAP!';
  else if (countdown > 0) label = String(Math.ceil(countdown));

  return (
    <div className="card center">
      <h3>Green Light</h3>
      <p className="small muted">
        Three, two, one — then it&rsquo;s green. Tap the moment it is. Fastest
        reaction picks first; tap before green and you go to the back of the
        order.
      </p>

      <button
        className={`pad ${green ? 'green' : ''} ${flash ? 'flash' : ''} ${done ? 'done' : ''}`}
        onClick={() => !done && onTap()}
        disabled={done}
        aria-label={green ? 'Tap now' : 'Counting down to green'}
      >
        <span className="pad-label">{label}</span>
      </button>

      {done ? (
        <p className="small muted">
          {result?.false_start
            ? `You jumped ${Math.round((result.early_by ?? 0) * 1000)} ms early — back of the line.`
            : 'In. Waiting for everyone else…'}
        </p>
      ) : null}

      <div className="row" style={{ justifyContent: 'center', flexWrap: 'wrap' }}>
        {(state?.taps ?? []).map((t: any) => (
          <span
            key={t.team_id}
            className={`tag ${t.false_start ? 'loss' : t.done ? 'win' : ''}`}
          >
            {t.name}
            {t.is_bot ? ' (bot)' : ''}
            {t.done ? (t.false_start ? ' ✗' : ' ✓') : ' …'}
          </span>
        ))}
      </div>
    </div>
  );
}
