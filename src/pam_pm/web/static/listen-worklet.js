/* Pam Listen worklet: resample mic audio to 24 kHz mono PCM16. */
class ListenCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this._ratio = sampleRate / 24000;
    this._pos = 0;
    this._carry = new Float32Array(0);
  }

  _resample(input) {
    if (!input || input.length === 0) return new Float32Array(0);
    const data = input.length > 1 ? this._mix(input) : input[0];
    const joined = this._join(this._carry, data);
    const outLen = Math.floor((joined.length - this._pos) / this._ratio);
    if (outLen <= 0) {
      this._carry = joined;
      return new Float32Array(0);
    }
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i += 1) {
      const idx = this._pos + i * this._ratio;
      const lo = Math.floor(idx);
      const hi = Math.min(lo + 1, joined.length - 1);
      const frac = idx - lo;
      out[i] = joined[lo] * (1 - frac) + joined[hi] * frac;
    }
    this._pos += outLen * this._ratio;
    const drop = Math.floor(this._pos);
    this._carry = joined.slice(drop);
    this._pos -= drop;
    return out;
  }

  _mix(channels) {
    const len = channels[0].length;
    const out = new Float32Array(len);
    for (let i = 0; i < len; i += 1) {
      let sum = 0;
      for (const ch of channels) sum += ch[i] || 0;
      out[i] = sum / channels.length;
    }
    return out;
  }

  _join(a, b) {
    if (a.length === 0) return b;
    const out = new Float32Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  }

  process(inputs) {
    const channel = inputs && inputs[0];
    if (!channel || channel.length === 0) return true;
    const mono = this._resample(channel);
    if (mono.length === 0) return true;
    const pcm = new Int16Array(mono.length);
    for (let i = 0; i < mono.length; i += 1) {
      const v = Math.max(-1, Math.min(1, mono[i]));
      pcm[i] = v < 0 ? Math.round(v * 32768) : Math.round(v * 32767);
    }
    this.port.postMessage(pcm.buffer, [pcm.buffer]);
    return true;
  }
}

registerProcessor("pam-listen-capture", ListenCapture);
