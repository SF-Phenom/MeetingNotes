import Foundation

// Fires every `intervalSeconds`, queries Zoom's participant panel via
// ZoomAX, writes one JSONL record per tick.
//
// "No-op tick" cases (returning count: null) are always written so the
// pipeline can distinguish "observer ran but couldn't see the panel"
// (user never opened it, Zoom minimized, etc.) from "observer wasn't
// running at all" (sidecar absent). The pipeline's fallback policy cares
// about that distinction.
final class ParticipantsPoller {

    // How often to poll by default. 10 s is enough for a ceiling bound
    // (max_speakers) that only grows; AX calls are cheap but not free and
    // we want the observer invisible on the system. Tests can override
    // via the init parameter.
    static let defaultIntervalSeconds: TimeInterval = 10.0

    private let writer: JSONLWriter
    private let startTime: TimeInterval
    private let observerVersion: Int
    private let intervalSeconds: TimeInterval
    private let dumpGate = DumpGate()
    private var timer: DispatchSourceTimer?
    private let queue = DispatchQueue(
        label: "com.meetingnotes.zoom-observer.poll",
        qos: .utility
    )

    init(
        writer: JSONLWriter,
        startTime: TimeInterval,
        observerVersion: Int,
        intervalSeconds: TimeInterval = ParticipantsPoller.defaultIntervalSeconds
    ) {
        self.writer = writer
        self.startTime = startTime
        self.observerVersion = observerVersion
        self.intervalSeconds = intervalSeconds
    }

    func start() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        // Fire once right away so we capture the initial count, then on the
        // configured cadence. Avoids a 10 s blind window at the start of
        // short recordings.
        t.schedule(
            deadline: .now(),
            repeating: intervalSeconds
        )
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume()
        self.timer = t
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    private func tick() {
        let t = Date().timeIntervalSince1970 - startTime
        var record: [String: Any] = [
            "t": t,
            "observer_version": observerVersion,
        ]

        guard let pid = ZoomAX.zoomProcessID() else {
            record["count"] = NSNull()
            record["reason"] = "zoom_not_running"
            writer.write(record)
            return
        }

        if let count = ZoomAX.participantCount(zoomPID: pid, dumpGateRef: dumpGate) {
            record["count"] = count
            record["source"] = "participants_panel"
        } else {
            record["count"] = NSNull()
            record["reason"] = "panel_closed"
        }
        writer.write(record)
    }
}
