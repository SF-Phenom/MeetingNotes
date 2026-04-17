import Foundation
import Speech
@preconcurrency import AVFoundation
import Darwin

// MARK: - Argument Parsing

func usage() -> Never {
    fputs("""
    Usage: SpeechTranscribe --input /path/to/file.wav [--output /path/to/out.ndjson] [--watch] [--locale en_US]

    Transcribes a WAV file using Apple's on-device speech recognition.

    Options:
      --input    Path to a 16kHz mono 16-bit PCM WAV file (required)
      --output   Path to write NDJSON output (default: stdout)
      --watch    Poll for new audio (for transcribing a recording in progress)
      --locale   BCP 47 locale code (default: en_US)

    Output: NDJSON lines (to --output file or stdout):
      {"type": "partial", "text": "..."}
      {"type": "final", "text": "..."}

    Send SIGINT to finalize and exit cleanly.

    """, stderr)
    exit(1)
}

var inputPath: String?
var outputPath: String?
var watchMode = false
var localeId = "en_US"

var i = 1
let args = CommandLine.arguments
while i < args.count {
    switch args[i] {
    case "--input":
        i += 1
        guard i < args.count else { usage() }
        inputPath = args[i]
    case "--output":
        i += 1
        guard i < args.count else { usage() }
        outputPath = args[i]
    case "--watch":
        watchMode = true
    case "--locale":
        i += 1
        guard i < args.count else { usage() }
        localeId = args[i]
    default:
        fputs("Unknown argument: \(args[i])\n", stderr)
        usage()
    }
    i += 1
}

guard let wavPath = inputPath else {
    fputs("Error: --input is required\n", stderr)
    usage()
}

// If --output specified, open that file for NDJSON output; otherwise stdout
nonisolated(unsafe) var outputHandle: FileHandle = FileHandle.standardOutput
if let outPath = outputPath {
    FileManager.default.createFile(atPath: outPath, contents: nil)
    guard let handle = FileHandle(forWritingAtPath: outPath) else {
        fputs("Error: Cannot open output file \(outPath)\n", stderr)
        exit(1)
    }
    outputHandle = handle
}

// MARK: - JSON Output

func emitJSON(type: String, text: String) {
    let escaped = text
        .replacingOccurrences(of: "\\", with: "\\\\")
        .replacingOccurrences(of: "\"", with: "\\\"")
        .replacingOccurrences(of: "\n", with: "\\n")
        .replacingOccurrences(of: "\r", with: "\\r")
        .replacingOccurrences(of: "\t", with: "\\t")
    let line = "{\"type\":\"\(type)\",\"text\":\"\(escaped)\"}\n"
    if let data = line.data(using: .utf8) {
        outputHandle.write(data)
        try? outputHandle.synchronize()
    }
}

/// Log a message to both stderr and the output file (as NDJSON "log" type)
func log(_ message: String) {
    fputs("SpeechTranscribe: \(message)\n", stderr)
    emitJSON(type: "log", text: message)
}

// MARK: - Signal Handling

nonisolated(unsafe) var shutdownRequested = false

let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
signal(SIGINT, SIG_IGN)
sigintSource.setEventHandler {
    fputs("SpeechTranscribe: SIGINT received, finalizing...\n", stderr)
    shutdownRequested = true
}
sigintSource.resume()

let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
signal(SIGTERM, SIG_IGN)
sigtermSource.setEventHandler {
    fputs("SpeechTranscribe: SIGTERM received, finalizing...\n", stderr)
    shutdownRequested = true
}
sigtermSource.resume()

// MARK: - Audio Normalization

