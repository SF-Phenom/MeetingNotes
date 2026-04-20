// swift-tools-version: 6.0
import PackageDescription

// MeetingNotes speaker diarization CLI.
//
// Shells-out-from-Python pattern mirrors CaptureAudio: a thin Swift
// executable that reads a WAV path, runs FluidAudio's CoreML-backed
// diarization on the Apple Neural Engine, and writes a JSON segment
// list for Python to consume. Python is responsible for aligning
// segments to transcript sentences and mapping raw speaker IDs to
// user-visible "Speaker A"/"Speaker B"/... labels.
//
// Built by setup.command into Engine/.bin/meetingnotes-diarize. Models
// download lazily on first run via FluidAudio.prepareModels() /
// SortformerModels.loadFromHuggingFace() and cache under
// ~/.cache/fluidaudio/.
let package = Package(
    name: "Diarize",
    platforms: [
        .macOS("14.2"),
    ],
    dependencies: [
        // Pinned. FluidAudio is pre-1.0 — API has been shifting every
        // few months. Bump deliberately, test against a real meeting
        // after each bump.
        .package(url: "https://github.com/FluidInference/FluidAudio.git", from: "0.13.6"),
    ],
    targets: [
        .executableTarget(
            name: "Diarize",
            dependencies: [
                .product(name: "FluidAudio", package: "FluidAudio"),
            ],
            path: "Sources"
        ),
    ]
)
