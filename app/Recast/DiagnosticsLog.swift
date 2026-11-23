import AppKit
import Foundation

/// A small rolling log at ~/Library/Logs/Recast/recast.log (1 MB per file, 2 old files kept).
/// It stays on this Mac and is never sent anywhere. Entries can contain file names.
final class DiagnosticsLog {
    static let shared = DiagnosticsLog(directory: FileManager.default
        .urls(for: .libraryDirectory, in: .userDomainMask).first!
        .appendingPathComponent("Logs/Recast"))

    let directory: URL
    let maxBytes: Int
    var url: URL { directory.appendingPathComponent("recast.log") }
    private let queue = DispatchQueue(label: "recast.log")
    private static let stamp: ISO8601DateFormatter = ISO8601DateFormatter()

    init(directory: URL, maxBytes: Int = 1_000_000) {
        self.directory = directory
        self.maxBytes = maxBytes
    }

    func log(_ message: String) {
        let line = "\(Self.stamp.string(from: Date())) \(message)\n"
        queue.async { [self] in
            let fm = FileManager.default
            try? fm.createDirectory(at: directory, withIntermediateDirectories: true)
            rotateIfNeeded()
            if let data = line.data(using: .utf8) {
                if let h = try? FileHandle(forWritingTo: url) {
                    defer { try? h.close() }
                    _ = try? h.seekToEnd()
                    try? h.write(contentsOf: data)
                } else {
                    let header = "# Recast log. It stays on this Mac and can contain file names: check it before sharing.\n"
                    try? (header.data(using: .utf8)! + data).write(to: url)
                }
            }
        }
    }

    /// Blocks until queued writes are on disk (used by tests).
    func flush() { queue.sync {} }

    private func rotateIfNeeded() {
        let fm = FileManager.default
        let size = (try? fm.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        guard size >= maxBytes else { return }
        let one = directory.appendingPathComponent("recast.log.1")
        let two = directory.appendingPathComponent("recast.log.2")
        try? fm.removeItem(at: two)
        try? fm.moveItem(at: one, to: two)
        try? fm.moveItem(at: url, to: one)
    }

    @MainActor func revealInFinder() {
        try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            NSWorkspace.shared.open(directory)
        }
    }
}
