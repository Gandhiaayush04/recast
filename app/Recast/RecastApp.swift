import AppKit
import SwiftUI

@main
struct RecastApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppModel()
    @AppStorage("appearance") private var appearance: Appearance = .system

    var body: some Scene {
        // A single window: files dropped on the Dock icon or sent from Finder
        // are added to this window instead of opening new ones.
        Window("Recast", id: "main") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 820, minHeight: 460)
                .onAppear {
                    appearance.apply()
                    appDelegate.model = model
                    appDelegate.flushPending()
                }
                .onChange(of: appearance) { appearance.apply() }
                .task { await model.checkEngines() }
                .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
                    // e.g. an engine was installed in Terminal while Recast was open
                    Task { await model.checkEngines() }
                }
        }
        .defaultSize(width: 900, height: 600)
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Add Files…") { model.browse() }
                    .keyboardShortcut("o")
            }
            CommandGroup(after: .newItem) {
                Divider()
                Button(model.willCombine ? "Combine" : "Convert") { model.convertSelectedTarget() }
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(!model.canConvert)
                Button("Stop") { model.cancel() }
                    .keyboardShortcut(".", modifiers: .command)
                    .disabled(!model.isWorking)
                Divider()
                Button("Show in Finder") { model.reveal(model.allOutputs) }
                    .keyboardShortcut("r", modifiers: .command)
                    .disabled(model.allOutputs.isEmpty)
            }
            CommandGroup(after: .pasteboard) {
                Divider()
                Button("Remove from List") { model.removeSelected() }
                    .keyboardShortcut(.delete, modifiers: [])
                    .disabled(model.selection.isEmpty || model.isWorking)
                Button("Clear List") { model.clear() }
                    .keyboardShortcut(.delete, modifiers: [.command, .shift])
                    .disabled(model.items.isEmpty || model.isWorking)
                Button("Sort by Name") { model.sortAZ() }
                    .disabled(model.items.count < 2 || model.isWorking)
            }
            CommandGroup(after: .help) {
                Divider()
                Button("Show Log in Finder") { DiagnosticsLog.shared.revealInFinder() }
            }
            CommandGroup(after: .toolbar) {
                Picker("Appearance", selection: $appearance) {
                    ForEach(Appearance.allCases) { Text($0.label).tag($0) }
                }
                Divider()
            }
        }
    }

    init() {
        let v = Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
        DiagnosticsLog.shared.log("Recast \(v) launched on macOS \(ProcessInfo.processInfo.operatingSystemVersionString)")
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?
    private var pending: [URL] = []

    /// Files dropped on the Dock icon, chosen via "Open With", or sent by the Finder Quick Action.
    func application(_ application: NSApplication, open urls: [URL]) {
        pending.append(contentsOf: urls.filter(\.isFileURL))
        flushPending()
        NSApp.activate(ignoringOtherApps: true)
        (NSApp.windows.first { $0.title.hasPrefix("Recast") } ?? NSApp.windows.first)?.makeKeyAndOrderFront(nil)
    }

    func flushPending() {
        guard let model, !pending.isEmpty else { return }
        model.add(pending)
        pending.removeAll()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) {
        model?.shutdown()
    }
}
