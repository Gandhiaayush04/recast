import AppKit
import SwiftUI
import UniformTypeIdentifiers

// Layout follows the Finder list view: a table of files in the middle, the
// toolbar in the title bar, and one bottom bar with the conversion controls.
private enum Col {
    static let kind: CGFloat = 140
    static let size: CGFloat = 80
    static let status: CGFloat = 220
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @AppStorage("appearance") private var appearance: Appearance = .system
    @State private var dropTargeted = false

    var body: some View {
        VStack(spacing: 0) {
            if !model.missingEngines.isEmpty || model.engineError != nil {
                EngineBanner()
            }
            if model.items.isEmpty {
                EmptyState(targeted: dropTargeted)
            } else {
                FileTable()
            }
            Divider()
            ConvertBar()
        }
        .navigationTitle("Recast")
        .navigationSubtitle(model.subtitle)
        .toolbar { toolbarContent }
        .dropDestination(for: URL.self) { urls, _ in
            model.add(urls.filter(\.isFileURL))
            return true
        } isTargeted: { dropTargeted = $0 }
        .overlay {
            if dropTargeted && !model.items.isEmpty {
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.accentColor, lineWidth: 3)
                    .padding(4)
                    .allowsHitTesting(false)
            }
        }
        .sheet(item: Binding(
            get: { model.installing.map { InstallTarget(engine: $0) } },
            set: { if $0 == nil { model.installing = nil } })) { t in
            InstallSheet(engine: t.engine)
                .interactiveDismissDisabled(model.installFinished == nil)
        }
    }

    @ToolbarContentBuilder private var toolbarContent: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Button { model.browse() } label: { Label("Add Files", systemImage: "plus") }
                .help("Add files to convert (⌘O)")
            Button { model.removeSelected() } label: { Label("Remove", systemImage: "minus") }
                .help("Remove the selected files from the list (Delete). The files themselves aren't touched.")
                .disabled(model.selection.isEmpty || model.isWorking)
            Button { model.sortAZ() } label: { Label("Sort by Name", systemImage: "arrow.up.arrow.down") }
                .help("Sort the list by name. This is also the page order for Combine into one PDF.")
                .disabled(model.items.count < 2 || model.isWorking)
        }
        ToolbarItem(placement: .primaryAction) {
            Button {
                let darkNow = appearance == .dark || (appearance == .system && Appearance.systemIsDark)
                appearance = darkNow ? .light : .dark
            } label: {
                Label("Appearance", systemImage: isDark ? "sun.max" : "moon")
            }
            .help(isDark ? "Switch to Light appearance" : "Switch to Dark appearance")
        }
    }

    private var isDark: Bool { appearance == .dark || (appearance == .system && Appearance.systemIsDark) }
}

private struct InstallTarget: Identifiable { let engine: String; var id: String { engine } }

// MARK: - Empty state

struct EmptyState: View {
    @EnvironmentObject var model: AppModel
    let targeted: Bool

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: "arrow.down.doc")
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(targeted ? Color.accentColor : .secondary)
            Text("Drop files here")
                .font(.title2.weight(.semibold))
            Text("Photos, videos, audio, documents and PDFs")
                .foregroundStyle(.secondary)
            Button("Add Files…") { model.browse() }
                .padding(.top, 6)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(targeted ? Color.accentColor.opacity(0.06) : Color(nsColor: .textBackgroundColor))
    }
}

// MARK: - File table

struct FileTable: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                Text("Name").frame(maxWidth: .infinity, alignment: .leading)
                Text("Kind").frame(width: Col.kind, alignment: .leading)
                Text("Size").frame(width: Col.size, alignment: .trailing).padding(.trailing, 16)
                Text("Status").frame(width: Col.status, alignment: .leading)
            }
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(.secondary)
            .padding(.horizontal, 24)
            .frame(height: 26)
            .background(Color(nsColor: .textBackgroundColor))
            Divider()

            List(selection: $model.selection) {
                ForEach(model.items) { item in
                    FileRow(item: item)
                        .tag(item.id)
                }
                .onMove { model.move(from: $0, to: $1) }
            }
            .listStyle(.inset(alternatesRowBackgrounds: true))
            .onDeleteCommand { model.removeSelected() }
            .contextMenu(forSelectionType: UUID.self) { ids in
                let picked = model.items.filter { ids.contains($0.id) }
                let outs = picked.flatMap(\.outputs)
                if !outs.isEmpty { Button("Show in Finder") { model.reveal(outs) } }
                Button("Show Original in Finder") { model.reveal(picked.map(\.url)) }
                // only offer Copy Details when there is technical detail to copy
                let failed = picked.filter {
                    if case .failed = $0.status { return model.failureDetails[$0.id] != nil }
                    return false
                }
                if !failed.isEmpty {
                    Button(failed.count > 1 ? "Copy Details (\(failed.count) files)" : "Copy Details") {
                        model.copyDetails(for: failed)
                    }
                }
                Divider()
                Button("Remove from List") {
                    model.selection = ids
                    model.removeSelected()
                }
                .disabled(model.isWorking)
            }
        }
    }
}

