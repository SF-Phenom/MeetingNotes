// Phase 4A spike: prove CoreAudio Process Tap can capture Zoom / VP-IO audio.
//
// Usage:
//   tap-spike [--duration SECONDS] [--output PATH]
// Default: 10 seconds, /tmp/tap_spike.wav
//
// Creates a CATapDescription that captures all system audio EXCEPT our own
// process, attaches a direct IOProc, writes the captured audio to a mono
// Int16 WAV, and reports peak amplitude + dBFS at the end. The peak level is
// the load-bearing diagnostic: ScreenCaptureKit's VP-IO bug gives us nonzero
// frames of digital silence, so frame count alone isn't enough.

import Foundation
import CoreAudio
import Darwin

// MARK: - CLI

var duration: Double = 10
var startDelay: Double = 0
var outputPath = "/tmp/tap_spike.wav"

do {
    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--duration":
            i += 1
            if i < args.count, let d = Double(args[i]) { duration = d }
        case "--delay":
            i += 1
            if i < args.count, let d = Double(args[i]) { startDelay = d }
        case "--output":
            i += 1
            if i < args.count { outputPath = args[i] }
        case "-h", "--help":
            fputs("Usage: tap-spike [--duration SECONDS] [--delay SECONDS] [--output PATH]\n", stderr)
            exit(0)
        default:
            fputs("Unknown argument: \(args[i])\n", stderr)
            exit(1)
        }
        i += 1
    }
}

guard #available(macOS 14.2, *) else {
    fputs("tap-spike requires macOS 14.2+\n", stderr)
    exit(1)
}

// MARK: - AudioObjectID translation

func pidToAudioObjectID(_ pid: pid_t) -> AudioObjectID? {
    var pidVar = pid
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var objID: AudioObjectID = kAudioObjectUnknown
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let err = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject),
        &addr,
        UInt32(MemoryLayout<pid_t>.size),
        &pidVar,
        &size,
        &objID
    )
    guard err == noErr, objID != kAudioObjectUnknown else { return nil }
    return objID
}

func getTapFormat(_ tapID: AudioObjectID) -> AudioStreamBasicDescription? {
    var fmt = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &fmt)
    guard err == noErr else { return nil }
    return fmt
}

func getTapUID(_ tapID: AudioObjectID) -> String? {
    var uid: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<CFString?>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyUID,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &uid)
    guard err == noErr, let u = uid?.takeRetainedValue() else { return nil }
    return u as String
}

/// Format an OSStatus as `<dec> (0x<hex>) '<fourcc>'` when the bytes are printable.
func fmtStatus(_ s: OSStatus) -> String {
    let v = UInt32(bitPattern: Int32(s))
    let bytes: [UInt8] = [
        UInt8((v >> 24) & 0xff),
        UInt8((v >> 16) & 0xff),
        UInt8((v >>  8) & 0xff),
        UInt8( v        & 0xff),
    ]
    let hex = String(format: "0x%08x", v)
    if bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7f }),
       let fourcc = String(bytes: bytes, encoding: .ascii) {
        return "\(s) (\(hex)) '\(fourcc)'"
    }
    return "\(s) (\(hex))"
}

// MARK: - Minimal Int16 WAV writer

final class SimpleWAVWriter: @unchecked Sendable {
    private let fh: FileHandle
    private var dataByteCount: UInt32 = 0
    let sampleRate: UInt32
    let channels: UInt16

    init(path: String, sampleRate: UInt32, channels: UInt16) throws {
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let fh = FileHandle(forWritingAtPath: path) else {
            throw NSError(domain: "tap-spike", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "cannot open \(path)"])
        }
        self.fh = fh
        self.sampleRate = sampleRate
        self.channels = channels
        writeHeader()
    }

    private func writeHeader() {
        var h = Data(capacity: 44)
        let bitsPerSample: UInt16 = 16
        let byteRate = sampleRate * UInt32(channels) * UInt32(bitsPerSample) / 8
        let blockAlign = channels * (bitsPerSample / 8)

        h.append(contentsOf: Array("RIFF".utf8))
        appendLE(&h, UInt32(0))
        h.append(contentsOf: Array("WAVE".utf8))
        h.append(contentsOf: Array("fmt ".utf8))
        appendLE(&h, UInt32(16))
        appendLE(&h, UInt16(1))          // PCM
        appendLE(&h, channels)
        appendLE(&h, sampleRate)
        appendLE(&h, byteRate)
        appendLE(&h, blockAlign)
        appendLE(&h, bitsPerSample)
        h.append(contentsOf: Array("data".utf8))
        appendLE(&h, UInt32(0))
        fh.write(h)
    }

    func write(_ samples: UnsafePointer<Int16>, count: Int) {
        let bytes = count * MemoryLayout<Int16>.size
        fh.write(Data(bytes: samples, count: bytes))
        dataByteCount += UInt32(bytes)
    }

    func finalize() {
        try? fh.seek(toOffset: 40)
        var d = dataByteCount
        fh.write(Data(bytes: &d, count: 4))
        try? fh.seek(toOffset: 4)
        var r = 36 + dataByteCount
        fh.write(Data(bytes: &r, count: 4))
        fh.closeFile()
    }
}

