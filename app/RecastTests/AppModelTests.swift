import XCTest
@testable import Recast

@MainActor
final class AppModelTests: XCTestCase {
    var dir: URL!
    var engine: FakeEngine!
    var model: AppModel!
    var defaults: UserDefaults!
    var suiteName = ""

    override func setUp() async throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("recast-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        engine = FakeEngine()
        suiteName = "recast-tests-\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)!
        model = AppModel(engine: engine, defaults: defaults)
    }

    override func tearDown() async throws {
        try? FileManager.default.removeItem(at: dir)
        defaults.removePersistentDomain(forName: suiteName)
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

    func testAddingAFileComputesTargetsWithoutStartingTheEngine() async throws {
        model.add([try makeFile("a.jpg")])
        XCTAssertTrue(engine.runOnceCalls.isEmpty)      // instant: no engine process
        XCTAssertEqual(model.targets.map(\.ext), ["png", "heic", "webp", "tiff", "bmp", "gif", "pdf"])
        XCTAssertEqual(model.selectedTarget, "png")
    }

    func testAliasedExtensionsAreUnderstood() async throws {
        model.add([try makeFile("a.heif"), try makeFile("b.tif")])
        XCTAssertFalse(model.targets.isEmpty)
        XCTAssertTrue(model.allImages)
        XCTAssertTrue(model.canCombinePDF)
    }

    func testMixedFilesWithNothingInCommon() async throws {
        model.add([try makeFile("a.jpg"), try makeFile("b.mp3")])
        XCTAssertTrue(model.mixedNoCommon)
        XCTAssertTrue(model.targets.isEmpty)
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

    func testUnsupportedFilesAreMarked() async throws {
        model.add([try makeFile("a.xyz")]); await settle()
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

    // MARK: file list

    func testFilePathsGoInAListFileNotOnTheCommandLine() async throws {
        try await startRun(["a.jpg", "b.jpg"])
        let args = engine.streamCalls.last ?? []
        XCTAssertFalse(args.contains { $0.hasSuffix("a.jpg") })
        let i = try XCTUnwrap(args.firstIndex(of: "--files-from"))
        let list = URL(fileURLWithPath: args[i + 1])
        let paths = try Data(contentsOf: list).split(separator: 0).map { String(decoding: $0, as: UTF8.self) }
        XCTAssertEqual(paths.map { URL(fileURLWithPath: $0).lastPathComponent }, ["a.jpg", "b.jpg"])
        engine.exit()
        XCTAssertFalse(FileManager.default.fileExists(atPath: list.path))   // cleaned up
    }

    func testEngineErrorBannerClearsOnceARunStarts() async throws {
        model.add([try makeFile("a.jpg")])
        model.engineError = "Recast's conversion engine couldn't start."
        model.convert(to: "png")
        XCTAssertNil(model.engineError)
    }

    // MARK: output folder

    func testDefaultsToDownloads() {
        XCTAssertTrue(model.usesDefaultOutputFolder)
        XCTAssertEqual(model.outputFolder.standardizedFileURL, AppModel.downloads.standardizedFileURL)
    }

    func testConvertPassesChosenFolderToEngine() async throws {
        let out = dir.appendingPathComponent("out")
        try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
        model.setOutputFolder(out)
        try await startRun(["a.jpg"])
        let args = engine.streamCalls.last ?? []
        let i = try XCTUnwrap(args.firstIndex(of: "--out"))
        XCTAssertEqual(args[i + 1], out.standardizedFileURL.path)
        XCTAssertFalse(model.usesDefaultOutputFolder)
    }

    func testCombinePassesChosenFolderToEngine() async throws {
        model.setOutputFolder(dir)
        model.add([try makeFile("a.jpg"), try makeFile("b.jpg")]); await settle()
        model.combineIntoPDF()
        XCTAssertTrue(engine.streamCalls.last?.contains("--out") ?? false)
    }

    func testChoiceIsRemembered() {
        model.setOutputFolder(dir)
        let again = AppModel(engine: engine, defaults: defaults)
        XCTAssertEqual(again.outputFolder.standardizedFileURL, dir.standardizedFileURL)
    }

    func testMissingFolderFallsBackToDownloads() {
        model.setOutputFolder(dir.appendingPathComponent("gone"))
        XCTAssertEqual(model.outputFolder.standardizedFileURL, AppModel.downloads.standardizedFileURL)
    }

    func testUnwritableFolderBlocksTheRunWithAMessage() async throws {
        try XCTSkipIf(getuid() == 0, "root can write anywhere")
        let ro = dir.appendingPathComponent("ro")
        try FileManager.default.createDirectory(at: ro, withIntermediateDirectories: true)
        try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: ro.path)
        defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: ro.path) }
        model.setOutputFolder(ro)
        model.add([try makeFile("a.jpg")]); await settle()
        model.convert(to: "png")
        XCTAssertFalse(model.isWorking)
        XCTAssertTrue(engine.streamCalls.isEmpty)
        XCTAssertNotNil(model.notice)
    }

