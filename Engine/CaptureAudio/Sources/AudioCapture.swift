import AVFoundation
import CoreAudio
import Foundation
import ScreenCaptureKit

// MARK: - AudioCaptureManager

final class AudioCaptureManager: NSObject, @unchecked Sendable {

    private let wavWriter: WAVWriter
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

    // Serialises writes from both tap callbacks
    private let writeLock = NSLock()

    // Track whether system audio is delivering data (for diagnostics)
    private var systemAudioFrameCount: Int = 0

    init(wavWriter: WAVWriter) {
        self.wavWriter = wavWriter
        super.init()
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

        fputs("CaptureAudio: system audio delivered \(systemAudioFrameCount) frames total\n", stderr)
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
        config.sampleRate = 16000          // match our WAV format
        config.channelCount = 1            // mono

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

        // Get the raw audio data
        guard let blockBuffer = sampleBuffer.dataBuffer else { return }

        let length = CMBlockBufferGetDataLength(blockBuffer)
        guard length > 0 else { return }

        var dataPointer: UnsafeMutablePointer<Int8>?
        var lengthOut: Int = 0
        let status = CMBlockBufferGetDataPointer(
            blockBuffer, atOffset: 0, lengthAtOffsetOut: nil,
            totalLengthOut: &lengthOut, dataPointerOut: &dataPointer
        )
        guard status == kCMBlockBufferNoErr, let ptr = dataPointer, lengthOut > 0 else {
            return
        }

        let rawData = Data(bytes: ptr, count: lengthOut)

        // The SCStream is configured for 16kHz mono, but the actual format
        // may differ. Convert if needed.
        let srcSampleRate = asbd.pointee.mSampleRate
        let srcChannels = asbd.pointee.mChannelsPerFrame
        let srcBitsPerChannel = asbd.pointee.mBitsPerChannel
        let srcIsFloat = (asbd.pointee.mFormatFlags & kAudioFormatFlagIsFloat) != 0

        // If format already matches our output (16kHz, mono, 16-bit int), write directly
        if Int(srcSampleRate) == 16000 && srcChannels == 1 &&
           srcBitsPerChannel == 16 && !srcIsFloat {
            writeLock.lock()
            wavWriter.write(rawData)
            writeLock.unlock()
            systemAudioFrameCount += lengthOut / 2
            return
        }

        // Otherwise, use AVAudioConverter for format conversion
        guard let srcFormat = AVAudioFormat(
            commonFormat: srcIsFloat ? .pcmFormatFloat32 : .pcmFormatInt16,
            sampleRate: srcSampleRate,
            channels: AVAudioChannelCount(srcChannels),
            interleaved: true
        ) else { return }

        guard let converter = AVAudioConverter(from: srcFormat, to: outputFormat) else { return }

        let frameCount = UInt32(lengthOut) / srcFormat.streamDescription.pointee.mBytesPerFrame
        guard frameCount > 0 else { return }

        guard let srcBuffer = AVAudioPCMBuffer(pcmFormat: srcFormat, frameCapacity: frameCount) else {
            return
        }
        srcBuffer.frameLength = frameCount

        // Copy raw data into the source buffer
        if srcIsFloat, let floatData = srcBuffer.floatChannelData {
            rawData.withUnsafeBytes { rawBuf in
                if let baseAddr = rawBuf.baseAddress {
                    memcpy(floatData[0], baseAddr, lengthOut)
                }
            }
        } else if let int16Data = srcBuffer.int16ChannelData {
            rawData.withUnsafeBytes { rawBuf in
                if let baseAddr = rawBuf.baseAddress {
                    memcpy(int16Data[0], baseAddr, lengthOut)
                }
            }
        } else {
            return
        }

        convertAndWrite(buffer: srcBuffer, converter: converter)
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
            self.convertAndWrite(buffer: buffer, converter: converter)
        }

        try engine.start()
    }

    // MARK: - Conversion and Writing

    private func convertAndWrite(buffer: AVAudioPCMBuffer, converter: AVAudioConverter) {
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

        writeLock.lock()
        wavWriter.write(data)
        writeLock.unlock()
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
