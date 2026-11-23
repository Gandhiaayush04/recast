import XCTest
@testable import Recast

@MainActor
final class AppModelTests: XCTestCase {
    var dir: URL!
    var engine: FakeEngine!
    var model: AppModel!

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("recast-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        engine = FakeEngine()
        engine.onceReply = { _ in targetsReply(["png", "pdf"]) }
        model = AppModel(engine: engine)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
    }

    @discardableResult
    func makeFile(_ name: String, in folder: URL? = nil) throws -> URL {
        let u = (folder ?? dir).appendingPathComponent(name)
        try Data("x".utf8).write(to: u)
        return u
    }

    /// Lets the `Task { await refreshTargets() }` started by add() finish.
    func settle() async {
        for _ in 0..<20 { await Task.yield() }
        try? await Task.sleep(nanoseconds: 50_000_000)
    }

    /// Adds files, waits for targets, and starts a convert so events can be pushed.
    func startRun(_ names: [String]) async throws {
        model.add(try names.map { try makeFile($0) })
        await settle()
        model.convert(to: "png")
        XCTAssertTrue(model.isWorking)
    }

    // MARK: adding files

    func testAddingAFileAsksTheEngineForTargets() async throws {
        model.add([try makeFile("a.jpg")])
        await settle()
        XCTAssertEqual(engine.runOnceCalls.count, 1)
        XCTAssertEqual(engine.runOnceCalls.first?.first, "targets")
        XCTAssertEqual(model.targets.map(\.ext), ["png", "pdf"])
        XCTAssertEqual(model.selectedTarget, "png")
    }

    func testAddingTheSameFileTwiceDoesNotDuplicate() async throws {
        let f = try makeFile("a.jpg")
        model.add([f]); await settle()
        model.add([f]); await settle()
        XCTAssertEqual(model.items.count, 1)
    }

    func testFolderDropTakesOnlyDirectChildren() async throws {
        let folder = dir.appendingPathComponent("pics")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try makeFile("one.jpg", in: folder)
        let sub = folder.appendingPathComponent("deeper")
        try FileManager.default.createDirectory(at: sub, withIntermediateDirectories: true)
        try makeFile("two.jpg", in: sub)
        model.add([folder]); await settle()
        XCTAssertEqual(model.items.map(\.name), ["one.jpg"])
    }

    func testEmptyFolderSetsNotice() async throws {
        let folder = dir.appendingPathComponent("empty")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        model.add([folder])
        XCTAssertEqual(model.notice, "Empty folders were skipped.")
        XCTAssertTrue(model.items.isEmpty)
    }

    func testStaleTargetsReplyIsIgnored() async throws {
        // First request (1 file) is slow and says "png"; the second (2 files) is fast and says "pdf".
        engine.onceReply = { args in targetsReply(args.count == 2 ? ["png"] : ["pdf"]) }
        engine.onceDelay = { args in args.count == 2 ? 300_000_000 : 0 }
        model.add([try makeFile("a.jpg")])
        try await Task.sleep(nanoseconds: 20_000_000)
        model.add([try makeFile("b.jpg")])
        try await Task.sleep(nanoseconds: 600_000_000)
        XCTAssertEqual(model.targets.map(\.ext), ["pdf"])
    }

    func testUnsupportedFilesAreMarked() async throws {
        let f = try makeFile("a.xyz")
        engine.onceReply = { _ in targetsReply([], unsupported: [f.standardizedFileURL.path]) }
        model.add([f]); await settle()
        XCTAssertEqual(model.items.first?.status, .unsupported)
    }

    // MARK: running

    func testAddingDuringARunIsQueuedUntilItFinishes() async throws {
        try await startRun(["a.jpg"])
        let late = try makeFile("late.jpg")
        model.add([late])
        XCTAssertEqual(model.items.count, 1)
        XCTAssertNotNil(model.notice)
        engine.exit(); await settle()
        XCTAssertEqual(model.items.count, 2)
        XCTAssertFalse(model.isWorking)
    }

