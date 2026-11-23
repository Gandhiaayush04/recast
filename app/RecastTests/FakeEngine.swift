import Foundation
@testable import Recast

/// A stand-in for the Python engine. Tests decide what `targets`/`status` return
/// and push streaming events by hand.
final class FakeHandle: EngineHandle {
    var isRunning = true
    var terminated = false
    func terminate() { terminated = true; isRunning = false }
}

final class FakeEngine: EngineRunning {
    var runOnceCalls: [[String]] = []
    var streamCalls: [[String]] = []
    var onceReply: ([String]) -> [String: Any] = { _ in [:] }
    var onceDelay: ((_ args: [String]) -> UInt64)?
    var onceError: Error?
    var startError: Error?

    let handle = FakeHandle()
    private var onEvent: (([String: Any]) -> Void)?
    private var onExit: (() -> Void)?

    func runOnce(_ args: [String], timeout: TimeInterval) async throws -> [String: Any] {
        runOnceCalls.append(args)
        if let d = onceDelay?(args), d > 0 { try? await Task.sleep(nanoseconds: d) }
        if let e = onceError { throw e }
        return onceReply(args)
    }

    func stream(_ args: [String],
                onEvent: @escaping ([String: Any]) -> Void,
                onExit: @escaping () -> Void) throws -> EngineHandle {
        if let e = startError { throw e }
        streamCalls.append(args)
        self.onEvent = onEvent
        self.onExit = onExit
        handle.isRunning = true
        return handle
    }

    func send(_ event: [String: Any]) { onEvent?(event) }
    func exit() { handle.isRunning = false; onExit?() }
}

/// A canned `targets` answer.
func targetsReply(_ exts: [String], unsupported: [String] = [], combine: Bool = false, mixed: Bool = false) -> [String: Any] {
    ["targets": exts.map { ["ext": $0, "label": $0.uppercased()] },
     "unsupported_files": unsupported,
     "can_combine_pdf": combine,
     "mixed_no_common": mixed]
}
