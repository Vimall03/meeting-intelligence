import workletUrl from './pcm-worklet.js?url&no-inline';
import { problem } from './user-messages';

export class PcmCapture {
  private node: AudioWorkletNode;
  private source: MediaStreamAudioSourceNode;
  private sink: (data: Blob) => void;
  private finishing?: Promise<void>;

  private constructor(context: AudioContext, stream: MediaStream, sink: (data: Blob) => void, onError: () => void) {
    this.sink = sink;
    this.source = context.createMediaStreamSource(stream);
    this.node = new AudioWorkletNode(context, 'meeting-pcm', { channelCount: 1, channelCountMode: 'explicit' });
    this.node.port.onmessage = ({ data }) => { if (data.pcm instanceof ArrayBuffer) this.sink(new Blob([data.pcm])); };
    this.node.onprocessorerror = onError;
    this.source.connect(this.node);
    this.node.connect(context.destination);
  }

  static async start(context: AudioContext, stream: MediaStream, sink: (data: Blob) => void, onError: () => void) {
    if (context.sampleRate !== 16000 || !context.audioWorklet) throw new Error(problem('MI-CAP-008'));
    await context.audioWorklet.addModule(workletUrl);
    return new PcmCapture(context, stream, sink, onError);
  }

  finish(sink: (data: Blob) => void): Promise<void> {
    if (this.finishing) return this.finishing;
    this.sink = sink;
    this.finishing = new Promise((resolve, reject) => {
      const cleanup = () => { clearTimeout(timeout); this.source.disconnect(); this.node.disconnect(); this.node.port.close(); };
      const previous = this.node.port.onmessage;
      const timeout = setTimeout(() => { cleanup(); reject(new Error(problem('MI-STT-001'))); }, 1000);
      this.node.port.onmessage = event => {
        if (event.data.finished) { cleanup(); resolve(); }
        else previous?.call(this.node.port, event);
      };
      this.node.port.postMessage('finish');
    });
    return this.finishing;
  }
}
