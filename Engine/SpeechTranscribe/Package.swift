// swift-tools-version: 6.2
import PackageDescription

let package = Package(
    name: "SpeechTranscribe",
    platforms: [
        .macOS(.v26)
    ],
    targets: [
        .executableTarget(
            name: "SpeechTranscribe",
            path: "Sources"
        )
    ]
)