struct FileRow: View {
    @EnvironmentObject var model: AppModel
    let item: FileItem

    var body: some View {
        HStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(nsImage: NSWorkspace.shared.icon(forFile: item.url.path))
                    .resizable()
                    .frame(width: 18, height: 18)
                Text(item.name)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            Text(item.kind)
                .lineLimit(1)
                .foregroundStyle(.secondary)
                .frame(width: Col.kind, alignment: .leading)
            Text(item.size)
                .foregroundStyle(.secondary)
                .monospacedDigit()
                .frame(width: Col.size, alignment: .trailing)
                .padding(.trailing, 16)
            StatusCell(item: item)
                .frame(width: Col.status, alignment: .leading)
        }
        .padding(.vertical, 2)
    }
}

struct StatusCell: View {
    @EnvironmentObject var model: AppModel
    let item: FileItem

    var body: some View {
        switch item.status {
        case .ready:
            // keep the column's width even when there's nothing to show
            Color.clear.frame(height: 1)
        case .unsupported:
            Label("Can't convert this type", systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .labelStyle(StatusLabelStyle())
        case .queued:
            Text("Waiting").foregroundStyle(.secondary)
        case .converting(let p):
            HStack(spacing: 8) {
                ProgressView(value: p).frame(width: 90)
                Text("\(Int(p * 100))%").foregroundStyle(.secondary).monospacedDigit()
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Converting, \(Int(p * 100)) percent")
        case .done:
            Label(doneText, systemImage: "checkmark.circle.fill")
                .labelStyle(StatusLabelStyle(iconColor: .green))
                .foregroundStyle(.secondary)
        case .skipped(let msg):
            Text(msg).foregroundStyle(.secondary).lineLimit(1).help(msg)
        case .failed(let msg, let missing):
            HStack(spacing: 6) {
                Label(msg, systemImage: "xmark.circle.fill")
                    .labelStyle(StatusLabelStyle(iconColor: .red))
                    .lineLimit(1)
                    .help(msg)
                if let m = missing {
                    Button("Install") { model.install(m) }
                        .controlSize(.small)
                        .help("Install \(m == "libreoffice" ? "LibreOffice" : "FFmpeg")")
                }
            }
        }
    }

    private var doneText: String {
        if item.combined { return "In the combined PDF" }
        let folder = model.outputFolderName
        if item.outputs.count > 1 { return "\(item.outputs.count) files in \(folder)" }
        return "Saved to \(folder)"
    }
}

/// Icon + text in a table cell, with an optional icon colour (green check, red cross).
struct StatusLabelStyle: LabelStyle {
    var iconColor: Color? = nil
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 6) {
            configuration.icon.foregroundStyle(iconColor ?? .primary)
            configuration.title.lineLimit(1)
        }
    }
}

// MARK: - Bottom bar

struct ConvertBar: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let note = model.notice ?? model.combineError ?? mixedNote {
                HStack(spacing: 8) {
                    Label(note, systemImage: model.combineError != nil ? "xmark.circle.fill" : "info.circle")
                        .font(.callout)
                        .foregroundStyle(model.combineError != nil ? .red : .secondary)
                    if model.notice == nil, model.combineError != nil, model.combineDetail != nil {
                        Button("Copy Details") { model.copyCombineDetails() }
                            .controlSize(.small)
                    }
                }
            }
            HStack(spacing: 20) {
                FormatPicker()

                if model.showsCombine {
                    Toggle("Combine into one PDF", isOn: $model.combinePDF)
                        .help("Put every image into a single PDF, one page each, in the list's order")
                }

                Picker("Quality", selection: $model.quality) {
                    ForEach(Quality.allCases) { Text($0.shortLabel).tag($0) }
                }
                .pickerStyle(.segmented)
                .fixedSize()
                .help("Smaller files or better quality")

                if model.showsResolution {
                    Picker("Video size", selection: $model.resolution) {
                        ForEach(Resolution.allCases) { Text($0.label).tag($0) }
                    }
                    .fixedSize()
                }
                if model.showsFolderOption {
                    Toggle("Put in one folder", isOn: $model.putInOneFolder)
                        .help("When a PDF has several pages, or a spreadsheet several sheets, put the files in one folder")
                }

                Spacer(minLength: 0)

                if model.isWorking {
                    Button(model.isStopping ? "Stopping…" : "Stop") { model.cancel() }
                        .disabled(model.isStopping)
                        .keyboardShortcut(".", modifiers: .command)
                } else if !model.allOutputs.isEmpty {
                    Button("Show in Finder") { model.reveal(model.allOutputs) }
                }
                Button(model.willCombine ? "Combine" : "Convert") { model.convertSelectedTarget() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.canConvert)
            }
            .disabled(model.items.isEmpty && !model.isWorking)
            HStack(spacing: 6) {
                Image(systemName: "folder").foregroundStyle(.secondary)
                Text("Save to").foregroundStyle(.secondary)
                Button(model.outputFolderName) { model.chooseOutputFolder() }
                    .buttonStyle(.link)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .frame(maxWidth: 240)
                    .help(model.outputFolder.path)
                if !model.usesDefaultOutputFolder {
                    Button("Reset to Downloads") { model.setOutputFolder(nil) }
                        .buttonStyle(.link)
                }
            }
            .font(.callout)
            .disabled(model.isWorking)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 12)
        .background(.bar)
    }

    private var mixedNote: String? {
        model.mixedNoCommon ? "These files can't all become the same format. Convert each kind separately." : nil
    }
}

