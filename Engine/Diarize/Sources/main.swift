// MeetingNotes diarization CLI — thin wrapper around FluidAudio's CoreML
// offline diarizers.
//
// Usage:
//   meetingnotes-diarize --input <wav> --output <json> [--model community-1|sortformer]
//                       [--min-speakers N] [--max-speakers N]
//
// The executable reads a 16-bit PCM WAV file, runs the requested diarizer
// on the Apple Neural Engine (via CoreML), and writes a JSON document of
// the shape:
//
//   {"segments": [
//     {"start": 0.0, "end": 3.42, "speaker_id": "0"},
//     ...
//   ]}
//
// Python (see app/diarizer_fluidaudio.py) consumes this and re-labels
// "0"/"1"/... to "Speaker A"/"Speaker B"/... in first-appearance order.
// Putting the human-readable labelling in Python makes it trivial to
// unit-test without spawning Swift.
//
// Model lazy-download: FluidAudio downloads + compiles its CoreML bundles
// on first invocation (cached under ~/.cache/fluidaudio/). First diarization
// will be noticeably slower than subsequent ones.

import Foundation
import FluidAudio

// MARK: - CLI parsing -----------------------------------------------------

private struct CLIArgs {
    let input: String
    let output: String
    let model: String  // "community-1" or "sortformer"
    // Optional speaker-count bounds forwarded to community-1's clusterer.
    // Sortformer ignores these (architectural 4-track cap).
    let minSpeakers: Int?
    let maxSpeakers: Int?
}

private func die(_ msg: String, code: Int32 = 2) -> Never {
    FileHandle.standardError.write(Data((msg + "\n").utf8))
    exit(code)
}

private func parseArgs() -> CLIArgs {
    var input: String?
    var output: String?
    var model: String = "community-1"
    var minSpeakers: Int?
    var maxSpeakers: Int?

    let args = CommandLine.arguments
    var i = 1
    while i < args.count {
        let flag = args[i]
        i += 1
        guard i < args.count else {
            die("Missing value for \(flag)")
        }
        let value = args[i]
        i += 1
        switch flag {
        case "--input":
            input = value
        case "--output":
            output = value
        case "--model":
            model = value
        case "--min-speakers":
            guard let n = Int(value), n >= 1 else {
                die("--min-speakers must be a positive integer (got \(value))")
            }
            minSpeakers = n
        case "--max-speakers":
            guard let n = Int(value), n >= 1 else {
                die("--max-speakers must be a positive integer (got \(value))")
            }
            maxSpeakers = n
        default:
            die("Unknown argument: \(flag)")
        }
    }

    guard let input, let output else {
        die("""
            Usage: meetingnotes-diarize --input <wav> --output <json> [--model community-1|sortformer] \
            [--min-speakers N] [--max-speakers N]
            """)
    }

    return CLIArgs(
        input: input,
        output: output,
        model: model,
        minSpeakers: minSpeakers,
        maxSpeakers: maxSpeakers
    )
}

// MARK: - Output ----------------------------------------------------------

private struct OutSegment: Encodable {
    let start: Double
    let end: Double
    let speaker_id: String
}

private struct OutDoc: Encodable {
    let segments: [OutSegment]
}

private func writeOutput(_ segments: [OutSegment], to path: String) throws {
    let sorted = segments.sorted { $0.start < $1.start }
    let encoder = JSONEncoder()
    encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
    let data = try encoder.encode(OutDoc(segments: sorted))
    try data.write(to: URL(fileURLWithPath: path))
}

// MARK: - Community-1 (pyannote) diarizer ---------------------------------

@available(macOS 14.2, *)
private func runCommunity1(
    inputPath: String,
    minSpeakers: Int?,
    maxSpeakers: Int?
) async throws -> [OutSegment] {
    var config = OfflineDiarizerConfig()
    // Pass the caller's hints straight through to FluidAudio's VBx clusterer.
    // Docs note these are ignored when `numSpeakers` is set — we never set it,
    // so the bounds always take effect when present.
    if let minSpeakers {
        config.clustering.minSpeakers = minSpeakers
    }
    if let maxSpeakers {
        config.clustering.maxSpeakers = maxSpeakers
    }
    let manager = OfflineDiarizerManager(config: config)
    try await manager.prepareModels()

    let samples = try AudioConverter().resampleAudioFile(path: inputPath)
    let result = try await manager.process(audio: samples)

    return result.segments.map { seg in
        OutSegment(
            start: Double(seg.startTimeSeconds),
            end: Double(seg.endTimeSeconds),
            speaker_id: String(describing: seg.speakerId)
        )
    }
}

// MARK: - Sortformer diarizer ---------------------------------------------

@available(macOS 14.2, *)
private func runSortformer(
    inputPath: String,
    minSpeakers: Int?,
    maxSpeakers: Int?
) async throws -> [OutSegment] {
    if minSpeakers != nil || maxSpeakers != nil {
        // Sortformer has a fixed 4-track output head — it cannot honor a
        // bound. Log and proceed with the default config so callers can
        // pass bounds uniformly across models without branching.
        FileHandle.standardError.write(Data(
            "warning: --min-speakers / --max-speakers ignored for sortformer (fixed 4 output tracks)\n".utf8
        ))
    }
    let diarizer = SortformerDiarizer(config: .default)
    let models = try await SortformerModels.loadFromHuggingFace(config: .default)
    diarizer.initialize(models: models)

    let audioURL = URL(fileURLWithPath: inputPath)
    let timeline = try diarizer.processComplete(audioFileURL: audioURL)

    var out: [OutSegment] = []
    for (speakerIndex, speaker) in timeline.speakers {
        for seg in speaker.finalizedSegments {
            out.append(OutSegment(
                start: Double(seg.startTime),
                end: Double(seg.endTime),
                speaker_id: String(speakerIndex)
            ))
        }
    }
    return out
}

// MARK: - Entry point -----------------------------------------------------

@main
struct DiarizeCLI {
    static func main() async {
        let args = parseArgs()

        do {
            let segments: [OutSegment]
            switch args.model {
            case "community-1":
                segments = try await runCommunity1(
                    inputPath: args.input,
                    minSpeakers: args.minSpeakers,
                    maxSpeakers: args.maxSpeakers
                )
            case "sortformer":
                segments = try await runSortformer(
                    inputPath: args.input,
                    minSpeakers: args.minSpeakers,
                    maxSpeakers: args.maxSpeakers
                )
            default:
                die("Unknown model: \(args.model) (expected community-1 or sortformer)")
            }
            try writeOutput(segments, to: args.output)
        } catch {
            die("Diarization failed: \(error)", code: 1)
        }
    }
}
