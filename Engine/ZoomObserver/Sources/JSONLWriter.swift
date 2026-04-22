import Foundation

// Append-only JSONL writer. Every record is flushed + fsync'd before the
// method returns so a mid-recording crash leaves every prior line intact;
// at worst the last (unfinished) line gets truncated, which consumers
// already have to tolerate because shutdown on SIGKILL skips this path.
//
// Not thread-safe: the caller must serialize writes (our poller runs on a
// single Dispatch queue, so this is already the case).
final class JSONLWriter {

    private let handle: FileHandle

    init(path: String) throws {
        // Ensure the file exists — FileHandle(forWritingAtPath:) returns nil
        // on a missing file, but .attachFileHandleForAppending() creates it
        // when needed.
        let fm = FileManager.default
        if !fm.fileExists(atPath: path) {
            guard fm.createFile(atPath: path, contents: nil) else {
                throw NSError(
                    domain: "ZoomObserver.JSONLWriter",
                    code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "Could not create \(path)"]
                )
            }
        }
        guard let h = FileHandle(forWritingAtPath: path) else {
            throw NSError(
                domain: "ZoomObserver.JSONLWriter",
                code: 2,
                userInfo: [NSLocalizedDescriptionKey: "Could not open \(path) for writing"]
            )
        }
        try h.seekToEnd()
        self.handle = h
    }

    // Serialize `record` to one JSON line + newline, write, fsync.
    // Returns silently on any error — the caller's poller tick is more
    // valuable than this single record.
    func write(_ record: [String: Any]) {
        guard
            let data = try? JSONSerialization.data(
                withJSONObject: record,
                options: [.sortedKeys]
            )
        else {
            FileHandle.standardError.write(Data(
                "zoom-observer: could not serialize record\n".utf8
            ))
            return
        }
        do {
            try handle.write(contentsOf: data)
            try handle.write(contentsOf: Data("\n".utf8))
            try handle.synchronize()
        } catch {
            FileHandle.standardError.write(Data(
                "zoom-observer: JSONL write failed: \(error)\n".utf8
            ))
        }
    }

    func close() {
        try? handle.close()
    }
}
