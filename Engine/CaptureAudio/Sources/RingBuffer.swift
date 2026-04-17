import Foundation

/// Fixed-capacity SPSC ring buffer of Int16 samples.
///
/// Producer: one audio callback thread (mic tap or Process Tap IOProc).
/// Consumer: one drainer thread (`MixerDrainer`).
///
/// Uses monotonically-increasing Int indices so `writeIdx == readIdx` ≡ empty
/// and `writeIdx - readIdx == capacity` ≡ full, without a wrap ambiguity bit.
/// Access is serialised by `NSLock`; each operation holds it only for a
/// couple of `memcpy`s and an index update — well under the audio callback's
/// time budget at the volumes we see (~480 samples at 48 kHz ≈ 10 ms).
///
/// On overflow (producer outruns consumer) the oldest samples are dropped;
/// `overflowCount` is bumped so callers can log it. In practice this should
/// never fire — 30 s of headroom at 16 kHz is ~960 KB.
final class RingBuffer: @unchecked Sendable {
    let capacity: Int
    private let storage: UnsafeMutableBufferPointer<Int16>
    private var writeIdx: Int = 0
    private var readIdx: Int = 0
    private var overflows: Int = 0
    private let lock = NSLock()

    init(capacity: Int) {
        precondition(capacity > 0, "RingBuffer capacity must be > 0")
        self.capacity = capacity
        let raw = UnsafeMutablePointer<Int16>.allocate(capacity: capacity)
        raw.initialize(repeating: 0, count: capacity)
        self.storage = UnsafeMutableBufferPointer(start: raw, count: capacity)
    }

    deinit {
        storage.baseAddress?.deinitialize(count: capacity)
        storage.baseAddress?.deallocate()
    }

    var availableToRead: Int {
        lock.lock(); defer { lock.unlock() }
        return writeIdx - readIdx
    }

    var overflowCount: Int {
        lock.lock(); defer { lock.unlock() }
        return overflows
    }

    /// Write `count` samples from `samples` into the ring. If there's not
    /// enough free space, advances the read head to make room and bumps
    /// `overflows`. A single write larger than `capacity` is rejected with
    /// a warning (should never happen in practice).
    func write(_ samples: UnsafePointer<Int16>, count: Int) {
        guard count > 0 else { return }
        guard count < capacity else {
            fputs(
                "RingBuffer: single-write count=\(count) exceeds capacity=\(capacity) — dropping\n",
                stderr
            )
            return
        }
        lock.lock()
        defer { lock.unlock() }

        let available = capacity - (writeIdx - readIdx)
        if count > available {
            overflows += 1
            readIdx += count - available
        }

        let offset = writeIdx % capacity
        let firstChunk = min(count, capacity - offset)
        memcpy(storage.baseAddress! + offset, samples, firstChunk * MemoryLayout<Int16>.size)
        if firstChunk < count {
            memcpy(
                storage.baseAddress!,
                samples + firstChunk,
                (count - firstChunk) * MemoryLayout<Int16>.size
            )
        }
        writeIdx += count
    }

    /// Read up to `maxCount` samples into `dst`. Returns the actual number
    /// read (≤ `maxCount`, possibly 0 if the ring is empty).
    @discardableResult
    func read(into dst: UnsafeMutablePointer<Int16>, maxCount: Int) -> Int {
        guard maxCount > 0 else { return 0 }
        lock.lock()
        defer { lock.unlock() }

        let available = writeIdx - readIdx
        let n = min(maxCount, available)
        guard n > 0 else { return 0 }

        let offset = readIdx % capacity
        let firstChunk = min(n, capacity - offset)
        memcpy(dst, storage.baseAddress! + offset, firstChunk * MemoryLayout<Int16>.size)
        if firstChunk < n {
            memcpy(
                dst + firstChunk,
                storage.baseAddress!,
                (n - firstChunk) * MemoryLayout<Int16>.size
            )
        }
        readIdx += n
        return n
    }
}
