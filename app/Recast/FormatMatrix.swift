import Foundation

/// Which formats can become which. This is a direct port of engine/recast_engine/formats.py,
/// reading the same data (Resources/formats.json, generated from the engine) so the app's
/// menus can't drift from what the engine accepts. engine/tests/test_swift_parity.py and
/// FormatMatrixTests keep the two in step.
struct FormatMatrix {
    struct Analysis: Equatable {
        var targets: [TargetFormat] = []
        var unsupportedPaths: [String] = []
        var canCombinePDF = false
        var mixedNoCommon = false
    }

    private let groups: [String: [String]]
    private let aliases: [String: String]
    private let labels: [String: String]
    private let targets: [String: [String]]
    private let groupOfExt: [String: String]

    init(data: Data) throws {
        struct Raw: Decodable {
            let groups: [String: [String]]
            let aliases: [String: String]
            let labels: [String: String]
            let targets: [String: [String]]
        }
        let raw = try JSONDecoder().decode(Raw.self, from: data)
        groups = raw.groups; aliases = raw.aliases; labels = raw.labels; targets = raw.targets
        var map: [String: String] = [:]
        for (g, exts) in raw.groups { for e in exts { map[e] = g } }
        groupOfExt = map
    }

    /// The matrix bundled with the app. A missing or unreadable file is a packaging bug.
    static let shared: FormatMatrix = {
        let bundles = [Bundle.main, Bundle(for: BundleToken.self)]
        for b in bundles {
            if let url = b.url(forResource: "formats", withExtension: "json"),
               let data = try? Data(contentsOf: url), let m = try? FormatMatrix(data: data) { return m }
        }
        DiagnosticsLog.shared.log("formats.json is missing from the app bundle")
        return try! FormatMatrix(data: Data(#"{"groups":{},"aliases":{},"labels":{},"targets":{}}"#.utf8))
    }()
    private final class BundleToken {}

    /// False when the bundled formats.json couldn't be loaded (a packaging bug).
    var isEmpty: Bool { groupOfExt.isEmpty }
    var allExtensions: [String] { groupOfExt.keys.sorted() }

    func normalize(_ ext: String) -> String {
        var e = ext.lowercased()
        if e.hasPrefix(".") { e.removeFirst() }
        return aliases[e] ?? e
    }

    /// "image", "video", "audio", "doc_text", "doc_slides", "doc_sheet" or "pdf"; nil when unsupported.
    func group(of ext: String) -> String? { groupOfExt[normalize(ext)] }

    func targetsFor(_ ext: String) -> [String] { targets[normalize(ext)] ?? [] }

    func label(for ext: String) -> String { labels[ext] ?? ext.uppercased() }

    /// Targets valid for every file (order follows the first file); translation of `common_targets`.
    func commonTargets(_ exts: [String]) -> [String] {
        guard !exts.isEmpty else { return [] }
        let norm = exts.map(normalize)
        let candidates = targetsFor(norm[0]) + [norm[0]]
        return candidates.filter { t in
            norm.allSatisfy { t == $0 || targetsFor($0).contains(t) } && norm.contains { t != $0 }
        }
    }

    /// Translation of `analyze_targets` in the engine's cli.py.
    func analyze(paths: [String]) -> Analysis {
        let exts = paths.map { normalize(URL(fileURLWithPath: $0).pathExtension) }
        var known: [String] = []
        var result = Analysis()
        for (p, e) in zip(paths, exts) {
            if groupOfExt[e] == nil { result.unsupportedPaths.append(p) } else { known.append(e) }
        }
        let common = known.isEmpty ? [] : commonTargets(known)
        result.targets = common.map { TargetFormat(ext: $0, label: label(for: $0)) }
        result.canCombinePDF = known.count > 1 && known.allSatisfy { groupOfExt[$0] == "image" }
        result.mixedNoCommon = known.count > 1 && common.isEmpty
        return result
    }
}
