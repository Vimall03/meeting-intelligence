/* Bundled locally: converts the existing mixed audio into 200 ms PCM frames. */
class MeetingPcmProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(3200);
    this.used = 0;
    this.closed = false;
    this.port.onmessage = ({ data }) => {
      if (data === 'finish') {
        this.flush();
        this.closed = true;
        this.port.postMessage({ finished: true });
      }
    };
  }
  flush() {
    if (!this.used) return;
    const frame = this.samples.slice(0, this.used).buffer;
    this.port.postMessage({ pcm: frame }, [frame]);
    this.used = 0;
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    if (this.closed) return false;
    const input = inputs[0]?.[0];
    if (!input) return true;
    for (const value of input) {
      const sample = Math.max(-1, Math.min(1, value));
      this.samples[this.used++] = Math.round(sample * (sample < 0 ? 32768 : 32767));
      if (this.used === this.samples.length) this.flush();
    }
    return true;
  }
}
registerProcessor('meeting-pcm', MeetingPcmProcessor);
