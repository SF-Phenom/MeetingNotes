import CoreAudio
import Darwin
import Foundation

// MARK: - Argument Parsing

func usage() -> Never {
    fputs("""
        Usage:
            CaptureAudio start --output /path/to/file.wav
            CaptureAudio check-audio-capture
        \n
        """, stderr)
    exit(1)
}

let args = CommandLine.arguments

// Permission pre-flight subcommand. Exits 0 if Audio Capture access is
// granted (Process Tap creation succeeds), 10 if it's denied, other non-zero
// for unexpected failures. Called by the Python recorder at menubar startup
// so the UI can surface a clear message instead of silently falling back to
// mic-only mode.
let PERMISSION_DENIED_EXIT_CODE: Int32 = 10
if args.count >= 2 && args[1] == "check-audio-capture" {
    // Minimal round-trip: create a tap excluding our own process, destroy it.
    let selfPID = ProcessInfo.processInfo.processIdentifier
    guard let selfObjID = pidToAudioObjectID(selfPID) else {
        fputs("check-audio-capture: could not resolve self pid\n", stderr)
        exit(PERMISSION_DENIED_EXIT_CODE)
    }
    let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [selfObjID])
    desc.isPrivate = true
    desc.name = "MeetingNotes permission probe"
    var tap: AudioObjectID = kAudioObjectUnknown
    let err = AudioHardwareCreateProcessTap(desc, &tap)
    if err == noErr, tap != kAudioObjectUnknown {
        _ = AudioHardwareDestroyProcessTap(tap)
        exit(0)
    }
    fputs("check-audio-capture: AudioHardwareCreateProcessTap failed \(fmtStatus(err))\n", stderr)
    exit(PERMISSION_DENIED_EXIT_CODE)
}

guard args.count >= 4,
      args[1] == "start",
      args[2] == "--output" else {
    usage()
}

let outputPath = args[3]

// MARK: - Paths

let meetingNotesHome = ProcessInfo.processInfo.environment["MEETINGNOTES_HOME"]
    ?? (NSHomeDirectory() as NSString).appendingPathComponent("MeetingNotes")
let lockDir = (meetingNotesHome as NSString)
    .appendingPathComponent("Engine/recordings/active")
let lockPath = (lockDir as NSString).appendingPathComponent(".lock")

// MARK: - WAV Writer (single mixed output)

let mixedWriter: WAVWriter
do {
    let outputDir = (outputPath as NSString).deletingLastPathComponent
    try FileManager.default.createDirectory(atPath: outputDir,
                                            withIntermediateDirectories: true)
    mixedWriter = try WAVWriter(path: outputPath)
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

let captureManager = AudioCaptureManager(mixedWriter: mixedWriter)

do {
    try captureManager.start()
} catch {
    fputs("Failed to start audio capture: \(error)\n", stderr)
    try? FileManager.default.removeItem(atPath: lockPath)
    exit(1)
}

// MARK: - Signal Handling

func shutdown() {
    // Drain rings into the WAV writer and finalize the header BEFORE
    // touching CoreAudio teardown. teardownAudio() can stall in
    // AudioHardwareDestroyProcessTap / aggregate-device destruction
    // when a tap reinit is in flight, and the recorder will SIGKILL us
    // after ~8 s. Finalizing first means the WAV on disk is already
    // valid even if we never reach the bottom of this function.
    captureManager.flush()
    do {
        try mixedWriter.finalize()
    } catch {
        fputs("Warning: Failed to finalize WAV file: \(error)\n", stderr)
    }
    try? FileManager.default.removeItem(atPath: lockPath)

    // Best-effort CoreAudio teardown. A stall here is recoverable
    // because the WAV is already on disk; the recorder's SIGKILL just
    // means a few leaked CoreAudio handles that the OS reclaims on
    // process exit.
    captureManager.teardownAudio()
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

fputs("CaptureAudio: recording mixed mic + system audio to \(outputPath)\n", stderr)
RunLoop.main.run()
