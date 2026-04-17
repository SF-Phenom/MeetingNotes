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
            ]
        ),
        .executableTarget(
            name: "TapSpike",
            path: "SpikeSources"
        )
    ]
)
