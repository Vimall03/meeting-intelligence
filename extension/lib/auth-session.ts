import { problem, type MessageCode } from './user-messages.ts';

type Tokens = { accessToken: string; refreshToken: string; expiresIn: number; idToken?: string };
export type Session = Tokens & { expiresAt: number; userId: string; email: string };
export type PublicSession = { signedIn: boolean; userId?: string; email?: string };
export class AuthSession {
  private storage: { read(): Promise<Session | undefined>; write(value: Session): Promise<void>; clear(): Promise<void> };
  private call: (path: string, body?: unknown, access?: string) => Promise<{ status: number; data: any }>;
  private onSignedOut: () => Promise<void>;
  private session?: Session;
  private generation = 0;
  private loaded?: Promise<void>;
  private refreshFlight?: Promise<string>;
  private writes: Promise<unknown> = Promise.resolve();
  private otp?: { email: string; session: string; resendAt: number };
  constructor(storage: { read(): Promise<Session | undefined>; write(value: Session): Promise<void>; clear(): Promise<void> },
    call: (path: string, body?: unknown, access?: string) => Promise<{ status: number; data: any }>,
    onSignedOut: () => Promise<void>) { this.storage = storage; this.call = call; this.onSignedOut = onSignedOut; }
  async initialize() {
    if (!this.loaded) this.loaded = this.storage.read().then(value => {
      if (value?.refreshToken && value.userId && value.email && Number.isFinite(value.expiresAt)) this.session = value;
    });
    await this.loaded;
  }
  async publicState(): Promise<PublicSession> {
    await this.initialize();
    return this.session ? { signedIn: true, userId: this.session.userId, email: this.session.email } : { signedIn: false };
  }
  private queue(write: () => Promise<void>) {
    const next = this.writes.catch(() => undefined).then(write);
    this.writes = next;
    return next;
  }
  private async persist(value: Session, generation: number) {
    await this.queue(async () => {
      if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
      await this.storage.write(value);
      if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
      this.session = value;
    });
  }
  private error(data: any) {
    const code = data?.error?.support_code as MessageCode;
    const approved: MessageCode[] = ['MI-AUTH-101', 'MI-AUTH-102', 'MI-AUTH-103', 'MI-AUTH-104', 'MI-AUTH-105', 'MI-AUTH-106', 'MI-AUTH-107', 'MI-AUTH-108', 'MI-AUTH-109'];
    return new Error(problem(approved.includes(code) ? code : 'MI-AUTH-104'));
  }
  async requestCode(email: string) {
    await this.initialize();
    if (this.session) throw new Error(problem('MI-AUTH-106'));
    email = email.trim().toLowerCase();
    if (this.otp?.email === email && Date.now() < this.otp.resendAt) throw new Error(problem('MI-AUTH-102'));
    const generation = this.generation;
    const response = await this.call('/auth/otp/request', { email });
    if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
    if (response.status !== 202 || !response.data.session) throw this.error(response.data);
    this.otp = { email, session: response.data.session, resendAt: Date.now() + response.data.resendAfter * 1000 };
    return { resendAfter: response.data.resendAfter };
  }
  async verifyCode(code: string) {
    if (!this.otp) throw new Error(problem('MI-AUTH-105'));
    const generation = this.generation;
    const response = await this.call('/auth/otp/verify', { ...this.otp, code });
    if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
    if (response.status !== 200) {
      if (response.data.session) this.otp.session = response.data.session;
      throw this.error(response.data);
    }
    const tokens = this.validTokens(response.data);
    const me = await this.call('/auth/me', undefined, tokens.accessToken);
    if (me.status !== 200 || !me.data.userId || !me.data.emailVerified) throw this.error(me.data);
    await this.persist({ ...tokens, expiresAt: Date.now() + tokens.expiresIn * 1000, userId: me.data.userId, email: me.data.email }, generation);
    this.otp = undefined;
    return this.publicState();
  }
  private validTokens(value: any): Tokens {
    if (!value?.accessToken || !value.refreshToken || !Number.isFinite(value.expiresIn) || value.expiresIn <= 0) throw new Error(problem('MI-AUTH-104'));
    return { accessToken: value.accessToken, refreshToken: value.refreshToken, expiresIn: value.expiresIn };
  }
  async access(force = false) {
    await this.initialize();
    if (!this.session) throw new Error(problem('MI-AUTH-105'));
    if (!force && this.session.expiresAt > Date.now() + 120_000) return this.session.accessToken;
    if (!this.refreshFlight) {
      const generation = this.generation;
      const old = this.session;
      const flight = (async () => {
        const response = await this.call('/auth/refresh', { refreshToken: old.refreshToken });
        if (response.status === 401) {
          if (generation === this.generation) await this.clear();
          throw new Error(problem('MI-AUTH-105'));
        }
        if (response.status !== 200) throw this.error(response.data);
        const tokens = this.validTokens(response.data);
        await this.persist({ ...old, ...tokens, expiresAt: Date.now() + tokens.expiresIn * 1000 }, generation);
        return tokens.accessToken;
      })();
      this.refreshFlight = flight;
      void flight.finally(() => { if (this.refreshFlight === flight) this.refreshFlight = undefined; }).catch(() => undefined);
    }
    return this.refreshFlight;
  }
  async authorized(operation: (access: string) => Promise<{ status: number; data: any }>) {
    const generation = this.generation;
    let access = await this.access();
    let result = await operation(access);
    if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
    if (result.status === 401 && result.data?.detail?.support_code === 'MI-AUTH-105') {
      // A 401 from the app auth dependency precedes the operation. Provider
      // reconnect responses and uncertain writes are never retried here.
      access = await this.access(true);
      result = await operation(access);
      if (generation !== this.generation) throw new Error(problem('MI-AUTH-105'));
      if (result.status === 401 && result.data?.detail?.support_code === 'MI-AUTH-105') await this.clear();
    }
    return result;
  }
  async clear() {
    this.generation++;
    this.session = undefined;
    this.otp = undefined;
    await this.queue(() => this.storage.clear());
    await this.onSignedOut();
  }
  async logout() {
    await this.initialize();
    // Wait for an already-running rotation so revocation uses its replacement.
    // A hung network call is bounded by the caller's timeout.
    if (this.session) await this.access().catch(() => undefined);
    if (this.refreshFlight) await this.refreshFlight.catch(() => undefined);
    const old = this.session;
    await this.clear();
    if (!old) return;
    const result = await this.call('/auth/logout', { refreshToken: old.refreshToken, accessToken: old.accessToken });
    if (result.status !== 200) throw new Error(problem('MI-AUTH-110'));
  }
}
