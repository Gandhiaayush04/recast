import Foundation

/// A running engine process, as far as the app cares. `Process` conforms, and tests use a fake.
protocol EngineHandle: AnyObject {
    var isRunning: Bool { get }
    func terminate()
    /// The last few KB the engine wrote to stderr (Python tracebacks end up here).
    var stderrTail: String { get }
}

/// Keeps the last `limit` bytes of a stream, filled by a background reader.
final class TailBuffer {
    private let limit: Int
    private var data = Data()
    private let lock = NSLock()
    private let drained = DispatchGroup()
    init(limit: Int = 8192) { self.limit = limit }

    func append(_ d: Data) {
        lock.lock(); defer { lock.unlock() }
        data.append(d)
        if data.count > limit {
            data = Data(data.suffix(limit))
            // don't start in the middle of a multi-byte character
            while let f = data.first, f & 0xC0 == 0x80 { data.removeFirst() }
        }
    }

    var text: String {
        lock.lock(); defer { lock.unlock() }
        return String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Read `fh` on a background thread until it closes, so a chatty engine can never fill the pipe.
    func drain(_ fh: FileHandle) {
        drained.enter()
        DispatchQueue.global(qos: .utility).async { [self] in
            while true {
                let d = fh.availableData
                if d.isEmpty { break }
                append(d)
            }
            drained.leave()
        }
    }

    /// Wait (briefly) for the reader to reach end-of-stream, so `text` is complete after an exit.
    func waitForEnd(timeout: TimeInterval = 0.3) { _ = drained.wait(timeout: .now() + timeout) }
}

/// A real engine process plus what it has said on stderr.
final class EngineProcess: EngineHandle {
    let process: Process
    let stderr: TailBuffer
    init(process: Process, stderr: TailBuffer) { self.process = process; self.stderr = stderr }
    var isRunning: Bool { process.isRunning }
    func terminate() { process.terminate() }
    var stderrTail: String { stderr.text }
}

/// What AppModel needs from the engine. EngineClient is the real thing; tests supply a fake.
protocol EngineRunning {
    func runOnce(_ args: [String], timeout: TimeInterval) async throws -> [String: Any]
    func stream(_ args: [String],
                onEvent: @escaping ([String: Any]) -> Void,
                onExit: @escaping () -> Void) throws -> EngineHandle
}

/// Runs the bundled Python conversion engine and turns its JSON-lines output
/// into events. The engine lives in Recast.app/Contents/Resources.
final class EngineClient: EngineRunning {
    static let shared = EngineClient()

    let pythonURL: URL
    let enginePath: URL

    private init() {
        // resourceURL is only nil for a broken bundle; fall back so we fail with a normal
        // "engine couldn't start" message instead of crashing.
        let res = Bundle.main.resourceURL ?? Bundle.main.bundleURL
        pythonURL = res.appendingPathComponent("python/bin/python3")
        enginePath = res.appendingPathComponent("engine")
    }

    /// GUI apps get a tiny PATH; give the engine (and brew) the usual Homebrew locations.
    static let searchPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

    private func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = enginePath.path
        env["PATH"] = Self.searchPath
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["RECAST_OUTPUT_DIR"] = nil   // the app passes --out explicitly; a stray variable must not redirect output
        return env
    }

