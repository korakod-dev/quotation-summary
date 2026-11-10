//
// QuotationSummaryApp.swift
// Entry point of the desktop app "สรุปใบเสนอราคา": one window, no document model, no menu bar item.
//
// Business logic stays in the Python engine (the installed quotation-summary folder, see EngineLocator).
// This app only shows its status (scripts/status.sh), starts a run (scripts/run.sh) and copies PDFs into the
// source folder.
//

import AppKit
import SwiftUI

@main
struct QuotationSummaryApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var model = AppViewModel.live()

    var body: some Scene {
        // `Window` (not WindowGroup): exactly one window, a second one cannot be opened.
        Window("สรุปใบเสนอราคา", id: "main") {
            ContentView(model: model)
        }
        .defaultSize(width: 900, height: 700)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// Closing the one window quits the app (nothing to run in the background: the schedule is launchd's job).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}
