import AVFoundation
import CoreAudio
import Foundation

// MARK: - AudioCaptureManager
//
// Captures mic + system audio simultaneously, resamples each to 16 kHz mono
// Int16, buffers both streams through per-source ring buffers, and lets a
// MixerDrainer saturating-add them into a single output WAV.
//
// System audio uses a CoreAudio Process Tap wrapped in a private aggregate
// device (AudioDeviceCreateIOProcIDWithBlock rejects a bare tap AudioObjectID
// with OSStatus `'!dev'`). This replaces the old ScreenCaptureKit path, which
// silently zero-padded its buffers whenever the source used Voice Processing
// IO (Zoom / FaceTime / Google Meet native).

final class AudioCaptureManager: @unchecked Sendable {

    private let mixedWriter: WAVWriter

    // Canonical output format for both rings — the drainer expects this on both sides.
    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16000,
        channels: 1,
        interleaved: true
    )!

    // 30 s of headroom at 16 kHz per source.
    private static let ringCapacity = 30 * 16000
    private let micRing = RingBuffer(capacity: ringCapacity)
    private let systemRing = RingBuffer(capacity: ringCapacity)
    private var drainer: MixerDrainer?

    // Mic path
    private var micEngine: AVAudioEngine?
    private var micConverter: AVAudioConverter?
    private var micConfigObserver: NSObjectProtocol?

    // Process Tap path
    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var aggregateID: AudioObjectID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID?
    private var tapSrcFormat: AVAudioFormat?
    private var tapConverter: AVAudioConverter?
    private let ioProcQueue = DispatchQueue(
        label: "com.meetingnotes.tap.ioproc",
        qos: .userInteractive
    )

    // Diagnostics
    private var micFrames: Int = 0
    private var tapFrames: Int = 0
    private var tapCallbacks: Int = 0

    init(mixedWriter: WAVWriter) {
        self.mixedWriter = mixedWriter
    }

    // MARK: - Lifecycle

    func start() throws {
        try startMicCapture()
        try startSystemAudioCapture()
        let drainer = MixerDrainer(mic: micRing, system: systemRing, writer: mixedWriter)
        drainer.start()
        self.drainer = drainer
    }

    func stop() {
        // Drain first — flushes anything still in the rings into the WAV.
        drainer?.stop()
        drainer = nil

        if let obs = micConfigObserver {
            NotificationCenter.default.removeObserver(obs)
            micConfigObserver = nil
        }
        micEngine?.stop()
        micEngine = nil
        micConverter = nil

        if let procID = ioProcID, aggregateID != kAudioObjectUnknown {
            _ = AudioDeviceStop(aggregateID, procID)
            _ = AudioDeviceDestroyIOProcID(aggregateID, procID)
        }
        ioProcID = nil

        if aggregateID != kAudioObjectUnknown {
            _ = AudioHardwareDestroyAggregateDevice(aggregateID)
            aggregateID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            _ = AudioHardwareDestroyProcessTap(tapID)
            tapID = kAudioObjectUnknown
        }

        fputs(
            "CaptureAudio: mic=\(micFrames) frames, tap=\(tapFrames) frames / \(tapCallbacks) callbacks\n",
            stderr
        )
        let micOverflows = micRing.overflowCount
        let sysOverflows = systemRing.overflowCount
        if micOverflows > 0 || sysOverflows > 0 {
            fputs(
                "CaptureAudio: ring overflows — mic=\(micOverflows) sys=\(sysOverflows)\n",
                stderr
            )
        }
    }

    // MARK: - Mic capture

    private func startMicCapture() throws {
        // Called on initial start and again from the AVAudioEngineConfiguration
        // Change handler. AVAudioEngine stops and fails to call the tap block
        // when the default input device changes (AirPods connect, HDMI
        // hot-plug, etc) — without the observer the WAV freezes because the
        // MixerDrainer blocks on min(mic, sys) with mic permanently empty.
        let engine = AVAudioEngine()
        let inputNode = engine.inputNode
        let hwFormat = inputNode.outputFormat(forBus: 0)

        guard let converter = AVAudioConverter(from: hwFormat, to: outputFormat) else {
            throw CaptureError.converterCreationFailed
        }

        inputNode.installTap(
            onBus: 0, bufferSize: 4096, format: hwFormat
        ) { [weak self] buffer, _ in
            self?.handleMicBuffer(buffer, converter: converter)
        }
        try engine.start()

        micEngine = engine
        micConverter = converter
        micConfigObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange,
            object: engine,
            queue: nil
        ) { [weak self] _ in
            self?.handleMicConfigChange()
        }
    }

    private func handleMicConfigChange() {
        // Apple stops the engine before posting this notification. Tearing
        // down fully and rebuilding is more reliable than restarting in place
        // when the hardware format actually changed (different sample rate or
        // channel count on the new input device).
        fputs(
            "CaptureAudio: mic engine config changed (device hot-swap?) — reconfiguring\n",
            stderr
        )
        if let obs = micConfigObserver {
            NotificationCenter.default.removeObserver(obs)
            micConfigObserver = nil
        }
        micEngine?.stop()
        micEngine?.inputNode.removeTap(onBus: 0)
        micEngine = nil
        micConverter = nil

        do {
            try startMicCapture()
            fputs("CaptureAudio: mic engine restarted after config change\n", stderr)
        } catch {
            fputs(
                "CaptureAudio: FAILED to reconfigure mic after device change: \(error)\n",
                stderr
            )
        }
    }

    private func handleMicBuffer(_ buffer: AVAudioPCMBuffer, converter: AVAudioConverter) {
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let outCapacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1
        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat, frameCapacity: outCapacity
        ) else { return }

        nonisolated(unsafe) var consumed = false
        nonisolated(unsafe) let inputBuffer = buffer
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if consumed { outStatus.pointee = .noDataNow; return nil }
            consumed = true
            outStatus.pointee = .haveData
            return inputBuffer
        }

        var error: NSError?
        let status = converter.convert(to: outBuffer, error: &error, withInputFrom: inputBlock)
        guard status != .error, outBuffer.frameLength > 0,
              let channelData = outBuffer.int16ChannelData else { return }

        micRing.write(channelData[0], count: Int(outBuffer.frameLength))
        micFrames += Int(outBuffer.frameLength)
    }

    // MARK: - Process Tap (system audio)

    private func startSystemAudioCapture() throws {
        let selfPID = ProcessInfo.processInfo.processIdentifier
        guard let selfObjID = pidToAudioObjectID(selfPID) else {
            throw CaptureError.tapSetupFailed("could not translate self pid to AudioObjectID")
        }

        // Create the tap, excluding our own process.
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [selfObjID])
        desc.isPrivate = true
        desc.name = "MeetingNotes tap"

        var tap: AudioObjectID = kAudioObjectUnknown
        let tapErr = AudioHardwareCreateProcessTap(desc, &tap)
        guard tapErr == noErr, tap != kAudioObjectUnknown else {
            throw CaptureError.tapSetupFailed(
                "AudioHardwareCreateProcessTap failed \(fmtStatus(tapErr))"
            )
        }
        tapID = tap

        // Read the tap's native format to set up the cached converter.
        guard let fmt = readTapFormat(tap) else {
            throw CaptureError.tapSetupFailed("could not read kAudioTapPropertyFormat")
        }

        let srcIsFloat = (fmt.mFormatFlags & kAudioFormatFlagIsFloat) != 0
        let srcIsNonInterleaved = (fmt.mFormatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        let srcChannels = Int(fmt.mChannelsPerFrame)
        guard srcIsFloat else {
            throw CaptureError.tapSetupFailed("tap format is non-float (unsupported)")
        }

        guard let srcFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: fmt.mSampleRate,
            channels: AVAudioChannelCount(srcChannels),
            interleaved: !srcIsNonInterleaved
        ) else {
            throw CaptureError.tapSetupFailed("could not build AVAudioFormat for tap")
        }
        tapSrcFormat = srcFormat

        guard let converter = AVAudioConverter(from: srcFormat, to: outputFormat) else {
            throw CaptureError.tapSetupFailed("could not create tap converter")
        }
        tapConverter = converter

        // Wrap the tap in a private aggregate device — direct IOProc on a
        // bare tap AudioObjectID returns OSStatus `'!dev'`.
        guard let tapUID = readTapUID(tap) else {
            throw CaptureError.tapSetupFailed("could not read tap UID")
        }

        let aggUID = "com.meetingnotes.capture.agg.\(selfPID)"
        let aggDict: [String: Any] = [
            kAudioAggregateDeviceNameKey as String: "MeetingNotes capture agg",
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

        var agg: AudioObjectID = kAudioObjectUnknown
        let aggErr = AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &agg)
        guard aggErr == noErr, agg != kAudioObjectUnknown else {
            throw CaptureError.tapSetupFailed(
                "AudioHardwareCreateAggregateDevice failed \(fmtStatus(aggErr))"
            )
        }
        aggregateID = agg

        var procID: AudioDeviceIOProcID?
        let procErr = AudioDeviceCreateIOProcIDWithBlock(
            &procID, agg, ioProcQueue
        ) { [weak self] _, input, _, _, _ in
            self?.handleTapBuffer(input)
        }
        guard procErr == noErr, let p = procID else {
            throw CaptureError.tapSetupFailed(
                "AudioDeviceCreateIOProcIDWithBlock failed \(fmtStatus(procErr))"
            )
        }
        ioProcID = p

        let startErr = AudioDeviceStart(agg, p)
        guard startErr == noErr else {
            throw CaptureError.tapSetupFailed(
                "AudioDeviceStart failed \(fmtStatus(startErr))"
            )
        }

        fputs(
            "CaptureAudio: Process Tap started "
                + "(\(fmt.mSampleRate) Hz, \(srcChannels) ch, "
                + "\(srcIsNonInterleaved ? "planar" : "interleaved") float32)\n",
            stderr
        )
    }

    private func handleTapBuffer(_ input: UnsafePointer<AudioBufferList>) {
        tapCallbacks += 1
        guard let srcFormat = tapSrcFormat, let converter = tapConverter else { return }

        let abl = UnsafeMutableAudioBufferListPointer(
            UnsafeMutablePointer(mutating: input)
        )
        guard let firstBuf = abl.first else { return }

        // Derive frame count from the first plane. All planes share a frame count
        // in non-interleaved layouts; for interleaved there's only one plane.
        let frames: Int
        if srcFormat.isInterleaved {
            let bytesPerFrame = Int(srcFormat.streamDescription.pointee.mBytesPerFrame)
            frames = bytesPerFrame > 0 ? Int(firstBuf.mDataByteSize) / bytesPerFrame : 0
        } else {
            frames = Int(firstBuf.mDataByteSize) / MemoryLayout<Float>.size
        }
        guard frames > 0 else { return }

        guard let srcBuffer = AVAudioPCMBuffer(
            pcmFormat: srcFormat, frameCapacity: AVAudioFrameCount(frames)
        ) else { return }
        srcBuffer.frameLength = AVAudioFrameCount(frames)

        // Copy each plane (or the single interleaved blob) from the IOProc's
        // AudioBufferList into the AVAudioPCMBuffer's matching slot.
        let dstList = UnsafeMutableAudioBufferListPointer(srcBuffer.mutableAudioBufferList)
        let planes = min(abl.count, dstList.count)
        for i in 0..<planes {
            let src = abl[i]
            let dst = dstList[i]
            guard let sp = src.mData, let dp = dst.mData else { continue }
            memcpy(dp, sp, min(Int(src.mDataByteSize), Int(dst.mDataByteSize)))
        }

        let ratio = outputFormat.sampleRate / srcFormat.sampleRate
        let outCapacity = AVAudioFrameCount(ceil(Double(frames) * ratio)) + 1
        guard let outBuffer = AVAudioPCMBuffer(
            pcmFormat: outputFormat, frameCapacity: outCapacity
        ) else { return }

        nonisolated(unsafe) var consumed = false
        nonisolated(unsafe) let inB = srcBuffer
        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if consumed { outStatus.pointee = .noDataNow; return nil }
            consumed = true
            outStatus.pointee = .haveData
            return inB
        }

        var error: NSError?
        let status = converter.convert(to: outBuffer, error: &error, withInputFrom: inputBlock)
        guard status != .error, outBuffer.frameLength > 0,
              let channelData = outBuffer.int16ChannelData else { return }

        systemRing.write(channelData[0], count: Int(outBuffer.frameLength))
        tapFrames += Int(outBuffer.frameLength)
    }
}

// MARK: - CoreAudio helpers

/// Translate a POSIX pid to its CoreAudio AudioObjectID.
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

func readTapFormat(_ tapID: AudioObjectID) -> AudioStreamBasicDescription? {
    var fmt = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    let err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &fmt)
    return err == noErr ? fmt : nil
}

func readTapUID(_ tapID: AudioObjectID) -> String? {
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

// MARK: - Errors

enum CaptureError: Error, CustomStringConvertible {
    case converterCreationFailed
    case tapSetupFailed(String)

    var description: String {
        switch self {
        case .converterCreationFailed: return "Failed to create audio converter"
        case .tapSetupFailed(let reason): return "Process Tap setup failed: \(reason)"
        }
    }
}