    /// Build and start an engine process. stderr is captured into the returned buffer.
    /// If the process can't start, every pipe is closed so nothing leaks, and the error is logged.
    private func launch(_ args: [String]) throws -> (Process, Pipe, TailBuffer) {
        let p = Process()
        p.executableURL = pythonURL
        p.arguments = ["-m", "recast_engine"] + args
        p.environment = environment()
        let out = Pipe()
        let err = Pipe()
        p.standardOutput = out
        p.standardError = err
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            DiagnosticsLog.shared.log("couldn't start the engine (\(args.first ?? "?")): \(error.localizedDescription)")
            for h in [out.fileHandleForReading, out.fileHandleForWriting,
                      err.fileHandleForReading, err.fileHandleForWriting] { try? h.close() }
            throw error
        }
        let tail = TailBuffer()
        tail.drain(err.fileHandleForReading)
        return (p, out, tail)
    }

    /// Run a short engine command (status / targets) and return its last JSON object.
    /// Output is drained while the process runs (no pipe-buffer deadlock) and a
    /// watchdog stops it after `timeout` seconds.
    func runOnce(_ args: [String], timeout: TimeInterval = 30) async throws -> [String: Any] {
        let (p, out, tail) = try launch(args)
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)

        let data: Data = await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let d = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                tail.waitForEnd()
                cont.resume(returning: d)
            }
        }
        watchdog.cancel()
        let line = String(decoding: data, as: UTF8.self)
            .split(separator: "\n").last.map(String.init) ?? ""
        guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else {
            DiagnosticsLog.shared.log("engine \(args.first ?? "?") gave no usable answer. stderr: \(tail.text)")
            throw NSError(domain: "Recast", code: 1, userInfo: [
                NSLocalizedDescriptionKey: "Recast's conversion engine didn't respond."])
        }
        return obj
    }

    /// Start a streaming engine command (convert / combine). Every JSON line is
    /// delivered to `onEvent` on the main thread, in order; `onExit` is always
    /// delivered last, after the final event.
    func stream(_ args: [String],
                onEvent: @escaping ([String: Any]) -> Void,
                onExit: @escaping () -> Void) throws -> EngineHandle {
        let (p, out, tail) = try launch(args)
        let handle = EngineProcess(process: p, stderr: tail)
        DispatchQueue(label: "recast.engine.stream").async {
            let fh = out.fileHandleForReading
            var buf = Data()
            while true {
                let chunk = fh.availableData        // blocks; empty means EOF
                if chunk.isEmpty { break }
                buf.append(chunk)
                while let nl = buf.firstIndex(of: 0x0A) {
                    let lineData = Data(buf[buf.startIndex..<nl])
                    buf.removeSubrange(buf.startIndex...nl)
                    if let obj = try? JSONSerialization.jsonObject(with: lineData) as? [String: Any] {
                        DispatchQueue.main.async { onEvent(obj) }
                    }
                }
            }
            if !buf.isEmpty, let obj = try? JSONSerialization.jsonObject(with: buf) as? [String: Any] {
                DispatchQueue.main.async { onEvent(obj) }
            }
            p.waitUntilExit()
            tail.waitForEnd()
            if p.terminationStatus != 0 {
                DiagnosticsLog.shared.log("engine \(args.first ?? "?") exited with status \(p.terminationStatus). stderr: \(tail.text)")
            }
            DispatchQueue.main.async { onExit() }
        }
        return handle
    }

    /// Install a missing engine with Homebrew, streaming its output line by line.
    /// Returns the running process (nil if it couldn't start) so it can be stopped on quit.
    @discardableResult
    func installEngine(_ engine: String,
                       onLine: @escaping (String) -> Void,
                       onExit: @escaping (Bool) -> Void) -> Process? {
        let brewPaths = ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"]
        guard let brew = brewPaths.first(where: { FileManager.default.isExecutableFile(atPath: $0) }) else {
            onLine("Homebrew isn't installed. Run 1-install-engines.command in the Recast project folder first.")
            onExit(false)
            return nil
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: brew)
        p.arguments = engine == "libreoffice" ? ["install", "--cask", "libreoffice"] : ["install", "ffmpeg"]
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = Self.searchPath
        env["HOMEBREW_NO_AUTO_UPDATE"] = "1"
        env["NONINTERACTIVE"] = "1"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        do { try p.run() } catch {
            onLine("Couldn't start Homebrew: \(error.localizedDescription)")
            onExit(false)
            return nil
        }
        DispatchQueue(label: "recast.brew").async {
            let fh = pipe.fileHandleForReading
            var pending = ""
            while true {
                let chunk = fh.availableData
                if chunk.isEmpty { break }
                pending += String(decoding: chunk, as: UTF8.self)
                // brew/curl redraw progress with \r; treat it like a newline
                var parts = pending.components(separatedBy: CharacterSet(charactersIn: "\r\n"))
                pending = parts.removeLast()
                for line in parts where !line.trimmingCharacters(in: .whitespaces).isEmpty {
                    DispatchQueue.main.async { onLine(line) }
                }
            }
            if !pending.isEmpty { DispatchQueue.main.async { onLine(pending) } }
            p.waitUntilExit()
            let ok = p.terminationStatus == 0
            DispatchQueue.main.async { onExit(ok) }
        }
        return p
    }
}
