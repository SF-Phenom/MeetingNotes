import AVFoundation
@preconcurrency import CoreAudio
import AudioToolbox
import Foundation

// MARK: - AudioCaptureManager

final class AudioCaptureManager: @unchecked Sendable {

    private let wavWriter: WAVWriter
    private var systemEngine: AVAudioEngine?
    private var micEngine: AVAudioEngine?

    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var aggregateDeviceID: AudioObjectID = kAudioObjectUnknown

    // Output format: 16kHz mono 16-bit PCM
    private let outputFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16000,
        channels: 1,
        interleaved: true
    )!

    // Serialises writes from both tap callbacks
    private let writeLock = NSLock()

    init(wavWriter: WAVWriter) {
        self.wavWriter = wavWriter
    }

    // MARK: - Public API

    func start() throws {
        try startMicCapture()
        if #available(macOS 14.2, *) {
            do {
                try startSystemAudioCapture()
            } catch {
                fputs("Warning: System audio capture unavailable: \(error)\n", stderr)
            }
        } else {
            fputs("Warning: System audio capture requires macOS 14.2+\n", stderr)
        }
    }

    func stop() {
        systemEngine?.stop()
        micEngine?.stop()
        teardownSystemAudio()
    }

    // MARK: - System Audio via CATapDescription + Aggregate Device

    @available(macOS 14.2, *)
    private func startSystemAudioCapture() throws {
        // 1. Create a global mono tap (captures all processes)
        let tapDesc = CATapDescription()
        tapDesc.isMono = true
        tapDesc.isExclusive = true   // exclude ourselves

        var tapIDOut: AudioObjectID = kAudioObjectUnknown
        let tapStatus = AudioHardwareCreateProcessTap(tapDesc, &tapIDOut)
        guard tapStatus == kAudioHardwareNoError else {
            throw CaptureError.tapCreationFailed(tapStatus)
        }
        tapID = tapIDOut

        // 2. Get the tap's UID string (needed for the aggregate device description)
        let tapUID = try getStringProperty(
            objectID: tapID,
            selector: kAudioTapPropertyUID,
            scope: kAudioObjectPropertyScopeGlobal
        )

        // 3. Create an aggregate device that includes the tap
        let aggUID = UUID().uuidString
        let aggDesc: [String: Any] = [
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceNameKey: "CaptureAudioAgg",
            kAudioAggregateDeviceIsPrivateKey: 1,
            kAudioAggregateDeviceTapListKey: [
                [kAudioSubTapUIDKey: tapUID]
            ],
            kAudioAggregateDeviceTapAutoStartKey: 1
        ]

        var aggDeviceIDOut: AudioObjectID = kAudioObjectUnknown
        let aggStatus = AudioHardwareCreateAggregateDevice(aggDesc as CFDictionary, &aggDeviceIDOut)
        guard aggStatus == kAudioHardwareNoError else {
            AudioHardwareDestroyProcessTap(tapID)
            tapID = kAudioObjectUnknown
            throw CaptureError.aggregateDeviceCreationFailed(aggStatus)
        }
        aggregateDeviceID = aggDeviceIDOut

        // 4. Stand up AVAudioEngine pointed at the aggregate device
        let engine = AVAudioEngine()
        systemEngine = engine
        try setEngineInputDevice(engine: engine, deviceID: aggregateDeviceID)

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

    private func teardownSystemAudio() {
        if aggregateDeviceID != kAudioObjectUnknown {
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = kAudioObjectUnknown
        }
        if tapID != kAudioObjectUnknown {
            if #available(macOS 14.2, *) {
                AudioHardwareDestroyProcessTap(tapID)
            }
            tapID = kAudioObjectUnknown
        }
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

    // MARK: - AudioUnit device routing

    private func setEngineInputDevice(engine: AVAudioEngine, deviceID: AudioDeviceID) throws {
        guard let auhal = engine.inputNode.audioUnit else {
            throw CaptureError.noAudioUnit
        }
        var devID = deviceID
        let result = AudioUnitSetProperty(
            auhal,
            kAudioOutputUnitProperty_CurrentDevice,
            kAudioUnitScope_Global,
            0,
            &devID,
            UInt32(MemoryLayout<AudioDeviceID>.size)
        )
        guard result == noErr else {
            throw CaptureError.audioUnitPropertyFailed(result)
        }
    }

    // MARK: - Conversion and Writing

    private func convertAndWrite(buffer: AVAudioPCMBuffer, converter: AVAudioConverter) {
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let outFrameCapacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * ratio)) + 1

        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: outputFormat,
                                                   frameCapacity: outFrameCapacity) else { return }

        // The converter input block is called once per conversion call.
        // We use nonisolated(unsafe) to satisfy Swift 6 sendability rules
        // for the local `consumed` flag captured across the @Sendable boundary.
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

    // MARK: - CoreAudio Helpers

    private func getStringProperty(
        objectID: AudioObjectID,
        selector: AudioObjectPropertySelector,
        scope: AudioObjectPropertyScope
    ) throws -> String {
        var address = AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: scope,
            mElement: kAudioObjectPropertyElementMain
        )
        // AudioObjectGetPropertyData returns a retained CFString for string properties.
        // We receive it via an Unmanaged<CFString> to avoid the incorrect UnsafeMutableRawPointer warning.
        var unmanagedStr: Unmanaged<CFString>? = nil
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(objectID, &address, 0, nil, &size, &unmanagedStr)
        guard status == kAudioHardwareNoError, let unmanaged = unmanagedStr else {
            throw CaptureError.propertyReadFailed(status)
        }
        return unmanaged.takeRetainedValue() as String
    }
}

// MARK: - Errors

enum CaptureError: Error {
    case tapCreationFailed(OSStatus)
    case aggregateDeviceCreationFailed(OSStatus)
    case converterCreationFailed
    case noAudioUnit
    case audioUnitPropertyFailed(OSStatus)
    case propertyReadFailed(OSStatus)
}