func appendLE<T: FixedWidthInteger>(_ data: inout Data, _ v: T) {
    var x = v.littleEndian
    withUnsafeBytes(of: &x) { data.append(contentsOf: $0) }
}

// MARK: - Shared IOProc state

final class SpikeState: @unchecked Sendable {
    var totalFrames: Int = 0
    var callbackCount: Int = 0
    var peakMagnitude: Float = 0
    let lock = NSLock()
    let writer: SimpleWAVWriter
    let srcChannels: Int
    let srcIsFloat: Bool
    let srcIsNonInterleaved: Bool

    init(writer: SimpleWAVWriter, srcChannels: Int,
         srcIsFloat: Bool, srcIsNonInterleaved: Bool) {
        self.writer = writer
        self.srcChannels = srcChannels
        self.srcIsFloat = srcIsFloat
        self.srcIsNonInterleaved = srcIsNonInterleaved
    }

    func handle(_ inputData: UnsafePointer<AudioBufferList>) {
        lock.lock()
        defer { lock.unlock() }
        callbackCount += 1

        let abl = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: inputData)
        )
        guard abl.count > 0 else { return }

        // Tap output is typically Float32. Handle both planar and interleaved.
        if srcIsFloat && srcIsNonInterleaved && abl.count >= srcChannels {
            let frames = Int(abl[0].mDataByteSize) / MemoryLayout<Float>.size
            guard frames > 0 else { return }
            var out = [Int16](repeating: 0, count: frames)
            for c in 0..<srcChannels {
                guard let ptr = abl[c].mData?.assumingMemoryBound(to: Float.self) else { continue }
                for f in 0..<frames {
                    let s = ptr[f]
                    let mag = abs(s)
                    if mag > peakMagnitude { peakMagnitude = mag }
                    let add = Int32(s * 32767.0) / Int32(srcChannels)
                    out[f] = Int16(clamping: Int32(out[f]) + add)
                }
            }
            totalFrames += frames
            out.withUnsafeBufferPointer { writer.write($0.baseAddress!, count: frames) }
        } else if srcIsFloat, let buf = abl.first {
            let totalSamples = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
            let frames = srcChannels > 0 ? totalSamples / srcChannels : 0
            guard frames > 0, let ptr = buf.mData?.assumingMemoryBound(to: Float.self) else { return }
            var out = [Int16](repeating: 0, count: frames)
            for f in 0..<frames {
                var sum: Float = 0
                for c in 0..<srcChannels {
                    let s = ptr[f * srcChannels + c]
                    sum += s
                    let mag = abs(s)
                    if mag > peakMagnitude { peakMagnitude = mag }
                }
                let avg = sum / Float(srcChannels)
                out[f] = Int16(clamping: Int32(avg * 32767.0))
            }
            totalFrames += frames
            out.withUnsafeBufferPointer { writer.write($0.baseAddress!, count: frames) }
        } else {
            // Int16 or other — for a spike we just count and move on
        }
    }
}

// MARK: - Setup

let selfPID = ProcessInfo.processInfo.processIdentifier
guard let selfObjID = pidToAudioObjectID(selfPID) else {
    fputs("Failed to translate own pid (\(selfPID)) to AudioObjectID\n", stderr)
    exit(1)
}
fputs("tap-spike: self pid=\(selfPID) audioObjectID=\(selfObjID)\n", stderr)

let tapDesc = CATapDescription(
    stereoGlobalTapButExcludeProcesses: [selfObjID]
)
tapDesc.isPrivate = true
tapDesc.name = "MeetingNotes tap-spike"

var tapID: AudioObjectID = kAudioObjectUnknown
let createTapErr = AudioHardwareCreateProcessTap(tapDesc, &tapID)
guard createTapErr == noErr, tapID != kAudioObjectUnknown else {
    fputs("AudioHardwareCreateProcessTap failed: OSStatus=\(fmtStatus(createTapErr))\n", stderr)
    fputs("  (may indicate missing Audio Capture permission — check System Settings > Privacy)\n", stderr)
    exit(1)
}
fputs("tap-spike: created process tap id=\(tapID)\n", stderr)

guard let fmt = getTapFormat(tapID) else {
    fputs("Failed to read tap format\n", stderr)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}

guard let tapUID = getTapUID(tapID) else {
    fputs("Failed to read tap UID\n", stderr)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}
fputs("tap-spike: tap UID=\(tapUID)\n", stderr)

