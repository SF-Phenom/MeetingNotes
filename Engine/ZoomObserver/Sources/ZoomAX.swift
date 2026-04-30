import AppKit
import ApplicationServices
import Foundation

// Zoom Accessibility-tree navigation for the participant panel.
//
// Every selector here is defensive — Zoom ships UI updates every ~2 weeks
// and the AX tree structure shifts without warning. When a lookup fails
// we return nil rather than raise: the poller tick records count: null
// and the pipeline falls back to the calendar-derived bound. The observer
// is experimental by design; its failure mode must be "degrade silently,
// never break audio capture".
//
// The first successful Zoom attach per run emits a truncated tree dump
// to stderr so we have a real-world snapshot to iterate selectors against
// without building a debug binary.

enum ZoomAX {

    // Zoom Workplace desktop client on Apple Silicon.
    static let zoomBundleIdentifier = "us.zoom.xos"

    // Case-insensitive tokens we look for in titles/role-descriptions when
    // identifying the participant panel. Zoom localizes these per user
    // language — we only try English for now. Non-English users see a
    // graceful no-op (count: null) until we extend this list.
    private static let participantPanelTokens = ["participants"]

    // Guardrails so a walker runs in bounded time if the tree is huge or
    // cyclic. Zoom trees max out around depth 8-10 in practice; we allow
    // 14 for headroom.
    private static let maxWalkDepth = 14
    private static let maxNodesVisited = 2000

    // Attempt to locate the running Zoom desktop client. Returns nil when
    // Zoom isn't running, or when only the web client (browser) is active.
    static func zoomProcessID() -> pid_t? {
        let matches = NSRunningApplication.runningApplications(
            withBundleIdentifier: zoomBundleIdentifier
        )
        return matches.first?.processIdentifier
    }

    // True when Zoom has a window whose title contains "meeting" — the
    // load-bearing signal for "user is currently in a Zoom call". Zoom's
    // home screen window is titled "Zoom Workplace" and is excluded by
    // requiring "meeting" in the title; the in-call window is "Zoom
    // Meeting"; and during screenshare the floating control bar still
    // carries "Meeting" in its title. False positives on a "Schedule a
    // Meeting" dialog are tolerable — they'd just delay an auto-stop.
    static func isInMeeting(zoomPID: pid_t) -> Bool {
        let app = AXUIElementCreateApplication(zoomPID)
        guard let windows: [AXUIElement] = children(app, attr: kAXWindowsAttribute) else {
            return false
        }
        for window in windows {
            guard let title = attrString(
                window, attr: kAXTitleAttribute as String
            )?.lowercased() else {
                continue
            }
            if title.contains("meeting") {
                return true
            }
        }
        return false
    }

    // Walk the given Zoom process and return the participant count, or
    // nil when we couldn't locate the panel (closed, not open yet, or
    // Zoom UI shape changed). Emits the first-attach tree dump once per
    // `dumpGateRef` instance — caller threads a single Bool through so
    // the dump fires exactly once.
    static func participantCount(
        zoomPID: pid_t,
        dumpGateRef: DumpGate
    ) -> Int? {
        let app = AXUIElementCreateApplication(zoomPID)

        guard let windows: [AXUIElement] = Self.children(app, attr: kAXWindowsAttribute) else {
            return nil
        }

        if !dumpGateRef.didDump {
            dumpGateRef.didDump = true
            emitTreeDump(app: app)
        }

        var nodesVisited = 0
        for window in windows {
            if let panel = findParticipantContainer(
                root: window,
                depth: 0,
                nodesVisited: &nodesVisited
            ) {
                return rowCount(in: panel)
            }
        }
        return nil
    }

    // Best-effort row count. Prefer a direct child-count attribute; fall
    // back to AXRows for table shapes.
    private static func rowCount(in container: AXUIElement) -> Int? {
        if let n = childCount(container, attr: kAXRowsAttribute), n > 0 {
            return n
        }
        if let n = childCount(container, attr: kAXChildrenAttribute), n > 0 {
            return n
        }
        return nil
    }

