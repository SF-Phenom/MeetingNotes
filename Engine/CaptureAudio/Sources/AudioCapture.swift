import AVFoundation
import CoreAudio
import Foundation
import ScreenCaptureKit

// MARK: - AudioCaptureManager

final class AudioCaptureManager: NSObject, @unchecked Sendable {

    private let micWriter: WAVWriter
    private let systemWriter: WAVWriter
    private var micEngine: AVAudioEngine?
    private var scStream: SCStream?
    private var streamDelegate: SystemAudioDelegate?

    // Output format: 16kHz mono 16-bit PCM
    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16000,
        channels: 1,
        interleaved: true
    )!

    // Separate write locks — mic and system write to different files, no need
    // to serialise across streams.
    private let micWriteLock = NSLock()
    private let systemWriteLock = NSLock()

    // Track whether system audio is delivering data (for diagnostics)
    private var systemAudioFrameCount: Int = 0
    private var systemAudioFormatLogged: Bool = false
    private var systemAudioCallbackCount: Int = 0

    // Pre-allocated AudioBufferList storage reused on every system-audio
    // callback, sized for up to `systemAudioMaxChannels` planes. Keeps the
    // SCK realtime audio thread free of `UnsafeMutableRawPointer.allocate()`
    // (which can block inside malloc under memory pressure and cause
    // dropouts). The SCK sample-handler queue is serial, so there is at
    // most one reader of this buffer at a time.
    private static let systemAudioMaxChannels = 8
    private let systemAudioBufferListSize: Int
    private let systemAudioBufferListPtr: UnsafeMutableRawPointer

    init(micWriter: WAVWriter, systemWriter: WAVWriter) {
        self.micWriter = micWriter
        self.systemWriter = systemWriter
        let ablSize = MemoryLayout<AudioBufferList>.size
            + (Self.systemAudioMaxChannels - 1) * MemoryLayout<AudioBuffer>.size
        self.systemAudioBufferListSize = ablSize
        self.systemAudioBufferListPtr = UnsafeMutableRawPointer.allocate(
            byteCount: ablSize,
            alignment: MemoryLayout<AudioBufferList>.alignment
        )
        super.init()
    }

    deinit {
        systemAudioBufferListPtr.deallocate()
    }

    // MARK: - Public API

    func start() throws {
        try startMicCapture()
        startSystemAudioCapture()
    }

    func stop() {
        micEngine?.stop()
        micEngine = nil

        if let stream = scStream {
            // SCStream.stop is async; fire and forget during shutdown
            let semaphore = DispatchSemaphore(value: 0)
            stream.stopCapture { error in
                if let error {
                    fputs("Warning: SCStream stop error: \(error)\n", stderr)
                }
                semaphore.signal()
            }
            _ = semaphore.wait(timeout: .now() + 3)
            scStream = nil
        }

        fputs("CaptureAudio: system audio delivered \(systemAudioFrameCount) frames in \(systemAudioCallbackCount) callbacks\n", stderr)
    }

    // MARK: - System Audio via ScreenCaptureKit

    private func startSystemAudioCapture() {
        // ScreenCaptureKit setup is async — run on a background queue
        // but block briefly so we know if it worked before returning
        let semaphore = DispatchSemaphore(value: 0)
        var captureError: Error?

        Task {
            do {
                try await self._setupScreenCaptureKit()
            } catch {
                captureError = error
            }
            semaphore.signal()
        }

        // Wait up to 5 seconds for setup
        let result = semaphore.wait(timeout: .now() + 5)
        if result == .timedOut {
            fputs("Warning: ScreenCaptureKit setup timed out\n", stderr)
        } else if let err = captureError {
            fputs("Warning: System audio capture unavailable: \(err)\n", stderr)
        }
    }

    @available(macOS 13.0, *)
    private func _setupScreenCaptureKit() async throws {
        // 1. Get shareable content (displays, apps, windows)
        let content = try await SCShareableContent.excludingDesktopWindows(
            false, onScreenWindowsOnly: false
        )

        guard let display = content.displays.first else {
            throw CaptureError.noDisplay
        }

        // 2. Exclude our own process from the capture
        let selfBundleID = Bundle.main.bundleIdentifier ?? ""
        let selfPID = ProcessInfo.processInfo.processIdentifier
        let excludedApps = content.applications.filter { app in
            app.bundleIdentifier == selfBundleID || app.processID == selfPID
        }

        // 3. Create a content filter — capture the whole display minus our app
        let filter = SCContentFilter(
            display: display,
            excludingApplications: excludedApps,
            exceptingWindows: []
        )

        // 4. Configure the stream for audio only
        let config = SCStreamConfiguration()
        config.capturesAudio = true
        config.excludesCurrentProcessAudio = true
        // Capture at the system's native 48 kHz stereo.  Requesting a
        // non-native rate (e.g. 16 kHz) forces ScreenCaptureKit's internal
        // resampler, which can silently drop audio from apps that use
        // Voice Processing IO (Zoom, FaceTime, etc.).  We downsample to
        // 16 kHz mono ourselves via AVAudioConverter.
        config.sampleRate = 48000
        config.channelCount = 2

        // We don't need video — set minimal video parameters
        config.width = 2
        config.height = 2
        config.minimumFrameInterval = CMTime(value: 1, timescale: 1) // 1 fps minimum

        // 5. Create the stream with our audio delegate
        let delegate = SystemAudioDelegate(manager: self)
        self.streamDelegate = delegate

        let stream = SCStream(filter: filter, configuration: config, delegate: nil)
        self.scStream = stream

        // Add output for audio samples
        try stream.addStreamOutput(
            delegate,
            type: .audio,
            sampleHandlerQueue: DispatchQueue(label: "com.meetingnotes.systemaudio", qos: .userInteractive)
        )

        // 6. Start capturing
        try await stream.startCapture()
        fputs("CaptureAudio: ScreenCaptureKit system audio capture started\n", stderr)
    }

    // Called by SystemAudioDelegate when audio samples arrive
    fileprivate func handleSystemAudioSample(_ sampleBuffer: CMSampleBuffer) {
        guard let formatDesc = sampleBuffer.formatDescription,
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }

        let srcSampleRate = asbd.pointee.mSampleRate
        let srcChannels = Int(asbd.pointee.mChannelsPerFrame)
        let srcBitsPerChannel = asbd.pointee.mBitsPerChannel
        let flags = asbd.pointee.mFormatFlags
        let srcIsFloat = (flags & kAudioFormatFlagIsFloat) != 0
        let srcIsNonInterleaved = (flags & kAudioFormatFlagIsNonInterleaved) != 0

        // Log the audio format once so we can diagnose capture issues
        if !systemAudioFormatLogged {
            fputs("CaptureAudio: system audio format: \(srcSampleRate) Hz, \(srcChannels) ch, "
                  + "\(srcBitsPerChannel)-bit \(srcIsFloat ? "float" : "int"), "
                  + "\(srcIsNonInterleaved ? "planar" : "interleaved")\n", stderr)
            systemAudioFormatLogged = true
        }
        systemAudioCallbackCount += 1

        let frameCount = AVAudioFrameCount(sampleBuffer.numSamples)
        guard frameCount > 0 else { return }

        // Build an AVAudioFormat that matches the actual layout (planar vs
        // interleaved). Copying planar data into a buffer declared as
        // interleaved produces garbled audio — the bug that caused the
        // "chipmunk" system-audio output.
        guard let srcFormat = AVAudioFormat(
            commonFormat: srcIsFloat ? .pcmFormatFloat32 : .pcmFormatInt16,
            sampleRate: srcSampleRate,
            channels: AVAudioChannelCount(srcChannels),
            interleaved: !srcIsNonInterleaved
        ) else { return }

        guard let converter = AVAudioConverter(from: srcFormat, to: outputFormat) else { return }
        guard let srcBuffer = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: frameCount) else {
            return
        }
        srcBuffer.frameLength = frameCount

        // Pull the AudioBufferList from the CMSampleBuffer. For non-interleaved
        // PCM the list has `srcChannels` buffers (one per channel). For
        // interleaved it has a single buffer.
        //
        // Uses the pre-allocated `systemAudioBufferListPtr` so this hot audio
        // callback never calls malloc/allocate. Bail out if a buffer with
        // more channels than we sized for ever arrives (should never happen
        // with stereo system audio).
        if srcChannels > Self.systemAudioMaxChannels {
            fputs(
                "CaptureAudio: system audio arrived with \(srcChannels) channels, "
                + "exceeds pre-allocated max of \(Self.systemAudioMaxChannels) — dropping buffer\n",
                stderr
            )
            return
        }
        let ablSize = MemoryLayout<AudioBufferList>.size
            + max(0, srcChannels - 1) * MemoryLayout<AudioBuffer>.size
        let ablTyped = systemAudioBufferListPtr.assumingMemoryBound(to: AudioBufferList.self)

        var blockBufferOut: CMBlockBuffer?
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: ablTyped,
            bufferListSize: ablSize,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: kCMSampleBufferFlag_AudioBufferList_Assure16ByteAlignment,
            blockBufferOut: &blockBufferOut
        )
        guard status == noErr else { return }

        let ablPointer = UnsafeMutableAudioBufferListPointer(ablTyped)
        let dstList = UnsafeMutableAudioBufferListPointer(srcBuffer.mutableAudioBufferList)

        // Copy each buffer (plane or interleaved blob) into the matching slot.
        let planeCount = min(ablPointer.count, dstList.count)
        for i in 0..<planeCount {
            let src = ablPointer[i]
            let dst = dstList[i]
            guard let srcData = src.mData, let dstData = dst.mData else { continue }
            let bytes = min(Int(src.mDataByteSize), Int(dst.mDataByteSize))
            memcpy(dstData, srcData, bytes)
        }

        convertAndWrite(buffer: srcBuffer, converter: converter,
                        writer: systemWriter, lock: systemWriteLock)
        systemAudioFrameCount += Int(frameCount)
    }

    // MARK: - Microphone Capture

    private func startMicCapture() throws {
        let engine = AVAudioEngine()
        micEngine = engine

        let inputNode = engine.inputNode
        let hwFormat = inputNode.outputFormat(forBus: 0)

        guard let converter = AVAudioConverter(from: hwFormat, to: outputFormat) else {
            throw CaptureError.converterCreationFailed
        }

        inputNode.installTap(onBus: 0, bufferSize: 4096, format: hwFormat) { [weak self] buffer, _ in
            guard let self else { return }
            self.convertAndWrite(buffer: buffer, converter: converter,
                                 writer: self.micWriter, lock: self.micWriteLock)
        }

        try engine.start()
    }

    // MARK: - Conversion and Writing

    private func convertAndWrite(buffer: AVAudioPCMBuffer,
                                 converter: AVAudioConverter,
                                 writer: WAVWriter,
                                 lock: NSLock) {
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let outFrameCapacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1

        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat,
                                                   frameCapacity: outFrameCapacity) else { return }

        nonisolated(unsafe) var consumed = false
        nonisolated(unsafe) let inputBuffer = buffer

        let inputBlock: AVAudioConverterInputBlock = { _, outStatus in
            if consumed {
                outStatus.pointee = .noDataNow
                return nil
            }
            consumed = true
            outStatus.pointee = .haveData
            return inputBuffer
        }

        var error: NSError?
        let status = converter.convert(to: outputBuffer, error: &error, withInputFrom: inputBlock)

        guard status != .error, outputBuffer.frameLength > 0 else { return }

        guard let channelData = outputBuffer.int16ChannelData else { return }
        let data = Data(bytes: channelData[0], count: Int(outputBuffer.frameLength) * 2)

        lock.lock()
        writer.write(data)
        lock.unlock()
    }
}

// MARK: - ScreenCaptureKit Audio Delegate

private class SystemAudioDelegate: NSObject, SCStreamOutput {
    private weak var manager: AudioCaptureManager?

    init(manager: AudioCaptureManager) {
        self.manager = manager
        super.init()
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sampleBuffer: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio else { return }
        manager?.handleSystemAudioSample(sampleBuffer)
    }
}

// MARK: - Errors

enum CaptureError: Error, CustomStringConvertible {
    case noDisplay
    case converterCreationFailed

    var description: String {
        switch self {
        case .noDisplay: return "No display found for ScreenCaptureKit"
        case .converterCreationFailed: return "Failed to create audio converter"
        }
    }
}
