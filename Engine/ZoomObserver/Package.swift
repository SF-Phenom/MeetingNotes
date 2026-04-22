// swift-tools-version: 5.9
import PackageDescription

// MeetingNotes Zoom Accessibility observer.
//
// Runs alongside CaptureAudio during Zoom recordings (when the user
// opts in via ax_participants_enabled). Polls the Zoom.app participant
// panel via the macOS Accessibility API every 10 s and writes a
// .participants.jsonl sidecar next to the .wav. The pipeline reads the
// peak observed count post-record to tighten the diarizer's
// --max-speakers bound.
//
// Isolated from CaptureAudio because (a) Accessibility TCC attaches
// per-binary, so keeping AX off the audio binary preserves the existing
// audio-permission UX; (b) Zoom ships UI updates every ~2 weeks — an AX
// tree-walk crash here can only kill the observer, never audio capture.
let package = Package(
    name: "ZoomObserver",
    platforms: [
        .macOS("14.2")
    ],
    targets: [
        .executableTarget(
            name: "ZoomObserver",
            path: "Sources",
            linkerSettings: [
                // Embed Info.plist into the Mach-O so TCC attributes the
                // Accessibility prompt to this binary (with our own usage
                // string) instead of walking up to the parent Terminal /
                // python3.12 process. Mirrors CaptureAudio's pattern.
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Resources/Info.plist",
                ])
            ]
        )
    ]
)