    // Recursive depth-first search for a container whose role is list or
    // table AND whose title/description contains "participants". We stop
    // at the first match (Zoom has only one participants panel per window)
    // and bound total work by `nodesVisited`.
    private static func findParticipantContainer(
        root: AXUIElement,
        depth: Int,
        nodesVisited: inout Int
    ) -> AXUIElement? {
        if depth > maxWalkDepth || nodesVisited > maxNodesVisited {
            return nil
        }
        nodesVisited += 1

        if isParticipantContainer(root) {
            return root
        }
        guard let children: [AXUIElement] = Self.children(root, attr: kAXChildrenAttribute) else {
            return nil
        }
        for child in children {
            if let found = findParticipantContainer(
                root: child,
                depth: depth + 1,
                nodesVisited: &nodesVisited
            ) {
                return found
            }
        }
        return nil
    }

    private static func isParticipantContainer(_ element: AXUIElement) -> Bool {
        guard let role: String = attrString(element, attr: kAXRoleAttribute) else {
            return false
        }
        // Lists and tables are the realistic shapes Zoom uses for the
        // participant list. Outline is included because some Zoom builds
        // nest the list inside one.
        let listyRoles = Set([
            kAXListRole as String,
            kAXTableRole as String,
            kAXOutlineRole as String,
        ])
        guard listyRoles.contains(role) else {
            return false
        }

        // Any of title, description, or identifier matching is good enough.
        for attr in [kAXTitleAttribute, kAXDescriptionAttribute, kAXIdentifierAttribute] {
            if let s = attrString(element, attr: attr as String)?.lowercased() {
                if participantPanelTokens.contains(where: s.contains) {
                    return true
                }
            }
        }
        return false
    }

    // Dump a shallow snapshot (roles + a couple of labels) of the first
    // three levels of Zoom's window tree. Emits one line to stderr; the
    // Python recorder captures stderr into the app log. Invaluable when
    // Zoom's UI shifts and the selector needs updating — we have real
    // tree shape without building a debug binary.
    private static func emitTreeDump(app: AXUIElement) {
        var summary: [String] = []
        visit(app, depth: 0, maxDepth: 3, summary: &summary)
        let joined = summary.prefix(40).joined(separator: " | ")
        FileHandle.standardError.write(Data(
            "zoom-observer: ax_tree_dump: \(joined)\n".utf8
        ))
    }

    private static func visit(
        _ el: AXUIElement,
        depth: Int,
        maxDepth: Int,
        summary: inout [String]
    ) {
        if depth > maxDepth || summary.count > 60 { return }
        let role = attrString(el, attr: kAXRoleAttribute as String) ?? "?"
        let title = attrString(el, attr: kAXTitleAttribute as String)
            ?? attrString(el, attr: kAXDescriptionAttribute as String)
            ?? ""
        let truncated = title.prefix(24)
        summary.append("[\(depth)\(role)\(truncated.isEmpty ? "" : ":\(truncated)")]")
        if let children: [AXUIElement] = children(el, attr: kAXChildrenAttribute) {
            for c in children.prefix(6) {
                visit(c, depth: depth + 1, maxDepth: maxDepth, summary: &summary)
            }
        }
    }

    // MARK: - Typed AX attribute helpers

    private static func attrString(_ el: AXUIElement, attr: String) -> String? {
        var raw: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(el, attr as CFString, &raw)
        guard err == .success, let s = raw as? String else { return nil }
        return s
    }

    // Pull an array-typed attribute as [AXUIElement]. AX returns CFArrays
    // containing AXUIElementRefs; we let Swift bridge via `as?` since
    // AXUIElement is a CF type.
    private static func children(_ el: AXUIElement, attr: String) -> [AXUIElement]? {
        var raw: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(el, attr as CFString, &raw)
        guard err == .success, let arr = raw as? [AXUIElement] else { return nil }
        return arr
    }

    private static func childCount(_ el: AXUIElement, attr: String) -> Int? {
        var count: CFIndex = 0
        let err = AXUIElementGetAttributeValueCount(el, attr as CFString, &count)
        guard err == .success else { return nil }
        return count
    }
}

// Shared one-shot dump gate. Observer keeps a single instance across
// poller ticks so the "first successful attach" tree dump fires exactly
// once per run — subsequent ticks reuse it and no-op.
final class DumpGate {
    var didDump: Bool = false
}
