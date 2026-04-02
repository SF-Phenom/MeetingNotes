import Foundation

/// Writes 16-bit mono PCM audio to a WAV file.
/// Writes a 44-byte header with placeholder sizes on open,
/// appends PCM data via write(_:), and patches the header on finalize().
final class WAVWriter: @unchecked Sendable {
    private let fileHandle: FileHandle
    private var dataByteCount: UInt32 = 0

    // WAV format constants
    private let sampleRate: UInt32 = 16000
    private let numChannels: UInt16 = 1
    private let bitsPerSample: UInt16 = 16

    init(path: String) throws {
        // Create the file if it doesn't exist
        FileManager.default.createFile(atPath: path, contents: nil)
        guard let fh = FileHandle(forWritingAtPath: path) else {
            throw WAVWriterError.cannotOpenFile(path)
        }
        self.fileHandle = fh
        try writeHeader()
    }

    private func writeHeader() throws {
        var header = Data(capacity: 44)

        let byteRate: UInt32 = sampleRate * UInt32(numChannels) * UInt32(bitsPerSample) / 8
        let blockAlign: UInt16 = numChannels * (bitsPerSample / 8)

        // RIFF chunk
        header.append(contentsOf: Array("RIFF".utf8))
        header.appendLE(UInt32(0))          // placeholder: file size - 8
        header.append(contentsOf: Array("WAVE".utf8))

        // fmt sub-chunk
        header.append(contentsOf: Array("fmt ".utf8))
        header.appendLE(UInt32(16))         // sub-chunk size (PCM)
        header.appendLE(UInt16(1))          // PCM format
        header.appendLE(numChannels)
        header.appendLE(sampleRate)
        header.appendLE(byteRate)
        header.appendLE(blockAlign)
        header.appendLE(bitsPerSample)

        // data sub-chunk
        header.append(contentsOf: Array("data".utf8))
        header.appendLE(UInt32(0))          // placeholder: data byte count

        fileHandle.write(header)
    }

    func write(_ data: Data) {
        fileHandle.write(data)
        dataByteCount += UInt32(data.count)
    }

    func finalize() throws {
        // Patch the data chunk size at byte 40
        try fileHandle.seek(toOffset: 40)
        var dataSize = dataByteCount
        let dataSizeBytes = Data(bytes: &dataSize, count: 4)
        fileHandle.write(dataSizeBytes)

        // Patch the RIFF chunk size at byte 4: (36 + dataByteCount)
        try fileHandle.seek(toOffset: 4)
        var riffSize = 36 + dataByteCount
        let riffSizeBytes = Data(bytes: &riffSize, count: 4)
        fileHandle.write(riffSizeBytes)

        fileHandle.closeFile()
    }
}

enum WAVWriterError: Error {
    case cannotOpenFile(String)
}

// MARK: - Data helpers for little-endian encoding

private extension Data {
    mutating func appendLE(_ value: UInt16) {
        var v = value.littleEndian
        self.append(contentsOf: Swift.withUnsafeBytes(of: &v) { Array($0) })
    }

    mutating func appendLE(_ value: UInt32) {
        var v = value.littleEndian
        self.append(contentsOf: Swift.withUnsafeBytes(of: &v) { Array($0) })
    }
}
