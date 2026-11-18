import Foundation

/// Runs the bundled Python conversion engine and turns its JSON-lines output
/// into events. The engine lives in Recast.app/Contents/Resources.
final class EngineClient {
    static let shared = EngineClient()

    let pythonURL: URL
    let enginePath: URL

    private init() {
        let res = Bundle.main.resourceURL!
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
        return env
    }

    private func makeProcess(_ args: [String]) -> (Process, Pipe) {
        let p = Process()
        p.executableURL = pythonURL
        p.arguments = ["-m", "recast_engine"] + args
        p.environment = environment()
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        return (p, out)
    }

    /// Run a short engine command (status / targets) and return its last JSON object.
    /// Output is drained while the process runs (no pipe-buffer deadlock) and a
    /// watchdog stops it after `timeout` seconds.
    func runOnce(_ args: [String], timeout: TimeInterval = 30) async throws -> [String: Any] {
        let (p, out) = makeProcess(args)
        try p.run()
        let watchdog = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global().asyncAfter(deadline: .now() + timeout, execute: watchdog)

        let data: Data = await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let d = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                cont.resume(returning: d)
            }
        }
        watchdog.cancel()
        let line = String(decoding: data, as: UTF8.self)
            .split(separator: "\n").last.map(String.init) ?? ""
        guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else {
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
                onExit: @escaping () -> Void) throws -> Process {
        let (p, out) = makeProcess(args)
        try p.run()
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
            DispatchQueue.main.async { onExit() }
        }
        return p
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