let srcChannels = Int(fmt.mChannelsPerFrame)
let srcIsFloat = (fmt.mFormatFlags & kAudioFormatFlagIsFloat) != 0
let srcIsNonInterleaved = (fmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0

fputs("""
tap-spike: tap format
  sampleRate: \(fmt.mSampleRate) Hz
  channels: \(fmt.mChannelsPerFrame)
  bitsPerChannel: \(fmt.mBitsPerChannel)
  formatID: \(String(format: "0x%08x", fmt.mFormatID))
  flags: \(String(format: "0x%08x", fmt.mFormatFlags)) (float=\(srcIsFloat) planar=\(srcIsNonInterleaved))
  bytesPerFrame: \(fmt.mBytesPerFrame)
  framesPerPacket: \(fmt.mFramesPerPacket)

""", stderr)

let sampleRate = UInt32(fmt.mSampleRate)
let writer: SimpleWAVWriter
do {
    writer = try SimpleWAVWriter(path: outputPath, sampleRate: sampleRate, channels: 1)
} catch {
    fputs("Failed to open output WAV: \(error.localizedDescription)\n", stderr)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}

let state = SpikeState(
    writer: writer,
    srcChannels: srcChannels,
    srcIsFloat: srcIsFloat,
    srcIsNonInterleaved: srcIsNonInterleaved
)

// MARK: - Aggregate device (wraps the tap so we can IOProc on it)
//
// AudioDeviceCreateIOProcIDWithBlock rejects a bare tap AudioObjectID with
// OSStatus '!dev'. The supported pattern is to create a private aggregate
// device containing the tap as a sub-tap, then IOProc on the aggregate.

let aggUID = "com.meetingnotes.tapspike.agg.\(selfPID)"
let aggDict: [String: Any] = [
    kAudioAggregateDeviceNameKey as String: "tap-spike-agg",
    kAudioAggregateDeviceUIDKey as String: aggUID,
    kAudioAggregateDeviceIsPrivateKey as String: 1,
    kAudioAggregateDeviceIsStackedKey as String: 0,
    kAudioAggregateDeviceTapListKey as String: [
        [
            kAudioSubTapUIDKey as String: tapUID,
            kAudioSubTapDriftCompensationKey as String: 1,
        ]
    ],
    kAudioAggregateDeviceSubDeviceListKey as String: [],
]

var aggID: AudioObjectID = kAudioObjectUnknown
let aggErr = AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID)
guard aggErr == noErr, aggID != kAudioObjectUnknown else {
    fputs("AudioHardwareCreateAggregateDevice failed: OSStatus=\(fmtStatus(aggErr))\n", stderr)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}
fputs("tap-spike: created aggregate device id=\(aggID) uid=\(aggUID)\n", stderr)

// MARK: - IOProc on the aggregate device

var ioProcID: AudioDeviceIOProcID?
let ioQueue = DispatchQueue(label: "com.meetingnotes.tapspike.ioproc", qos: .userInteractive)
let createProcErr = AudioDeviceCreateIOProcIDWithBlock(
    &ioProcID,
    aggID,
    ioQueue
) { _, input, _, _, _ in
    state.handle(input)
}

guard createProcErr == noErr, let procID = ioProcID else {
    fputs("AudioDeviceCreateIOProcIDWithBlock failed: OSStatus=\(fmtStatus(createProcErr))\n", stderr)
    _ = AudioHardwareDestroyAggregateDevice(aggID)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}

if startDelay > 0 {
    fputs("tap-spike: waiting \(startDelay)s before recording starts...\n", stderr)
    Thread.sleep(forTimeInterval: startDelay)
}

let startErr = AudioDeviceStart(aggID, procID)
guard startErr == noErr else {
    fputs("AudioDeviceStart failed: OSStatus=\(fmtStatus(startErr))\n", stderr)
    _ = AudioDeviceDestroyIOProcID(aggID, procID)
    _ = AudioHardwareDestroyAggregateDevice(aggID)
    _ = AudioHardwareDestroyProcessTap(tapID)
    exit(1)
}

fputs("tap-spike: recording for \(duration)s to \(outputPath)\n", stderr)
fputs("tap-spike: play audio now (YouTube, Zoom, etc.)\n", stderr)

// MARK: - Run + teardown

Thread.sleep(forTimeInterval: duration)

_ = AudioDeviceStop(aggID, procID)
_ = AudioDeviceDestroyIOProcID(aggID, procID)
_ = AudioHardwareDestroyAggregateDevice(aggID)
_ = AudioHardwareDestroyProcessTap(tapID)

writer.finalize()

let peakDBFS: Double = state.peakMagnitude > 0
    ? 20.0 * log10(Double(state.peakMagnitude))
    : -.infinity
let verdict: String
if state.callbackCount == 0 {
    verdict = "NO CALLBACKS — tap never fired. Check permissions / API support."
} else if state.totalFrames == 0 {
    verdict = "callbacks fired but zero frames captured."
} else if state.peakMagnitude == 0 {
    verdict = "DIGITAL SILENCE — frames captured but all zero-amplitude (VP-IO bypass still in play?)."
} else if peakDBFS < -60 {
    verdict = "very quiet (\(String(format: "%.1f", peakDBFS)) dBFS) — source may have been silent during capture."
} else {
    verdict = "OK — real audio captured (peak \(String(format: "%.1f", peakDBFS)) dBFS)."
}

fputs("""
tap-spike: done
  callbacks: \(state.callbackCount)
  frames: \(state.totalFrames)
  peak magnitude: \(state.peakMagnitude)
  peak dBFS: \(String(format: "%.2f", peakDBFS))
  verdict: \(verdict)
  output: \(outputPath)
""", stderr)
