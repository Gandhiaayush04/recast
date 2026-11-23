import AppKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers

enum FileStatus: Equatable {
    case ready
    case unsupported
    case queued
    case converting(Double)
    case done
    case skipped(String)
    case failed(String, missingEngine: String?)
}

struct FileItem: Identifiable, Equatable {
    let id = UUID()
    let url: URL
    let kind: String       // e.g. "HEIF image", from macOS's own type names
    let size: String       // e.g. "3.2 MB"
    var status: FileStatus = .ready
    var outputs: [URL] = []
    var combined = false   // true when this file went into a combined PDF

    var name: String { url.lastPathComponent }
    var ext: String { url.pathExtension.lowercased() }

    init(url: URL) {
        self.url = url
        let type = UTType(filenameExtension: url.pathExtension)
        let desc = type?.localizedDescription ?? "\(url.pathExtension.uppercased()) file"
        self.kind = desc.prefix(1).uppercased() + desc.dropFirst()
        let bytes = (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0
        self.size = ByteCountFormatter.string(fromByteCount: Int64(bytes), countStyle: .file)
    }
}

// MARK: - Format menu: grouping and plain-English hints

enum FormatGroup: String, CaseIterable {
    case image = "Image", video = "Video", audio = "Audio", document = "Document"
}

enum FormatInfo {
    static let groups: [String: FormatGroup] = [
        "jpg": .image, "png": .image, "heic": .image, "webp": .image, "tiff": .image, "bmp": .image, "gif": .image,
        "mp4": .video, "mov": .video, "mkv": .video, "webm": .video, "avi": .video,
        "mp3": .audio, "wav": .audio, "m4a": .audio, "aac": .audio, "flac": .audio, "ogg": .audio,
        "docx": .document, "odt": .document, "rtf": .document, "txt": .document, "pptx": .document,
        "odp": .document, "xlsx": .document, "ods": .document, "csv": .document, "pdf": .document,
    ]

