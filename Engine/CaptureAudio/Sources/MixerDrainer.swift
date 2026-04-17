import Foundation

/// Drains mic + system ring buffers on a 100 ms tick, saturating-mixes to
/// Int16, and appends to a single WAVWriter.
///
/// Both rings are expected to be 16 kHz mono Int16 — the AVAudioConverters
/// on the producer side are responsible for resampling/downmixing to that
/// common format before writing into the ring. Here we just read pre-
/// converted samples and combine them.
///
/// Clock skew between mic and Process Tap is handled by reading
/// `min(mic, sys)` each tick. The faster source leaves residual samples
/// in its ring which get drained on the next tick(s) once the slower side
/// catches up. Over long recordings drift stays bounded by the 30 s ring
/// capacity.
///
/// On stop the timer is cancelled and one final synchronous tick drains
/// remaining samples; anything in the longer ring past the min length is
/// discarded (at most ~100 ms).
final class MixerDrainer: @unchecked Sendable {
    private let micRing: RingBuffer
    private let systemRing: RingBuffer
    private let writer: WAVWriter
    private let queue: DispatchQueue
    private var timer: DispatchSourceTimer?

    // Pre-allocated scratch — avoids malloc on the tick thread. 1 s of
    // headroom at 16 kHz easily covers the 100 ms cadence.
    private let chunkCapacity = 16000
    private let micScratch: UnsafeMutableBufferPointer<Int16>
    private let sysScratch: UnsafeMutableBufferPointer<Int16>
    private let outScratch: UnsafeMutableBufferPointer<Int16>

    init(mic: RingBuffer, system: RingBuffer, writer: WAVWriter) {
        self.micRing = mic
        self.systemRing = system
        self.writer = writer
        self.queue = DispatchQueue(
            label: "com.meetingnotes.mixer",
            qos: .userInitiated
        )
        self.micScratch = Self.allocateScratch(chunkCapacity)
        self.sysScratch = Self.allocateScratch(chunkCapacity)
        self.outScratch = Self.allocateScratch(chunkCapacity)
    }

    deinit {
        Self.deallocateScratch(micScratch)
        Self.deallocateScratch(sysScratch)
        Self.deallocateScratch(outScratch)
    }

    private static func allocateScratch(_ n: Int) -> UnsafeMutableBufferPointer<Int16> {
        let raw = UnsafeMutablePointer<Int16>.allocate(capacity: n)
        raw.initialize(repeating: 0, count: n)
        return UnsafeMutableBufferPointer(start: raw, count: n)
    }

    private static func deallocateScratch(_ buf: UnsafeMutableBufferPointer<Int16>) {
        buf.baseAddress?.deinitialize(count: buf.count)
        buf.baseAddress?.deallocate()
    }

    func start() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(
            deadline: .now() + .milliseconds(100),
            repeating: .milliseconds(100),
            leeway: .milliseconds(10)
        )
        t.setEventHandler { [weak self] in
            self?.tick()
        }
        timer = t
        t.resume()
    }

    func stop() {
        timer?.cancel()
        timer = nil
        queue.sync { self.tick() }
    }

    private func tick() {
        let pending = min(micRing.availableToRead, systemRing.availableToRead)
        guard pending > 0 else { return }
        let n = min(pending, chunkCapacity)

        let micRead = micRing.read(into: micScratch.baseAddress!, maxCount: n)
        let sysRead = systemRing.read(into: sysScratch.baseAddress!, maxCount: n)
        let count = min(micRead, sysRead)
        guard count > 0 else { return }

        // Saturating add — keeps mixed samples in Int16 range without wrap.
        for i in 0..<count {
            let mixed = Int32(micScratch[i]) + Int32(sysScratch[i])
            outScratch[i] = Int16(clamping: mixed)
        }

        let data = Data(
            bytes: outScratch.baseAddress!,
            count: count * MemoryLayout<Int16>.size
        )
        writer.write(data)
    }
}
