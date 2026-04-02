import Foundation
import Darwin

// MARK: - Argument Parsing

func usage() -> Never {
    fputs("Usage: CaptureAudio start --output /path/to/file.wav\n", stderr)
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 4,
      args[1] == "start",
      args[2] == "--output" else {
    usage()
}

let outputPath = args[3]

// MARK: - Paths

let lockDir = (NSHomeDirectory() as NSString)
    .appendingPathComponent("MeetingNotes/recordings/active")
let lockPath = (lockDir as NSString).appendingPathComponent(".lock")

// MARK: - WAV Writer

let wavWriter: WAVWriter
do {
    // Ensure the output directory exists
    let outputDir = (outputPath as NSString).deletingLastPathComponent
    try FileManager.default.createDirectory(atPath: outputDir,
                                            withIntermediateDirectories: true)
    wavWriter = try WAVWriter(path: outputPath)
} catch {
    fputs("Failed to open output file: \(error)\n", stderr)
    exit(1)
}

// MARK: - Lock File

do {
    try FileManager.default.createDirectory(atPath: lockDir,
                                            withIntermediateDirectories: true)
    let pid = String(ProcessInfo.processInfo.processIdentifier)
    try pid.write(toFile: lockPath, atomically: true, encoding: .utf8)
} catch {
    fputs("Warning: Could not write lock file: \(error)\n", stderr)
}

// MARK: - Audio Capture

let captureManager = AudioCaptureManager(wavWriter: wavWriter)

do {
    try captureManager.start()
} catch {
    fputs("Failed to start audio capture: \(error)\n", stderr)
    // Clean up lock file before exit
    try? FileManager.default.removeItem(atPath: lockPath)
    exit(1)
}

// MARK: - Signal Handling

func shutdown() {
    captureManager.stop()
    do {
        try wavWriter.finalize()
    } catch {
        fputs("Warning: Failed to finalize WAV file: \(error)\n", stderr)
    }
    try? FileManager.default.removeItem(atPath: lockPath)
    exit(0)
}

// Use a DispatchSource for safe signal handling from the main thread
let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)

signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)

sigintSource.setEventHandler { shutdown() }
sigtermSource.setEventHandler { shutdown() }

sigintSource.resume()
sigtermSource.resume()

// MARK: - Run Loop

fputs("CaptureAudio: recording to \(outputPath)\n", stderr)
RunLoop.main.run()