    /// A short note shown next to each format so people can pick without knowing the formats.
    static func hint(for ext: String, sourceIsPDF: Bool, sourceIsVideo: Bool) -> String {
        if sourceIsPDF && (ext == "png" || ext == "jpg") { return "One image per page" }
        if sourceIsVideo && ext == "gif" { return "Short silent loop" }
        switch ext {
        case "jpg": return "Smallest photos"
        case "png": return "Keeps transparency"
        case "heic": return "iPhone photo format"
        case "webp": return "Web friendly"
        case "tiff": return "For printing"
        case "bmp": return "Uncompressed"
        case "gif": return "Simple animations"
        case "pdf": return "Opens anywhere"
        case "mp4": return "Plays everywhere"
        case "mov": return "Apple QuickTime"
        case "mkv": return "Flexible video file"
        case "webm": return "For the web"
        case "avi": return "For older players"
        case "mp3": return "Plays everywhere"
        case "m4a": return "Apple Music and iPhone"
        case "wav": return "Uncompressed"
        case "aac": return "Compact"
        case "flac": return "Lossless"
        case "ogg": return "Open format"
        case "docx": return "Microsoft Word"
        case "odt": return "LibreOffice Writer"
        case "rtf": return "Basic formatting"
        case "txt": return "Text only"
        case "pptx": return "PowerPoint"
        case "odp": return "LibreOffice Impress"
        case "xlsx": return "Excel"
        case "ods": return "LibreOffice Calc"
        case "csv": return "Plain table data"
        default: return ""
        }
    }
}

enum Appearance: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }
    var label: String {
        switch self {
        case .system: return "Match System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

    @MainActor func apply() {
        switch self {
        case .system: NSApp.appearance = nil
        case .light: NSApp.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp.appearance = NSAppearance(named: .darkAqua)
        }
    }

    @MainActor static var systemIsDark: Bool {
        NSApp.effectiveAppearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
    }
}

struct TargetFormat: Identifiable, Hashable {
    let ext: String
    let label: String
    var id: String { ext }
}

enum Quality: String, CaseIterable, Identifiable {
    case smaller, balanced, best
    var id: String { rawValue }
    var label: String {
        switch self {
        case .smaller: return "Smaller file"
        case .balanced: return "Balanced"
        case .best: return "Best quality"
        }
    }
    var shortLabel: String {
        switch self {
        case .smaller: return "Smaller"
        case .balanced: return "Balanced"
        case .best: return "Best"
        }
    }
}

enum Resolution: String, CaseIterable, Identifiable {
    case original, p1080 = "1080p", p720 = "720p"
    var id: String { rawValue }
    var label: String { self == .original ? "Original" : rawValue }
}

private let videoExts: Set<String> = ["mp4", "mov", "mkv", "webm", "avi", "m4v", "qt"]
private let multiPageExts: Set<String> = ["pdf", "xlsx", "ods"]

@MainActor
final class AppModel: ObservableObject {
    @Published var items: [FileItem] = []
    @Published var targets: [TargetFormat] = []
    @Published var canCombinePDF = false
    @Published var mixedNoCommon = false
    @Published var targetsLoading = false
    @Published var quality: Quality = .balanced
    @Published var resolution: Resolution = .original
    @Published var putInOneFolder = false
    @Published var isWorking = false
    @Published var isStopping = false
    @Published var wasStopped = false
    @Published var lastTarget: String?
    @Published var combinedOutput: URL?
    @Published var combineError: String?
    @Published var engineError: String?
    @Published var missingEngines: [String] = []   // "ffmpeg", "libreoffice"
    @Published var notice: String?                  // short one-off message (e.g. folders skipped)
    @Published var selectedTarget: String?          // the format chosen in "Convert to"
    @Published var combinePDF = false               // "Combine into one PDF" checkbox
    @Published var selection = Set<UUID>()

    // Install sheet
    @Published var installing: String?
    @Published var installLog: [String] = []
    @Published var installFinished: Bool?

    private let engine: EngineRunning
    private var running: EngineHandle?
    private var installProcess: Process?
    private var batchIDs: [Int: UUID] = [:]
    private var pendingAdds: [URL] = []
    private var cancelled = false
    private var targetsGeneration = 0
    private var pendingNoticeShown = false

    init(engine: EngineRunning = EngineClient.shared) {
        self.engine = engine
    }

    // MARK: derived

    var hasVideo: Bool { items.contains { videoExts.contains($0.ext) } }
    var hasMultiPage: Bool { items.contains { multiPageExts.contains($0.ext) } }
    var allImages: Bool { !convertible.isEmpty && convertible.allSatisfy { FormatInfo.groups[$0.ext == "jpeg" ? "jpg" : $0.ext] == .image } }
    var sourceIsPDF: Bool { convertible.contains { $0.ext == "pdf" } }
    var showsCombine: Bool { selectedTarget == "pdf" && canCombinePDF }
    var willCombine: Bool { showsCombine && combinePDF }
    var showsResolution: Bool { hasVideo && FormatInfo.groups[selectedTarget ?? ""] == .video }
    var showsFolderOption: Bool { hasMultiPage && ["png", "jpg", "csv"].contains(selectedTarget ?? "") }
    var canConvert: Bool { !isWorking && !targetsLoading && selectedTarget != nil && !convertible.isEmpty }

    /// Window subtitle: a short summary of the list and the last run.
    var subtitle: String {
        if items.isEmpty { return "Drop files to convert" }
        let n = items.count == 1 ? "1 file" : "\(items.count) files"
        if isWorking { return "\(n), converting…" }
        var parts = [n]
        if combinedOutput != nil { parts.append("combined into one PDF") }
        else if doneCount > 0 { parts.append("\(doneCount) converted") }
        if failedCount > 0 { parts.append("\(failedCount) failed") }
        if wasStopped { parts.append("stopped") }
        return parts.joined(separator: ", ")
    }

    func removeSelected() {
        guard !isWorking, !selection.isEmpty else { return }
        items.removeAll { selection.contains($0.id) }
        selection.removeAll()
        Task { await refreshTargets() }
    }

    func convertSelectedTarget() {
        guard let t = selectedTarget else { return }
        if willCombine { combineIntoPDF() } else { convert(to: t) }
    }
    var convertible: [FileItem] { items.filter { $0.status != .unsupported } }
    var allOutputs: [URL] {
        var urls = items.filter { !$0.combined }.flatMap(\.outputs)
        if let c = combinedOutput { urls.append(c) }
        return urls
    }
    var doneCount: Int { items.filter { $0.status == .done }.count }
    var skippedCount: Int { items.filter { if case .skipped = $0.status { return true }; return false }.count }
    var failedCount: Int { items.filter { if case .failed = $0.status { return true }; return false }.count }

    // MARK: engine health

    func checkEngines() async {
        do {
            let s = try await engine.runOnce(["status"], timeout: 30)
            let engines = s["engines"] as? [String: Bool] ?? [:]
            missingEngines = ["ffmpeg", "libreoffice"].filter { engines[$0] == false }
            engineError = nil
        } catch {
            engineError = "Recast's conversion engine couldn't start."
        }
    }

    // MARK: files

    func add(_ urls: [URL]) {
        if isWorking {             // don't disturb a running conversion; add when it finishes
            pendingAdds += urls
            pendingNoticeShown = true
            notice = "Files will be added when the current conversion finishes."
            return
        }
        var skippedFolders = 0
        var newOnes: [URL] = []
        for raw in urls where raw.isFileURL {
            let u = raw.standardizedFileURL
            var isDir: ObjCBool = false
            guard FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir) else { continue }
            if isDir.boolValue {
                // a dropped folder: take the files directly inside it (not subfolders)
                let kids = (try? FileManager.default.contentsOfDirectory(
                    at: u, includingPropertiesForKeys: [.isRegularFileKey],
                    options: [.skipsHiddenFiles, .skipsSubdirectoryDescendants])) ?? []
                let files = kids.filter { (try? $0.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile) == true }
                if files.isEmpty { skippedFolders += 1 }
                newOnes += files.map(\.standardizedFileURL)
            } else {
                newOnes.append(u)
            }
        }
        var added = false
        for u in newOnes where !items.contains(where: { $0.url == u }) {
            items.append(FileItem(url: u))
            added = true
        }
        notice = skippedFolders > 0 ? "Empty folders were skipped." : nil
        if added { Task { await refreshTargets() } }
    }

    func remove(_ item: FileItem) {
        guard !isWorking else { return }
        items.removeAll { $0.id == item.id }
        selection.remove(item.id)
        Task { await refreshTargets() }
    }

    func clear() {
        guard !isWorking else { return }
        items.removeAll()
        selection.removeAll()
        selectedTarget = nil
        combinePDF = false
        targets = []
        canCombinePDF = false
        mixedNoCommon = false
        combinedOutput = nil
        combineError = nil
        lastTarget = nil
        notice = nil
        wasStopped = false
        targetsGeneration += 1
    }

    func move(from: IndexSet, to: Int) {
        guard !isWorking else { return }
        items.move(fromOffsets: from, toOffset: to)
    }

    func sortAZ() {
        guard !isWorking else { return }
        items.sort { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    func browse() {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        panel.prompt = "Add"
        if panel.runModal() == .OK { add(panel.urls) }
    }

    private func resetResults() {
        combinedOutput = nil
        combineError = nil
        wasStopped = false
        for i in items.indices where items[i].status != .unsupported {
            items[i].status = .ready
            items[i].outputs = []
            items[i].combined = false
        }
    }

    func refreshTargets() async {
        targetsGeneration += 1
        let gen = targetsGeneration
        guard !items.isEmpty else {
            targets = []; canCombinePDF = false; mixedNoCommon = false; targetsLoading = false; selectedTarget = nil
            return
        }
        targetsLoading = true
        let paths = items.map(\.url.path)
        do {
            let r = try await engine.runOnce(["targets"] + paths, timeout: 30)
            guard gen == targetsGeneration else { return }   // a newer request superseded this one
            let t = r["targets"] as? [[String: Any]] ?? []
            targets = t.compactMap {
                guard let e = $0["ext"] as? String else { return nil }
                return TargetFormat(ext: e, label: $0["label"] as? String ?? e.uppercased())
            }
            canCombinePDF = r["can_combine_pdf"] as? Bool ?? false
            if !targets.contains(where: { $0.ext == selectedTarget }) { selectedTarget = targets.first?.ext }
            mixedNoCommon = r["mixed_no_common"] as? Bool ?? false
            let unsupported = Set(r["unsupported_files"] as? [String] ?? [])
            for i in items.indices {
                if unsupported.contains(items[i].url.path) { items[i].status = .unsupported }
                else if items[i].status == .unsupported { items[i].status = .ready }
            }
            engineError = nil
        } catch {
            guard gen == targetsGeneration else { return }
            engineError = error.localizedDescription
        }
        targetsLoading = false
    }

    // MARK: converting

    func convert(to target: String) {
        let files = convertible
        guard !files.isEmpty, !isWorking, !targetsLoading else { return }
        var args = ["convert", "--to", target, "--quality", quality.rawValue, "--resolution", resolution.rawValue]
        if putInOneFolder { args.append("--folder") }
        args += files.map(\.url.path)
        let ids = Dictionary(uniqueKeysWithValues: files.enumerated().map { ($0.offset, $0.element.id) })
        start(args, combining: false) {
            self.lastTarget = target
            self.batchIDs = ids
        }
    }

    func combineIntoPDF() {
        let files = convertible
        guard files.count > 1, !isWorking, !targetsLoading else { return }
        start(["combine", "--quality", quality.rawValue] + files.map(\.url.path), combining: true) {
            self.lastTarget = nil
            self.batchIDs = [:]
        }
    }

    private func start(_ args: [String], combining: Bool, prepare: () -> Void) {
        cancelled = false
        do {
            running = try engine.stream(args, onEvent: { [weak self] ev in
                self?.handle(ev, combining: combining)
            }, onExit: { [weak self] in
                self?.finish()
            })
        } catch {
            engineError = "Recast's conversion engine couldn't start."
            return
        }
        // only touch previous results once the engine is actually running
        resetResults()
        prepare()
        isWorking = true
        notice = nil
    }

    private func finish() {
        for i in items.indices {
            switch items[i].status {
            case .queued, .converting:
                items[i].status = cancelled
                    ? .ready
                    : .failed("The conversion stopped unexpectedly.", missingEngine: nil)
            default: break
            }
        }
        wasStopped = cancelled
        cancelled = false
        isWorking = false
        isStopping = false
        running = nil
        batchIDs = [:]
        if !pendingAdds.isEmpty {
            let urls = pendingAdds
            pendingAdds = []
            add(urls)
        } else if pendingNoticeShown {
            notice = nil
        }
        pendingNoticeShown = false
    }

    private func handle(_ ev: [String: Any], combining: Bool) {
        let kind = ev["event"] as? String ?? ""
        if combining {
            let rows = items.indices.filter { items[$0].status != .unsupported }
            switch kind {
            case "started":
                for i in rows { items[i].status = .converting(0) }
            case "progress":
                let p = ev["progress"] as? Double ?? 0
                for i in rows { items[i].status = .converting(p) }
            case "done":
                let out = (ev["outputs"] as? [String])?.first.map { URL(fileURLWithPath: $0) }
                combinedOutput = out
                for i in rows {
                    items[i].status = .done
                    items[i].combined = true
                    items[i].outputs = out.map { [$0] } ?? []
                }
            case "failed":
                combineError = ev["message"] as? String ?? "Couldn't combine these images."
                for i in rows { items[i].status = .ready }
            default: break
            }
            return
        }
        guard let n = ev["id"] as? Int, let uid = batchIDs[n],
              let idx = items.firstIndex(where: { $0.id == uid }) else { return }
        switch kind {
        case "queued": items[idx].status = .queued
        case "started": items[idx].status = .converting(0)
        case "progress": items[idx].status = .converting(ev["progress"] as? Double ?? 0)
        case "done":
            items[idx].status = .done
            items[idx].outputs = (ev["outputs"] as? [String] ?? []).map { URL(fileURLWithPath: $0) }
        case "skipped":
            items[idx].status = .skipped(ev["message"] as? String ?? "Already in that format.")
        case "failed":
            let missing = ev["missing_engine"] as? String
            items[idx].status = .failed(ev["message"] as? String ?? "Couldn't convert this file.", missingEngine: missing)
            if let m = missing, !missingEngines.contains(m) { missingEngines.append(m) }
        default: break
        }
    }

    func cancel() {
        guard let p = running, p.isRunning, !isStopping else { return }
        cancelled = true
        isStopping = true
        p.terminate()
    }

    /// Called when the app quits: stop work and give the engine a moment to clean up.
    func shutdown() {
        if let p = running, p.isRunning {
            cancelled = true
            p.terminate()
            let deadline = Date().addingTimeInterval(2)
            while p.isRunning && Date() < deadline { usleep(50_000) }
        }
        if let b = installProcess, b.isRunning { b.terminate() }
    }

    // MARK: Finder

    func reveal(_ urls: [URL]) {
        guard !urls.isEmpty else { return }
        NSWorkspace.shared.activateFileViewerSelecting(urls)
    }

    // MARK: engine install

    func install(_ engine: String) {
        guard installing == nil else { return }
        installing = engine
        installLog = []
        installFinished = nil
        installProcess = EngineClient.shared.installEngine(engine, onLine: { [weak self] line in
            self?.installLog.append(line)
        }, onExit: { [weak self] ok in
            guard let self else { return }
            self.installFinished = ok
            self.installProcess = nil
            if ok {
                self.missingEngines.removeAll { $0 == engine }
                // rows that failed only because this engine was missing can be retried
                for i in self.items.indices {
                    if case .failed(_, let m) = self.items[i].status, m == engine {
                        self.items[i].status = .ready
                    }
                }
            }
            Task { await self.checkEngines() }
        })
    }
}
