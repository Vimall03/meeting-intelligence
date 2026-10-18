import { useEffect, useState, type ReactNode } from 'react';
import type { PublicSession } from '../../lib/auth-session';
import { problem, userError } from '../../lib/user-messages';
import { closeSidePanel } from '../../lib/side-panel';
import { PolicyLinks } from './PolicyLinks';
import { UiIcon } from './UiIcon';
import './style.css';

export function AuthGate({ children }: { children: (session: PublicSession, accountControls: ReactNode, accountNotice: ReactNode) => ReactNode }) {
  const [session, setSession] = useState<PublicSession>();
  const [email, setEmail] = useState('');
  const [code, setCode] = useState('');
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string>();
  const [resendAt, setResendAt] = useState(0);
  const [now, setNow] = useState(Date.now());
  const sync = async () => {
    const result = await chrome.runtime.sendMessage({ type: 'AUTH_STATE' });
    if (result?.ok) setSession(result.session);
  };
  useEffect(() => {
    void sync().catch(() => setError(problem('MI-AUTH-104')));
    const listener = (message: { type: string }) => {
      if (message.type === 'AUTH_CHANGED') { setSent(false); setCode(''); void sync(); }
    };
    chrome.runtime.onMessage.addListener(listener);
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => { chrome.runtime.onMessage.removeListener(listener); clearInterval(timer); };
  }, []);
  const action = async (type: 'AUTH_REQUEST' | 'AUTH_VERIFY' | 'AUTH_LOGOUT') => {
    if (busy) return;
    setBusy(true); setError(undefined);
    try {
      const result = await chrome.runtime.sendMessage({ type, email, code });
      if (!result?.ok) throw new Error(result?.error ?? problem('MI-AUTH-104'));
      if (type === 'AUTH_REQUEST') { setSent(true); setResendAt(Date.now() + result.resendAfter * 1000); }
      if (type === 'AUTH_VERIFY') await sync();
      if (type === 'AUTH_LOGOUT') { setSent(false); setCode(''); await sync(); }
    } catch (cause) { setError(userError(cause, 'MI-AUTH-104')); }
    finally { setBusy(false); if (type === 'AUTH_LOGOUT') await sync(); }
  };
  if (session?.signedIn) return children(
    session,
    <div className="account-settings"><span title={session.email}>{session.email}</span><button disabled={busy} onClick={() => void action('AUTH_LOGOUT')}>Sign out</button></div>,
    error && <div className="notice error" role="alert">{userError(error, 'MI-AUTH-104')}</div>,
  );
  const remaining = Math.max(0, Math.ceil((resendAt - now) / 1000));
  return <main className="auth-card">
    <div className="auth-card-header">
      <img src="/meeting-intelligence-icon-128.png" alt="" width="48" height="48" />
      <button type="button" className="panel-close" aria-label="Close Meeting Intelligence panel" title="Close panel" onClick={() => void closeSidePanel().catch(cause => setError(userError(cause, 'MI-APP-003')))}><UiIcon name="close" /></button>
    </div>
    <h1>Welcome to<br/>Meeting Intelligence</h1><p>Find relevant work and follow-ups during your meetings.</p>
    <form onSubmit={event => { event.preventDefault(); void action(sent ? 'AUTH_VERIFY' : 'AUTH_REQUEST'); }}>
      <label>Email<input type="email" autoComplete="email" value={email} onChange={event => setEmail(event.target.value)} disabled={busy || sent} required maxLength={254} /></label>
      {sent && <><p>Check your inbox for your six-digit sign-in code.</p><label>Sign-in code<input autoComplete="one-time-code" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={event => setCode(event.target.value.replace(/\D/g, ''))} disabled={busy} required /></label></>}
      <button className="auth-primary" disabled={busy || !session}>{busy ? 'Please wait…' : sent ? 'Sign in' : 'Send sign-in code'}</button>
    </form>
    {sent && <div className="auth-links"><button disabled={busy || remaining > 0} onClick={() => void action('AUTH_REQUEST')}>{remaining ? `Resend in ${remaining}s` : 'Send a new code'}</button><button disabled={busy} onClick={() => { setSent(false); setCode(''); }}>Change email</button></div>}
    {error && <p className="auth-error" role="alert">{userError(error, 'MI-AUTH-104')}</p>}
    <p className="auth-hint">Google Meet is supported. Zoom in your browser is available as a preview.</p>
    <PolicyLinks />
  </main>;
}