/// Normalize a 16-bit PCM WAV file to -3 dB peak.
/// Returns the URL of the normalized file, or nil if normalization isn't needed/fails.
func normalizeAudio(inputPath: String, outputURL: URL) -> URL? {
    guard let handle = FileHandle(forReadingAtPath: inputPath) else { return nil }
    defer { try? handle.close() }

    let headerData = handle.readData(ofLength: 44)
    guard headerData.count == 44 else { return nil }

    let allData = handle.readDataToEndOfFile()
    guard allData.count >= 2 else { return nil }

    // Find peak sample value
    let sampleCount = allData.count / 2
    var peak: Int16 = 0
    allData.withUnsafeBytes { raw in
        let samples = raw.bindMemory(to: Int16.self)
        for i in 0..<sampleCount {
            let abs = samples[i] == Int16.min ? Int16.max : Swift.abs(samples[i])
            if abs > peak { peak = abs }
        }
    }

    // If peak is already above -6 dB (16384), no normalization needed
    guard peak > 0, peak < 16384 else { return nil }

    // Target: -3 dB = 23197
    let gain = Float(23197) / Float(peak)
    log("normalizing: peak=\(peak) gain=\(String(format: "%.1f", gain))x")

    // Apply gain
    var normalized = Data(capacity: allData.count)
    allData.withUnsafeBytes { raw in
        let samples = raw.bindMemory(to: Int16.self)
        for i in 0..<sampleCount {
            let amplified = Int32(Float(samples[i]) * gain)
            let clamped = Int16(clamping: amplified)
            withUnsafeBytes(of: clamped.littleEndian) { normalized.append(contentsOf: $0) }
        }
    }

    // Update WAV header data size
    var header = headerData
    let dataSize = UInt32(normalized.count)
    let fileSize = dataSize + 36
    header.replaceSubrange(4..<8, with: withUnsafeBytes(of: fileSize.littleEndian) { Data($0) })
    header.replaceSubrange(40..<44, with: withUnsafeBytes(of: dataSize.littleEndian) { Data($0) })

    var output = header
    output.append(normalized)
    do {
        try output.write(to: outputURL)
        return outputURL
    } catch {
        return nil
    }
}

// MARK: - SFSpeechRecognizer Transcription (on-device)

/// Transcribe a complete WAV file using SFSpeechRecognizer with on-device processing.
func transcribeFile(path: String, locale: Locale) {
    guard let recognizer = SFSpeechRecognizer(locale: locale) else {
        log("Error: Speech recognizer not available for locale \(locale.identifier)")
        exit(1)
    }

    guard recognizer.isAvailable else {
        log("Error: Speech recognizer is not available (check locale support)")
        exit(1)
    }

    log("on-device available: \(recognizer.supportsOnDeviceRecognition)")
    log("authorizationStatus: \(SFSpeechRecognizer.authorizationStatus().rawValue)")

    // Normalize audio to ensure adequate levels for speech detection
    let normalizedURL: URL
    let tmpNormalized = URL(fileURLWithPath: NSTemporaryDirectory())
        .appendingPathComponent("speech_normalized.wav")
    if let nURL = normalizeAudio(inputPath: path, outputURL: tmpNormalized) {
        normalizedURL = nURL
        log("audio normalized to \(normalizedURL.path)")
    } else {
        normalizedURL = URL(fileURLWithPath: path)
        log("using original audio (normalization skipped)")
    }

    let request = SFSpeechURLRecognitionRequest(url: normalizedURL)
    request.requiresOnDeviceRecognition = true
    request.shouldReportPartialResults = true
    request.addsPunctuation = true

    log("starting recognition of \(path)...")

    recognizer.recognitionTask(with: request) { result, error in
        if let error = error {
            let nsError = error as NSError
            log("recognition error: \(error.localizedDescription) [domain=\(nsError.domain) code=\(nsError.code)]")
            // Still emit whatever we have
            if let result = result {
                let text = result.bestTranscription.formattedString
                emitJSON(type: "final", text: text)
            }
            emitJSON(type: "error", text: error.localizedDescription)
            exit(nsError.code == 0 ? 0 : 1)
        }

        guard let result = result else { return }

        let text = result.bestTranscription.formattedString

        if result.isFinal {
            emitJSON(type: "final", text: text)
            log("done (\(text.count) chars)")
            exit(0)
        } else {
            emitJSON(type: "partial", text: text)
        }
    }
}

