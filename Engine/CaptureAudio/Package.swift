// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CaptureAudio",
    platforms: [
        .macOS("14.2")
    ],
    targets: [
        .executableTarget(
            name: "CaptureAudio",
            path: "Sources",
            swiftSettings: [
                .unsafeFlags(["-strict-concurrency=complete"])
            ],
            linkerSettings: [
                // Embed Info.plist into the Mach-O so TCC attributes the Audio
                // Capture + Microphone prompts to this binary (with our own
                // usage strings) instead of walking up to the parent Terminal
                // / python3.12 process. Relative path resolves from the
                // package root — swift build must be invoked from
                // Engine/CaptureAudio (which setup.command already does).
                .unsafeFlags([
                    "-Xlinker", "-sectcreate",
                    "-Xlinker", "__TEXT",
                    "-Xlinker", "__info_plist",
                    "-Xlinker", "Resources/Info.plist",
                ])
            ]
        ),
        .executableTarget(
            name: "TapSpike",
            path: "SpikeSources"
        )
    ]
)
