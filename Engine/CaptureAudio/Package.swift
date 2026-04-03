// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "CaptureAudio",
    platforms: [
        .macOS(.v14)
    ],
    targets: [
        .executableTarget(
            name: "CaptureAudio",
            path: "Sources",
            swiftSettings: [
                .unsafeFlags(["-strict-concurrency=complete"])
            ]
        )
    ]
)
