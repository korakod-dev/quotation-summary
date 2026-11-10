//
// FileOpening.swift
// "เปิด Excel" and "เปิดโฟลเดอร์ PDF". A protocol so the view model can be tested without opening anything.
//

import AppKit
import Foundation

protocol FileOpening {
    /// Open the workbook in Microsoft Excel; if Excel is not installed, in the default app. False = could not open.
    func openWorkbook(_ url: URL) -> Bool
    /// Show a folder in Finder.
    func openFolder(_ url: URL) -> Bool
}

struct SystemFileOpener: FileOpening {
    static let excelBundleID = "com.microsoft.Excel"

    func openWorkbook(_ url: URL) -> Bool {
        let workspace = NSWorkspace.shared
        if let excel = workspace.urlForApplication(withBundleIdentifier: SystemFileOpener.excelBundleID) {
            workspace.open([url], withApplicationAt: excel, configuration: NSWorkspace.OpenConfiguration(), completionHandler: nil)
            return true
        }
        return workspace.open(url)      // fallback: whatever app is set for .xlsx
    }

    func openFolder(_ url: URL) -> Bool {
        NSWorkspace.shared.open(url)
    }
}