/// Watch a growing WAV file and transcribe chunks as they appear.
func transcribeWatching(path: String, locale: Locale) {
    guard let recognizer = SFSpeechRecognizer(locale: locale) else {
        log("Error: Speech recognizer not available for locale \(locale.identifier)")
        exit(1)
    }

    log("on-device available: \(recognizer.supportsOnDeviceRecognition)")

    let headerSize = 44
    let bytesPerSec = 16000 * 2 * 1  // 16kHz, 16-bit, mono
    let chunkDurationSecs = 30
    let chunkBytes = chunkDurationSecs * bytesPerSec
    let pollInterval: TimeInterval = 3.0
    let minNewBytes = 2 * bytesPerSec  // 2 seconds minimum

    var byteOffset = 0
    var chunkTexts: [String] = []
    var currentChunkText = ""

    func fileDataSize() -> Int {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: path),
              let fileSize = attrs[.size] as? Int else { return 0 }
        return max(0, fileSize - headerSize)
    }

    func transcribeChunk(chunkData: Data, chunkIndex: Int) {
        // Write chunk to temp WAV file
        let tmpURL = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("speech_chunk_\(chunkIndex).wav")

        // Build WAV header
        let dataSize = UInt32(chunkData.count)
        let fileSize = dataSize + 36
        var header = Data()
        header.append(contentsOf: "RIFF".utf8)
        header.append(contentsOf: withUnsafeBytes(of: fileSize.littleEndian) { Array($0) })
        header.append(contentsOf: "WAVE".utf8)
        header.append(contentsOf: "fmt ".utf8)
        header.append(contentsOf: withUnsafeBytes(of: UInt32(16).littleEndian) { Array($0) })
        header.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) }) // PCM
        header.append(contentsOf: withUnsafeBytes(of: UInt16(1).littleEndian) { Array($0) }) // mono
        header.append(contentsOf: withUnsafeBytes(of: UInt32(16000).littleEndian) { Array($0) }) // sample rate
        header.append(contentsOf: withUnsafeBytes(of: UInt32(32000).littleEndian) { Array($0) }) // byte rate
        header.append(contentsOf: withUnsafeBytes(of: UInt16(2).littleEndian) { Array($0) }) // block align
        header.append(contentsOf: withUnsafeBytes(of: UInt16(16).littleEndian) { Array($0) }) // bits per sample
        header.append(contentsOf: "data".utf8)
        header.append(contentsOf: withUnsafeBytes(of: dataSize.littleEndian) { Array($0) })

        var wavData = header
        wavData.append(chunkData)
        try? wavData.write(to: tmpURL)

        // Normalize the chunk if needed
        let normURL = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("speech_chunk_\(chunkIndex)_norm.wav")
        let requestURL: URL
        if let nURL = normalizeAudio(inputPath: tmpURL.path, outputURL: normURL) {
            requestURL = nURL
        } else {
            requestURL = tmpURL
        }

        let request = SFSpeechURLRecognitionRequest(url: requestURL)
        request.requiresOnDeviceRecognition = true
        request.shouldReportPartialResults = true
        request.addsPunctuation = true

        let semaphore = DispatchSemaphore(value: 0)

        recognizer.recognitionTask(with: request) { result, error in
            if let error = error {
                log("chunk \(chunkIndex) error: \(error.localizedDescription)")
                semaphore.signal()
                return
            }
            guard let result = result else { return }
            let text = result.bestTranscription.formattedString
            if result.isFinal {
                currentChunkText = text
                // Emit combined text
                let allTexts = chunkTexts + [currentChunkText]
                let combined = allTexts.joined(separator: " ")
                emitJSON(type: "final", text: combined)
                try? FileManager.default.removeItem(at: tmpURL)
                semaphore.signal()
            } else {
                currentChunkText = text
                let allTexts = chunkTexts + [currentChunkText]
                let combined = allTexts.joined(separator: " ")
                emitJSON(type: "partial", text: combined)
            }
        }

        // Wait for this chunk to complete (with timeout)
        let waitResult = semaphore.wait(timeout: .now() + 120)
        if waitResult == .timedOut {
            log("chunk \(chunkIndex) timed out")
        }
    }

    log("watch mode, polling every \(pollInterval)s...")

    // Timer-based polling
    let timer = DispatchSource.makeTimerSource(queue: .global())
    timer.schedule(deadline: .now(), repeating: pollInterval)
    timer.setEventHandler {
        guard !shutdownRequested else {
            // Final read
            let totalData = fileDataSize()
            if totalData > byteOffset {
                guard let handle = FileHandle(forReadingAtPath: path) else { return }
                defer { try? handle.close() }
                handle.seek(toFileOffset: UInt64(headerSize + byteOffset))
                let data = handle.readData(ofLength: totalData - byteOffset)
                if !data.isEmpty {
                    let chunkIdx = chunkTexts.count
                    log("final chunk \(chunkIdx) (\(data.count) bytes)")
                    transcribeChunk(chunkData: data, chunkIndex: chunkIdx)
                    if !currentChunkText.isEmpty {
                        chunkTexts.append(currentChunkText)
                    }
                }
            }
            let finalText = chunkTexts.joined(separator: " ")
            emitJSON(type: "final", text: finalText)
            log("done (\(finalText.count) chars)")
            exit(0)
        }

        let totalData = fileDataSize()
        let newBytes = totalData - byteOffset

        // Check if we've accumulated enough for a new chunk
        if newBytes >= chunkBytes {
            guard let handle = FileHandle(forReadingAtPath: path) else { return }
            defer { try? handle.close() }
            handle.seek(toFileOffset: UInt64(headerSize + byteOffset))
            let data = handle.readData(ofLength: chunkBytes)

            let chunkIdx = chunkTexts.count
            log("transcribing chunk \(chunkIdx) (\(data.count) bytes, \(chunkDurationSecs)s)")
            transcribeChunk(chunkData: data, chunkIndex: chunkIdx)
            if !currentChunkText.isEmpty {
                chunkTexts.append(currentChunkText)
                currentChunkText = ""
            }
            byteOffset += data.count
        } else if newBytes >= minNewBytes {
            // Partial chunk — transcribe what we have for live feedback
            guard let handle = FileHandle(forReadingAtPath: path) else { return }
            defer { try? handle.close() }
            handle.seek(toFileOffset: UInt64(headerSize + byteOffset))
            let data = handle.readData(ofLength: newBytes)

            let chunkIdx = chunkTexts.count
            transcribeChunk(chunkData: data, chunkIndex: chunkIdx)
            // Don't advance byteOffset — re-transcribe this chunk as it grows
        }
    }
    timer.resume()
}

// MARK: - Authorization

func requestAuthorization(completion: @escaping (Bool) -> Void) {
    let status = SFSpeechRecognizer.authorizationStatus()
    switch status {
    case .authorized:
        completion(true)
    case .notDetermined:
        SFSpeechRecognizer.requestAuthorization { newStatus in
            completion(newStatus == .authorized)
        }
    default:
        completion(false)
    }
}

// MARK: - Entry Point

let locale = Locale(identifier: localeId)

fputs("SpeechTranscribe: locale=\(localeId) watch=\(watchMode) input=\(wavPath) output=\(outputPath ?? "stdout")\n", stderr)

requestAuthorization { authorized in
    guard authorized else {
        log("Error: Speech recognition not authorized. Grant permission in System Settings > Privacy & Security > Speech Recognition.")
        exit(1)
    }

    log("authorized, starting...")

    if watchMode {
        transcribeWatching(path: wavPath, locale: locale)
    } else {
        transcribeFile(path: wavPath, locale: locale)
    }
}

RunLoop.main.run()