struct FormatPicker: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            Text("Convert to")
            Menu {
                ForEach(sections, id: \.group) { section in
                    Section(section.group.rawValue) {
                        ForEach(section.formats) { f in
                            Toggle(isOn: Binding(
                                get: { model.selectedTarget == f.ext },
                                set: { _ in model.selectedTarget = f.ext })) {
                                Text(f.label)
                                Text(hint(f.ext))
                            }
                        }
                    }
                }
            } label: {
                Text(currentLabel)
            }
            .menuStyle(.button)
            .fixedSize()
            .disabled(model.targets.isEmpty || model.isWorking)
            .help("Choose the format to convert to")
        }
    }

    private struct SectionData { let group: FormatGroup; let formats: [TargetFormat] }

    private var sections: [SectionData] {
        FormatGroup.allCases.compactMap { g in
            let fs = model.targets.filter { (FormatInfo.groups[$0.ext] ?? .document) == g }
            return fs.isEmpty ? nil : SectionData(group: g, formats: fs)
        }
    }

    private var currentLabel: String {
        guard let t = model.selectedTarget,
              let f = model.targets.first(where: { $0.ext == t }) else { return "Choose" }
        return f.label
    }

    private func hint(_ ext: String) -> String {
        FormatInfo.hint(for: ext, sourceIsPDF: model.sourceIsPDF, sourceIsVideo: model.hasVideo)
    }
}

// MARK: - Engines

struct EngineBanner: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            if let err = model.engineError {
                Text(err)
                Spacer()
                Button("Try Again") { Task { await model.checkEngines(); model.refreshTargets() } }
            } else {
                Text(text)
                Spacer()
                ForEach(model.missingEngines, id: \.self) { e in
                    Button("Install \(e == "libreoffice" ? "LibreOffice" : "FFmpeg")") { model.install(e) }
                }
            }
        }
        .font(.callout)
        .padding(.horizontal, 16)
        .padding(.vertical, 8)
        .background(Color.orange.opacity(0.12))
    }

    private var text: String {
        var parts: [String] = []
        if model.missingEngines.contains("ffmpeg") { parts.append("Video and audio need FFmpeg.") }
        if model.missingEngines.contains("libreoffice") { parts.append("Documents need LibreOffice.") }
        return parts.joined(separator: " ")
    }
}

struct InstallSheet: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let engine: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Installing \(engine == "libreoffice" ? "LibreOffice" : "FFmpeg")")
                .font(.title3.weight(.semibold))
            Text(engine == "libreoffice"
                 ? "About 1 GB. This can take 5 to 15 minutes."
                 : "This usually takes a few minutes.")
                .foregroundStyle(.secondary)
            ScrollViewReader { proxy in
                ScrollView {
                    Text(model.installLog.joined(separator: "\n"))
                        .font(.system(.caption, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                    Color.clear.frame(height: 1).id("end")
                }
                .frame(height: 220)
                .background(Color.primary.opacity(0.04))
                .onChange(of: model.installLog.count) { proxy.scrollTo("end") }
            }
            HStack {
                switch model.installFinished {
                case .some(true): Label("Installed", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                case .some(false): Label("Install failed. See the log above.", systemImage: "xmark.circle.fill").foregroundStyle(.red)
                case .none: ProgressView().controlSize(.small); Text("Installing…").foregroundStyle(.secondary)
                }
                Spacer()
                Button("Close") { model.installing = nil; dismiss() }
                    .disabled(model.installFinished == nil)
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}
