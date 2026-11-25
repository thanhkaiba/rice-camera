// FaceAttendance Mic: records the office computer's microphone for a few seconds into a WAV file, so the app can
// tell whether the door speaker really made a sound. Built once on the office computer (see listen.py) and kept
// in its own small app, so macOS asks for the microphone once and remembers the answer across app updates.
//
//   FaceAttendanceMic <out.wav> <seconds>
//
// Next to out.wav it writes out.wav.status: "asking", "recording <unix time>", "done", "denied", "failed", and
// out.wav.started: the unix time the recording started.
import AVFoundation
import Foundation

let args = CommandLine.arguments
guard args.count >= 3, let seconds = Double(args[2]), seconds > 0, seconds <= 120 else {
    FileHandle.standardError.write("usage: FaceAttendanceMic out.wav seconds\n".data(using: .utf8)!)
    exit(2)
}
let out = URL(fileURLWithPath: args[1])
let statusPath = out.path + ".status"

func tell(_ text: String) {
    try? text.write(toFile: statusPath, atomically: true, encoding: .utf8)
}

var recorder: AVAudioRecorder?

func record() {
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM, AVSampleRateKey: 16000.0, AVNumberOfChannelsKey: 1,
        AVLinearPCMBitDepthKey: 16, AVLinearPCMIsFloatKey: false, AVLinearPCMIsBigEndianKey: false,
    ]
    guard let r = try? AVAudioRecorder(url: out, settings: settings), r.record(forDuration: seconds) else {
        tell("failed")
        exit(4)
    }
    recorder = r
    let began = String(format: "%.3f", Date().timeIntervalSince1970)
    try? began.write(toFile: out.path + ".started", atomically: true, encoding: .utf8)
    tell("recording " + began)
    DispatchQueue.main.asyncAfter(deadline: .now() + seconds + 0.3) {
        recorder?.stop()
        tell("done")
        exit(0)
    }
}

switch AVCaptureDevice.authorizationStatus(for: .audio) {
case .authorized:
    record()
case .notDetermined:
    tell("asking")
    AVCaptureDevice.requestAccess(for: .audio) { granted in
        DispatchQueue.main.async {
            if granted { record() } else { tell("denied"); exit(3) }
        }
    }
default:
    tell("denied")
    exit(3)
}
DispatchQueue.main.asyncAfter(deadline: .now() + 300) { tell("failed"); exit(5) }
dispatchMain()
