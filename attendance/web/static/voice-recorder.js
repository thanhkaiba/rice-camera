/* Browser capture stays local until Create voice sends the resulting WAV to this app. */
(() => {
  class VoiceRecorder {
    constructor(onChange) {
      this.onChange = onChange;
      this.state = 'idle';
      this.message = 'Read one sample naturally. Aim for 10–20 seconds.';
      this.seconds = 0;
      this.blob = this.url = null;
      this._generation = 0;
    }

    get busy() { return ['asking', 'recording', 'processing'].includes(this.state); }

    static encodeWav(audio) {
      const rate = audio.sampleRate;
      const frames = Math.min(audio.length, 30 * rate);
      if (!Number.isInteger(rate) || rate < 8000 || rate > 192000 || frames / rate < 3 || !audio.numberOfChannels)
        throw new Error('Record at least 3 seconds; aim for 10–20 seconds.');
      const bytes = new ArrayBuffer(44 + frames * 2), view = new DataView(bytes);
      const text = (offset, value) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)); };
      text(0, 'RIFF'); view.setUint32(4, bytes.byteLength - 8, true); text(8, 'WAVE');
      text(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
      view.setUint16(22, 1, true); view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true);
      view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data');
      view.setUint32(40, frames * 2, true);
      const channels = Array.from({length: audio.numberOfChannels}, (_, i) => audio.getChannelData(i));
      for (let i = 0; i < frames; i++) {
        let sample = 0;
        for (const channel of channels) sample += channel[i] / channels.length;
        if (!Number.isFinite(sample)) throw new Error('The recording contains invalid audio. Please record again.');
        sample = Math.max(-1, Math.min(1, sample));
        view.setInt16(44 + i * 2, sample * (sample < 0 ? 32768 : 32767), true);
      }
      return new Blob([bytes], {type: 'audio/wav'});
    }

    async start(deviceId = '') {
      if (this.busy) return;
      const generation = ++this._generation;
      this.state = 'asking'; this.message = 'Allow microphone access in your browser…'; this.onChange(this);
      try {
        if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia)
          throw new Error('Microphone recording requires HTTPS or http://localhost. You can still choose a WAV file below.');
        if (!window.MediaRecorder || !(window.AudioContext || window.webkitAudioContext))
          throw new Error('This browser cannot record here. Use a current Chrome, Edge, Firefox or Safari, or choose a WAV file.');
        const audio = {channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false};
        if (deviceId) audio.deviceId = {exact: deviceId};
        const stream = await navigator.mediaDevices.getUserMedia({audio});
        if (generation !== this._generation) { stream.getTracks().forEach(track => track.stop()); return; }
        this._stream = stream;
        const recorder = this._recorder = new window.MediaRecorder(stream), chunks = [];
        recorder.ondataavailable = event => { if (generation === this._generation && event.data.size) chunks.push(event.data); };
        recorder.onstop = () => this._finish(chunks, generation);
        recorder.onerror = event => {
          if (generation === this._generation) this._fail(event.error || new Error('Microphone recording failed.'));
        };
        stream.getTracks().forEach(track => { track.onended = () => this.stop(); });
        recorder.start();
        this._clearBlob();
        this.state = 'recording'; this.seconds = 0; this.message = 'Recording… Read one passage, then press Stop.';
        const started = performance.now();
        this._clock = setInterval(() => { this.seconds = Math.min(30, (performance.now() - started) / 1000); this.onChange(this); }, 250);
        this._limit = setTimeout(() => this.stop(), 30000);
        this.onChange(this);
      } catch (error) { if (generation === this._generation) this._fail(error); }
    }

    stop() {
      if (this.state !== 'recording') return;
      this.state = 'processing'; this.message = 'Preparing the recording for listening…'; this.onChange(this);
      try { if (this._recorder.state === 'recording') this._recorder.stop(); }
      catch (error) { this._fail(error); }
      this._release();
    }

    async _finish(chunks, generation) {
      if (generation !== this._generation) return;
      this._release();
      this._recorder = null;
      this.state = 'processing'; this.message = 'Preparing the recording for listening…'; this.onChange(this);
      let context;
      try {
        context = this._context = new (window.AudioContext || window.webkitAudioContext)();
        const decoded = await context.decodeAudioData(await new Blob(chunks).arrayBuffer());
        if (generation !== this._generation) return;
        this.blob = VoiceRecorder.encodeWav(decoded);
        this.url = URL.createObjectURL(this.blob);
        this.seconds = Math.min(30, decoded.length / decoded.sampleRate);
        this.state = 'ready'; this.message = 'Recording ready. Listen, record again if needed, then press Create voice.';
        this.onChange(this);
      } catch (error) { if (generation === this._generation) this._fail(error); }
      finally {
        if (context && context.state !== 'closed') await context.close().catch(() => {});
        if (this._context === context) this._context = null;
      }
    }

    _release() {
      clearInterval(this._clock); clearTimeout(this._limit);
      if (this._stream) {
        this._stream.getTracks().forEach(track => { track.onended = null; track.stop(); });
        this._stream = null;
      }
    }

    _fail(error) {
      ++this._generation;
      try { if (this._recorder?.state === 'recording') this._recorder.stop(); } catch {}
      this._release();
      const messages = {NotAllowedError: 'Microphone permission was denied. Allow it in the browser and try again.',
        NotFoundError: 'No microphone was found. Connect one and try again.',
        NotReadableError: 'The microphone could not be opened. Check whether another app is using it.'};
      this.state = 'error'; this.message = messages[error.name] || error.message || 'Recording failed. Please try again.';
      if (this.blob) this.message += ' Your previous recording is still available.';
      this.onChange(this);
    }

    _clearBlob() {
      if (this.url) URL.revokeObjectURL(this.url);
      this.blob = this.url = null;
    }

    clear() {
      ++this._generation;
      try { if (this._recorder?.state === 'recording') this._recorder.stop(); } catch {}
      this._recorder = null;
      this._release(); this._clearBlob();
      if (this._context && this._context.state !== 'closed') this._context.close().catch(() => {});
      this.state = 'idle'; this.seconds = 0; this.message = 'Read one sample naturally. Aim for 10–20 seconds.';
      this.onChange(this);
    }

    destroy() { this.clear(); }
  }
  window.VoiceRecorder = VoiceRecorder;
})();
