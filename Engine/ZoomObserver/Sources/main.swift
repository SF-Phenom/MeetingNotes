import ApplicationServices
import Darwin
import Foundation

// MeetingNotes Zoom Accessibility observer.
//
// Usage:
//   zoom-observer start --output /path/to/recording.participants.jsonl
//                       [--interval-seconds 10]
//   zoom-observer check-accessibility
//
// The start subcommand runs until SIGINT/SIGTERM, polling Zoom's
// participant panel every interval and writing one JSONL record per
// poll. check-accessibility triggers the macOS TCC prompt if the user
// hasn't already granted access; returns exit 0 when trusted, 10 when
// not (mirrors capture-audio's permission preflight).
//
// Exit 0 is used for *graceful* non-observation too — missing TCC, Zoom
// not running, etc. all exit 0. Non-zero exit means the observer itself
// is broken (bad args, I/O errors at startup). This mirrors the plan's
// "degrade silently, never break audio capture" posture.

let PERMISSION_DENIED_EXIT_CODE: Int32 = 10
let OBSERVER_VERSION = 1

// MARK: - Arg parsing ----------------------------------------------------

func usage() -> Never {
    let msg = """
    Usage:
        zoom-observer start --output /path/to/recording.participants.jsonl [--interval-seconds 10]
        zoom-observer check-accessibility
    """
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(1)
}

let args = CommandLine.arguments
guard args.count >= 2 else { usage() }

// MARK: - check-accessibility subcommand -------------------------------

if args[1] == "check-accessibility" {
    // kAXTrustedCheckOptionPrompt = true → macOS presents the grant
    // dialog if TCC hasn't recorded a decision yet. We call this at
    // opt-in time so the prompt appears while the user is looking at
    // the setting, not mid-meeting.
    let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary
    let trusted = AXIsProcessTrustedWithOptions(options)
    exit(trusted ? 0 : PERMISSION_DENIED_EXIT_CODE)
}

guard args[1] == "start" else { usage() }

// Parse `start` options.
var outputPath: String?
var intervalSeconds: TimeInterval = ParticipantsPoller.defaultIntervalSeconds

var i = 2
while i < args.count {
    let flag = args[i]
    i += 1
    guard i < args.count else {
        FileHandle.standardError.write(Data("Missing value for \(flag)\n".utf8))
        exit(1)
    }
    let value = args[i]
    i += 1
    switch flag {
    case "--output":
        outputPath = value
    case "--interval-seconds":
        guard let n = Double(value), n > 0 else {
            FileHandle.standardError.write(Data(
                "--interval-seconds must be a positive number (got \(value))\n".utf8
            ))
            exit(1)
        }
        intervalSeconds = n
    default:
        FileHandle.standardError.write(Data("Unknown argument: \(flag)\n".utf8))
        exit(1)
    }
}

guard let outputPath else { usage() }

// MARK: - Accessibility preflight (no prompt) --------------------------

// On start we do NOT present the TCC dialog — opt-in flow handles that
// via the `check-accessibility` subcommand. If the user has the flag on
// but hasn't granted, we exit 0 with a stderr note so the Python
// recorder's log shows the reason and the pipeline falls back to calendar.
let silentOptions = [kAXTrustedCheckOptionPrompt.takeUnretainedValue(): false] as CFDictionary
if !AXIsProcessTrustedWithOptions(silentOptions) {
    FileHandle.standardError.write(Data(
        "zoom-observer: Accessibility permission not granted; exiting without observation.\n".utf8
    ))
    exit(0)
}

// MARK: - Lock file ------------------------------------------------------

// Mirrors capture-audio's lockfile pattern so orphan recovery on the
// Python side is symmetric. Path derived from MEETINGNOTES_HOME for
// test overrides.
let meetingNotesHome = ProcessInfo.processInfo.environment["MEETINGNOTES_HOME"]
    ?? (NSHomeDirectory() as NSString).appendingPathComponent("MeetingNotes")
let lockDir = (meetingNotesHome as NSString)
    .appendingPathComponent("Engine/recordings/active")
let lockPath = (lockDir as NSString).appendingPathComponent(".zoom-observer.lock")

do {
    try FileManager.default.createDirectory(
        atPath: lockDir, withIntermediateDirectories: true
    )
    let pid = String(ProcessInfo.processInfo.processIdentifier)
    try pid.write(toFile: lockPath, atomically: true, encoding: .utf8)
} catch {
    FileHandle.standardError.write(Data(
        "zoom-observer: Warning: Could not write lock file: \(error)\n".utf8
    ))
}

// MARK: - JSONL writer ---------------------------------------------------

let writer: JSONLWriter
do {
    let parent = (outputPath as NSString).deletingLastPathComponent
    if !parent.isEmpty {
        try FileManager.default.createDirectory(
            atPath: parent, withIntermediateDirectories: true
        )
    }
    writer = try JSONLWriter(path: outputPath)
} catch {
    FileHandle.standardError.write(Data(
        "zoom-observer: Could not open output file \(outputPath): \(error)\n".utf8
    ))
    try? FileManager.default.removeItem(atPath: lockPath)
    exit(1)
}

// MARK: - Poller ---------------------------------------------------------

// Record the binary's own start time as t0. The capture-audio binary
// starts a fraction of a second earlier, but the drift is bounded by
// subprocess launch latency (tens of ms) — far below our 10 s poll
// cadence. For bounds derivation this is more than accurate enough.
let startTime = Date().timeIntervalSince1970

let poller = ParticipantsPoller(
    writer: writer,
    startTime: startTime,
    observerVersion: OBSERVER_VERSION,
    intervalSeconds: intervalSeconds
)
poller.start()

// MARK: - Signal handling + shutdown ------------------------------------

var didShutdown = false
func shutdown() {
    // Guard against double-fire (SIGINT followed immediately by SIGTERM
    // isn't uncommon if the Python recorder escalates).
    if didShutdown { return }
    didShutdown = true
    poller.stop()
    writer.close()
    try? FileManager.default.removeItem(atPath: lockPath)
    exit(0)
}

let sigintSource = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
let sigtermSource = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
signal(SIGINT, SIG_IGN)
signal(SIGTERM, SIG_IGN)
sigintSource.setEventHandler { shutdown() }
sigtermSource.setEventHandler { shutdown() }
sigintSource.resume()
sigtermSource.resume()

FileHandle.standardError.write(Data(
    "zoom-observer: watching Zoom → \(outputPath) (every \(intervalSeconds)s)\n".utf8
))
RunLoop.main.run()
