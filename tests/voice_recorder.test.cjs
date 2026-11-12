const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const sourcePath = require('node:path').join(__dirname, '../attendance/web/static/voice-recorder.js');

function buffer(seconds = 4, channels = 1, rate = 8000) {
  return {sampleRate: rate, length: seconds * rate, numberOfChannels: channels,
    getChannelData: channel => new Float32Array(seconds * rate).fill(channel ? -.25 : .5)};
}

function setup({denied = false, pending = false, decoded = buffer()} = {}) {
  let stopped = 0, closed = 0, revoked = 0, grant;
  const instances = [];
  const track = {stop() {stopped++;}, onended: null};
  const stream = {getTracks: () => [track]};
  class MediaRecorder {
    constructor() {this.state = 'inactive'; this.mimeType = 'audio/webm'; instances.push(this);}
    start() {this.state = 'recording';}
    stop() {
      this.state = 'inactive';
      this.ondataavailable?.({data: new Blob(['encoded audio'])});
      queueMicrotask(() => this.onstop?.());
    }
  }
  class AudioContext {
    constructor() {this.state = 'running';}
    async decodeAudioData() {return decoded;}
    async close() {this.state = 'closed'; closed++;}
  }
  const sandbox = {window: {isSecureContext: true, MediaRecorder, AudioContext},
    navigator: {mediaDevices: {getUserMedia: () => pending ? new Promise(resolve => {grant = resolve;}) :
      denied ? Promise.reject(Object.assign(new Error('denied'), {name: 'NotAllowedError'})) : Promise.resolve(stream)}},
    URL: {createObjectURL: () => 'blob:recording', revokeObjectURL: () => revoked++},
    Blob, performance, setTimeout, clearTimeout, setInterval, clearInterval};
  vm.runInNewContext(fs.readFileSync(sourcePath, 'utf8'), sandbox);
  const recorder = new sandbox.window.VoiceRecorder(() => {});
  return {recorder, Recorder: sandbox.window.VoiceRecorder, sandbox, track, instances,
    grant: () => grant(stream), counts: () => ({stopped, closed, revoked})};
}

async function finished(recorder) {
  for (let i = 0; i < 30 && recorder.busy; i++) await new Promise(r => setTimeout(r, 5));
  assert.equal(recorder.busy, false);
}

test('PCM WAV has correct header, downmix and samples; output is capped at 30 seconds', async () => {
  const {Recorder} = setup();
  const blob = Recorder.encodeWav(buffer(31, 2));
  const bytes = await blob.arrayBuffer(), view = new DataView(bytes);
  assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), 'RIFF');
  assert.equal(new TextDecoder().decode(bytes.slice(8, 12)), 'WAVE');
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 8000);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(view.getUint32(40, true), 30 * 8000 * 2);
  assert.equal(view.getInt16(44, true), Math.trunc(.125 * 32767));
  assert.equal(bytes.byteLength, 44 + 30 * 8000 * 2);
  assert.throws(() => Recorder.encodeWav(buffer(2)), /3/);
});

test('stop creates replayable WAV and releases the microphone; clear releases the object URL', async () => {
  const {recorder, counts} = setup();
  await recorder.start('chosen-mic');
  assert.equal(recorder.state, 'recording');
  assert.equal(recorder.blob, null);
  recorder.stop();
  await finished(recorder);
  assert.equal(recorder.state, 'ready');
  assert.equal(recorder.blob.type, 'audio/wav');
  assert.equal(counts().stopped, 1);
  assert.equal(counts().closed, 1);
  recorder.clear();
  assert.equal(recorder.blob, null);
  assert.equal(counts().revoked, 1);
});

test('permission denial produces a useful error and no recording', async () => {
  const {recorder} = setup({denied: true});
  await recorder.start();
  assert.equal(recorder.state, 'error');
  assert.match(recorder.message, /permission/i);
  assert.equal(recorder.blob, null);
});

test('a late permission result after leaving the page stops its microphone', async () => {
  const {recorder, grant, counts} = setup({pending: true});
  const start = recorder.start();
  recorder.destroy();
  grant();
  await start;
  assert.equal(counts().stopped, 1);
  assert.equal(recorder.blob, null);
  assert.notEqual(recorder.state, 'recording');
});

test('short recordings fail clearly, close audio context and release tracks', async () => {
  const {recorder, counts} = setup({decoded: buffer(2)});
  await recorder.start();
  recorder.stop();
  await finished(recorder);
  assert.equal(recorder.state, 'error');
  assert.match(recorder.message, /3/);
  assert.equal(counts().stopped, 1);
  assert.equal(counts().closed, 1);
});

test('page exit during recording releases the microphone without creating a WAV', async () => {
  const {recorder, counts} = setup();
  await recorder.start();
  recorder.destroy();
  await new Promise(r => setTimeout(r, 5));
  assert.equal(counts().stopped, 1);
  assert.equal(recorder.blob, null);
});

test('late errors from a discarded recorder cannot stop a new recording', async () => {
  const {recorder, instances} = setup();
  await recorder.start();
  const previous = instances[0];
  recorder.clear();
  await recorder.start();
  previous.onerror({error:new Error('stale error')});
  try {assert.equal(recorder.state, 'recording');}
  finally {recorder.destroy();}
});

test('insecure origins fail without requesting microphone access', async () => {
  const {recorder, sandbox} = setup();
  sandbox.window.isSecureContext = false;
  sandbox.navigator.mediaDevices.getUserMedia = () => {throw new Error('Must not ask');};
  await recorder.start();
  assert.equal(recorder.state, 'error');
  assert.match(recorder.message, /HTTPS/);
});