    func testNoticeClearsAfterRunWhenNothingWasQueued() async throws {
        try await startRun(["a.jpg"])
        model.notice = "something else"
        engine.exit()
        XCTAssertEqual(model.notice, "something else")   // only our own message is cleared
    }

    func testProgressDoneSkippedAndFailedUpdateTheRightRows() async throws {
        try await startRun(["a.jpg", "b.jpg", "c.jpg"])
        engine.send(["event": "started", "id": 0])
        engine.send(["event": "progress", "id": 0, "progress": 0.5])
        XCTAssertEqual(model.items[0].status, .converting(0.5))
        engine.send(["event": "done", "id": 0, "outputs": ["/tmp/a.png"]])
        engine.send(["event": "skipped", "id": 1, "message": "Already PNG"])
        engine.send(["event": "failed", "id": 2, "message": "Broken", "missing_engine": "ffmpeg"])
        XCTAssertEqual(model.items[0].status, .done)
        XCTAssertEqual(model.items[0].outputs, [URL(fileURLWithPath: "/tmp/a.png")])
        XCTAssertEqual(model.items[1].status, .skipped("Already PNG"))
        XCTAssertEqual(model.items[2].status, .failed("Broken", missingEngine: "ffmpeg"))
        XCTAssertTrue(model.missingEngines.contains("ffmpeg"))
    }

    func testEarlyEngineExitMarksUnfinishedRowsFailed() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "started", "id": 0])
        engine.exit()
        XCTAssertEqual(model.items[0].status, .failed("The conversion stopped unexpectedly.", missingEngine: nil))
        XCTAssertFalse(model.isWorking)
    }

    func testCancelReturnsRowsToReady() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "started", "id": 0])
        model.cancel()
        XCTAssertTrue(model.isStopping)
        XCTAssertTrue(engine.handle.terminated)
        engine.exit()
        XCTAssertEqual(model.items[0].status, .ready)
        XCTAssertTrue(model.wasStopped)
        XCTAssertFalse(model.isStopping)
    }

    func testEngineThatCannotStartShowsAnError() async throws {
        model.add([try makeFile("a.jpg")]); await settle()
        engine.startError = NSError(domain: "t", code: 1)
        model.convert(to: "png")
        XCTAssertEqual(model.engineError, "Recast's conversion engine couldn't start.")
        XCTAssertFalse(model.isWorking)
    }

    // MARK: combine

    func testCombineMovesAllRowsTogether() async throws {
        engine.onceReply = { _ in targetsReply(["pdf"], combine: true) }
        model.add([try makeFile("a.jpg"), try makeFile("b.jpg")]); await settle()
        model.combineIntoPDF()
        XCTAssertEqual(engine.streamCalls.last?.first, "combine")
        engine.send(["event": "progress", "id": 0, "progress": 0.4])
        XCTAssertEqual(model.items.map(\.status), [.converting(0.4), .converting(0.4)])
        engine.send(["event": "done", "id": 0, "outputs": ["/tmp/c.pdf"]])
        XCTAssertEqual(model.combinedOutput, URL(fileURLWithPath: "/tmp/c.pdf"))
        XCTAssertTrue(model.items.allSatisfy { $0.status == .done && $0.combined })
    }

    func testCombineFailureSetsErrorAndResetsRows() async throws {
        engine.onceReply = { _ in targetsReply(["pdf"], combine: true) }
        model.add([try makeFile("a.jpg"), try makeFile("b.jpg")]); await settle()
        model.combineIntoPDF()
        engine.send(["event": "failed", "id": 0, "message": "Too big"])
        XCTAssertEqual(model.combineError, "Too big")
        XCTAssertTrue(model.items.allSatisfy { $0.status == .ready })
    }

    // MARK: engine install

    func testRemovingFilesClearsTargets() async throws {
        model.add([try makeFile("a.jpg")]); await settle()
        model.remove(model.items[0]); await settle()
        XCTAssertTrue(model.items.isEmpty)
        XCTAssertTrue(model.targets.isEmpty)
        XCTAssertNil(model.selectedTarget)
    }
}