    func testVanishedFolderBlocksTheRunWithAMessage() async throws {
        model.add([try makeFile("a.jpg")]); await settle()
        model.setOutputFolder(dir.appendingPathComponent("unplugged"))
        model.convert(to: "png")
        XCTAssertFalse(model.isWorking)
        XCTAssertTrue(model.notice?.contains("unplugged") ?? false)
    }

    func testResetGoesBackToDownloads() {
        model.setOutputFolder(dir)
        model.setOutputFolder(nil)
        XCTAssertTrue(model.usesDefaultOutputFolder)
    }

    // MARK: diagnostics

    func testFailedEventKeepsDetailAndTrace() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "failed", "id": 0, "message": "Something went wrong converting this file.",
                     "detail": "ValueError: boom", "trace": "Traceback line"])
        let item = model.items[0]
        XCTAssertEqual(model.failureDetails[item.id], "ValueError: boom\n\nTraceback line")
        let report = model.failureReport(for: item)
        XCTAssertTrue(report.contains("ValueError: boom"))
        XCTAssertTrue(report.contains("a.jpg"))
        XCTAssertTrue(report.contains("contains file names"))
    }

    func testEarlyExitRecordsEngineStderr() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "started", "id": 0])
        engine.handle.stderrTail = "Traceback: ImportError PIL"
        engine.exit()
        let d = model.failureDetails[model.items[0].id] ?? ""
        XCTAssertTrue(d.contains("ImportError PIL"))
    }

    func testCombineFailureKeepsDetailForCopying() async throws {
        model.add([try makeFile("a.jpg"), try makeFile("b.jpg")]); await settle()
        model.combineIntoPDF()
        engine.send(["event": "failed", "id": 0, "message": "Boom", "detail": "OSError: disk", "trace": "tb"])
        XCTAssertEqual(model.combineDetail, "OSError: disk\n\ntb")
    }

    func testRemovingARowDropsItsDetails() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "failed", "id": 0, "message": "x", "detail": "d"])
        engine.exit()
        model.remove(model.items[0])
        XCTAssertTrue(model.failureDetails.isEmpty)
    }

    func testRerunClearsOldDetails() async throws {
        try await startRun(["a.jpg"])
        engine.send(["event": "failed", "id": 0, "message": "x", "detail": "d"])
        engine.exit()
        XCTAssertFalse(model.failureDetails.isEmpty)
        model.convert(to: "png")
        XCTAssertTrue(model.failureDetails.isEmpty)
    }
}

final class DiagnosticsLogTests: XCTestCase {
    func testWritesAndRotates() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("recast-log-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let log = DiagnosticsLog(directory: dir, maxBytes: 200)
        for i in 0..<30 { log.log("line number \(i) with some padding to grow the file") }
        log.flush()
        let names = Set(try FileManager.default.contentsOfDirectory(atPath: dir.path))
        XCTAssertTrue(names.contains("recast.log"))
        XCTAssertTrue(names.contains("recast.log.1"))
        XCTAssertFalse(names.contains("recast.log.3"))
        let current = try String(contentsOf: log.url, encoding: .utf8)
        XCTAssertTrue(current.contains("line number 29"))
    }
}

final class TailBufferTests: XCTestCase {
    func testKeepsOnlyTheEndAndNeverStartsMidCharacter() {
        let t = TailBuffer(limit: 10)
        t.append(Data(String(repeating: "é", count: 20).utf8))   // 40 bytes, 2 per character
        XCTAssertFalse(t.text.contains("\u{FFFD}"))
        XCTAssertTrue(t.text.allSatisfy { $0 == "é" })
        XCTAssertLessThanOrEqual(t.text.utf8.count, 10)
    }

    func testDrainCollectsEverythingBeforeEnd() {
        let pipe = Pipe()
        let t = TailBuffer()
        t.drain(pipe.fileHandleForReading)
        pipe.fileHandleForWriting.write(Data("Traceback: boom".utf8))
        try? pipe.fileHandleForWriting.close()
        t.waitForEnd()
        XCTAssertEqual(t.text, "Traceback: boom")
    }
}
