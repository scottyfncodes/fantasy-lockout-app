import { useState } from 'react';
import { api } from '../lib/api';
import { useApi } from '../lib/hooks';
import { ErrorBanner, Loading } from '../components/common';

export default function Commissioner({ code }: { code: string }) {
  const { data, error, reload } = useApi<any>(`/api/leagues/${code}`, code);
  const { data: tx, reload: reloadTx } = useApi<any>(`/api/leagues/${code}/transactions?limit=40`, code);
  const [msg, setMsg] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [days, setDays] = useState(1);
  const [scoringEdits, setScoringEdits] = useState<Record<string, Record<string, number>>>({});
  const [settings, setSettings] = useState<Record<string, any>>({});

  if (error) return <ErrorBanner message={error} />;
  if (!data) return <Loading what="league" />;

  const cfg = data.config;

  async function patch(body: any) {
    setMsg(null);
    setOk(null);
    try {
      await api.patch(`/api/leagues/${code}/settings`, code, body);
      setOk('Settings saved.');
      reload();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  async function advance() {
    setMsg(null);
    setOk(null);
    try {
      const res = await api.post(`/api/leagues/${code}/advance`, code, { days });
      setOk(`Advanced to ${res.last_simulated_date ?? '—'} (week ${res.current_week}, ${res.phase}).`);
      reload();
      reloadTx();
    } catch (e: any) {
      setMsg(e.message);
    }
  }

  const setScore = (half: string, key: string, value: number) =>
    setScoringEdits({ ...scoringEdits, [half]: { ...(scoringEdits[half] ?? {}), [key]: value } });

  return (
    <>
      <h1>Commissioner</h1>
      <ErrorBanner message={msg} />
      {ok ? <div className="banner info">{ok}</div> : null}

      <div className="grid two">
        <div className="card tight">
          <h3>League settings</h3>
          <p className="small muted">
            Team count and roster shape lock once the draft begins. The replay year is drawn at
            random and the nightly pace is fixed at 8:00 PM Central — neither is adjustable.
          </p>
          {[
            ['team_count', 'Team count (lobby only)'],
            ['min_teams', 'Bot-fill minimum'],
            ['lobby_timeout_seconds', 'Lobby timeout (seconds)'],
            ['bench_size', 'Bench size'],
            ['il_size', 'IL slots'],
            ['faab_budget', 'FAAB budget'],
            ['waiver_clear_days', 'Waiver clear days'],
            ['freeze_adds_final_weeks', 'Freeze adds for final N weeks'],
            ['draft_pick_seconds', 'Seconds per draft pick (0 = no clock)'],
          ].map(([key, label]) => (
            <label className="field" key={key} style={{ marginBottom: '.4rem' }}>
              {label}
              <input
                type="number"
                value={settings[key] ?? cfg[key]}
                onChange={(e) => setSettings({ ...settings, [key]: Number(e.target.value) })}
              />
            </label>
          ))}
          <label className="field" style={{ marginBottom: '.4rem' }}>
            Bots participate in waivers
            <select
              value={String(settings.bots_use_waivers ?? cfg.bots_use_waivers)}
              onChange={(e) => setSettings({ ...settings, bots_use_waivers: e.target.value === 'true' })}
            >
              <option value="true">yes</option>
              <option value="false">no</option>
            </select>
          </label>
          <label className="field" style={{ marginBottom: '.4rem' }}>
            Draft order mini-game
            <select
              value={settings.draft_order_mode ?? cfg.draft_order_mode}
              onChange={(e) => setSettings({ ...settings, draft_order_mode: e.target.value })}
            >
              <option value="speed_round">Speed Round (tap the ball)</option>
              <option value="randomizer">Animated randomizer</option>
            </select>
          </label>
          <button className="primary" onClick={() => patch({ config: settings })}>
            Save settings
          </button>
        </div>

        <div>
          <div className="card tight">
            <h3>Run the replay</h3>
            <p className="small muted">
              The nightly job advances one day at 8:00 PM Central. Use this to catch a league up
              or to demo the season quickly.
            </p>
            <div className="row">
              <input
                type="number"
                min={1}
                max={200}
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                style={{ width: '6rem' }}
              />
              <button className="primary" onClick={advance}>Advance days</button>
            </div>
            {data.timeline ? (
              <p className="small muted">
                Currently {data.timeline.label}, replayed through {data.timeline.as_of}.
              </p>
            ) : null}
          </div>

          <div className="card tight">
            <h3>Scoring</h3>
            <p className="small muted">Changes apply to weeks scored after the edit.</p>
            <div className="scroll-x">
              <table>
                <tbody>
                  {(['batting', 'pitching'] as const).flatMap((half) =>
                    Object.entries(data.scoring[half]).map(([k, v]: any) => (
                      <tr key={`${half}.${k}`}>
                        <td className="small">{half === 'batting' ? 'B' : 'P'} · {k}</td>
                        <td className="right">
                          <input
                            type="number"
                            step="0.5"
                            style={{ width: '5.5rem' }}
                            value={scoringEdits[half]?.[k] ?? v}
                            onChange={(e) => setScore(half, k, Number(e.target.value))}
                          />
                        </td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
            <button style={{ marginTop: '.5rem' }} onClick={() => patch({ scoring: scoringEdits })}>
              Save scoring
            </button>
          </div>
        </div>
      </div>

      <div className="card tight">
        <h3>Transaction log</h3>
        <div className="scroll-x">
          <table>
            <thead>
              <tr><th>Week</th><th>Type</th><th>Team</th><th>Player</th><th>Detail</th></tr>
            </thead>
            <tbody>
              {(tx?.transactions ?? []).map((t: any) => (
                <tr key={t.id}>
                  <td className="mono small">{t.week}</td>
                  <td className="small">{t.kind}</td>
                  <td className="small">{t.team_name}</td>
                  <td className="small">{t.player_name}</td>
                  <td className="small muted">{t.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
