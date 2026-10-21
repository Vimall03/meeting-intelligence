import { problem } from './user-messages';

export type PcmFrame = { start: number; end: number; data: Blob };

/** Keeps only audio that has not been durably transcribed. Cursors count samples. */
export class PcmReplay {
  readonly maxSamples = 90 * 16000;
  frames: PcmFrame[] = [];
  end = 0;
  ack = 0;

  append(data: Blob) {
    if (!data.size || data.size % 2 || data.size > 6400 || this.end - this.ack + data.size / 2 > this.maxSamples) {
      throw new Error(problem('MI-CON-005'));
    }
    const start = this.end;
    this.end += data.size / 2;
    this.frames.push({ start, end: this.end, data });
  }

  acknowledge(cursor: unknown): cursor is number {
    if (!Number.isSafeInteger(cursor) || (cursor as number) < this.ack || (cursor as number) > this.end) return false;
    this.ack = cursor as number;
    this.frames = this.frames.filter(frame => frame.end > this.ack).map(frame => frame.start < this.ack
      ? { start: this.ack, end: frame.end, data: frame.data.slice((this.ack - frame.start) * 2) } : frame);
    return true;
  }
}
