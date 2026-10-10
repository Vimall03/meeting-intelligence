import { useEffect, useState } from 'react';
import { defaultMomSettings, terminalMomStates, type MomMeeting, type MomMode, type MomSettings } from '../../lib/mom';
import { problem, userError } from '../../lib/user-messages';
import { UiIcon } from './UiIcon';

export const modeLabels: Record<MomMode, string> = { live_only: 'Live assistance', mom_only: 'Recap only', both: 'Live assistance + recap' };
const modes: { mode: MomMode; title: string; description: string }[] = [
  { mode: 'live_only', title: 'Live assistance', description: 'Related files and task suggestions as you talk. No email recap.' },
  { mode: 'mom_only', title: 'Recap only', description: 'A short email with key points and follow-ups. Live searches and suggestions are off.' },
  { mode: 'both', title: 'Live assistance + recap', description: 'Help during the call, plus a concise recap afterwards.' },
];

export function MinutesSettings({ settings, userId }: { settings?: MomSettings; userId: string }) {
  const [loading, setLoading] = useState(!settings);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string>();
  const [saved, setSaved] = useState(false);
  const prefs = settings ?? defaultMomSettings;
  useEffect(() => {
    let mounted = true;
    chrome.runtime.sendMessage({ type: 'MOM_SETTINGS' }).then(result => {
      if (mounted && !result?.ok) setError(userError(result?.error, 'MI-MOM-001'));
    }).catch(cause => { if (mounted) setError(userError(cause, 'MI-MOM-001')); }).finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [userId]);
  const change = async (mode: MomMode) => {
    if (saving || mode === prefs.mode) return;
    setSaving(true); setSaved(false); setError(undefined);
    try {
      const result = await chrome.runtime.sendMessage({ type: 'MOM_SETTINGS', mode, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' });
      if (!result?.ok) throw new Error(userError(result?.error, 'MI-MOM-002'));
      setSaved(true);
    } catch (cause) { setError(userError(cause, 'MI-MOM-002')); }
    finally { setSaving(false); }
  };
  return <section className="settings-group minutes-settings" aria-labelledby="meeting-mode-heading">
    <h3 id="meeting-mode-heading" className="settings-heading">Meeting mode</h3>
    <p className="settings-intro">Choose how your assistant helps. Listening only starts when you choose Start listening.</p>
    <fieldset className="mode-options" disabled={loading || saving}>
      <legend className="visually-hidden">Meeting mode</legend>
      {modes.map(option => <label key={option.mode} className={`mode-option ${prefs.mode === option.mode ? 'is-selected' : ''}`}>
        <input type="radio" name="meeting-mode" value={option.mode} checked={prefs.mode === option.mode} disabled={option.mode !== 'live_only' && !prefs.minutes_available} onChange={() => void change(option.mode)} />
        <span><strong>{option.title}</strong><small>{option.description}</small></span>
      </label>)}
    </fieldset>
    <p className="privacy-note">Recaps are off by default and go to your signed-in email. Mode changes apply to your next new meeting, including after any rejoin period.</p>
    <p className="privacy-note">Stop listening allows {Math.round(prefs.grace_seconds / 60)} minutes to rejoin. Finish &amp; email recap sends it sooner. Temporary meeting content is cleared after sending.</p>
    <div aria-live="polite">{loading ? <p className="settings-intro">Loading meeting modes…</p> : saving ? <p className="settings-intro">Saving…</p> : saved ? <p className="mode-saved">Saved for your next new meeting.</p> : null}</div>
    {error && <div className="notice error" role="alert">{userError(error, 'MI-MOM-001')} <button className="text-button" onClick={() => { setLoading(true); setError(undefined); void chrome.runtime.sendMessage({ type: 'MOM_SETTINGS' }).then(r => { if (!r?.ok) setError(userError(r?.error, 'MI-MOM-001')); }).catch(e => setError(userError(e, 'MI-MOM-001'))).finally(() => setLoading(false)); }}>Try again</button></div>}
    {!loading && !prefs.minutes_available && !error && <p className="privacy-note">Email recaps aren’t available right now.</p>}
  </section>;
}

export function MinutesCard({ meeting, running, busy, onFinish, graceSeconds }: { meeting: MomMeeting; running: boolean; busy: boolean; onFinish: () => void; graceSeconds: number }) {
  const [error, setError] = useState<string>();
  const [actionBusy, setActionBusy] = useState(false);
  useEffect(() => {
    let mounted = true, pending = false;
    const refresh = async () => {
      if (pending) return;
      pending = true;
      try {
        const result = await chrome.runtime.sendMessage({ type: 'MOM_STATUS', sessionId: meeting.sessionId });
        if (mounted) setError(result?.ok ? undefined : userError(result?.error, 'MI-MOM-005'));
      } catch (cause) { if (mounted) setError(userError(cause, 'MI-MOM-005')); }
      finally { pending = false; }
    };
    if (!terminalMomStates.includes(meeting.state)) void refresh();
    const timer = !terminalMomStates.includes(meeting.state) ? window.setInterval(() => void refresh(), 10_000) : undefined;
    return () => { mounted = false; if (timer) window.clearInterval(timer); };
  }, [meeting.sessionId, meeting.state]);
  const action = async (type: 'MOM_FINISH' | 'MOM_RETRY') => {
    setActionBusy(true); setError(undefined);
    try { const r = await chrome.runtime.sendMessage({ type, sessionId: meeting.sessionId }); if (!r?.ok) throw new Error(userError(r?.error, 'MI-MOM-010')); }
    catch (cause) { setError(userError(cause, 'MI-MOM-010')); }
    finally { setActionBusy(false); }
  };
  const titles: Record<string, string> = { active: running ? 'Your recap is being captured' : 'Ready when your meeting ends', ready: 'Preparing your recap', processing: 'Preparing your recap', sending: 'Sending your recap', cleanup: 'Clearing temporary meeting content', sent: 'Recap sent to your email', empty: 'No speech captured for a recap', expired: 'Temporary meeting content cleared', failed: 'Your recap needs attention', delivery_unknown: 'Check your inbox for your recap' };
  return <section className="minutes-card" aria-label="Meeting recap">
    <div className="minutes-card-heading"><span className="minutes-symbol"><UiIcon name="document" size={18} /></span><div><strong>{titles[meeting.state] ?? 'Checking your recap'}</strong><p>{running ? 'Key points and follow-ups, without the long transcript.' : meeting.state === 'active' ? `Rejoin within ${Math.round(graceSeconds / 60)} minutes to continue the same recap, or finish now.` : meeting.state === 'sent' ? 'A concise summary of the part of your meeting we captured.' : 'Your microphone is off. You can close this panel.'}</p></div></div>
    {meeting.state === 'active' && <button className="recap-finish" disabled={busy || actionBusy} onClick={running ? onFinish : () => void action('MOM_FINISH')}>{busy || actionBusy ? 'Finishing…' : 'Finish & email recap'}</button>}
    <div aria-live="polite">{(error || meeting.supportCode) && <p className="notice error" role="alert">{error ? userError(error, 'MI-MOM-010') : userError(problem(meeting.supportCode as Parameters<typeof problem>[0]), 'MI-MOM-010')}</p>}</div>
    {meeting.state === 'failed' && <button className="text-button" disabled={actionBusy} onClick={() => void action('MOM_RETRY')}>Retry recap</button>}
  </section>;
}
