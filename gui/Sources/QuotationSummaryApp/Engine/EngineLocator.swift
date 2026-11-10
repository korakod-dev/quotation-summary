//
// EngineLocator.swift
// Where the Python engine (the installed quotation-summary folder) lives, and the paths inside it.
//
// Resolution (contract C6): env QS_APP_DIR if set and not empty, else ~/QuotationSummary/quotation-summary
// (the place scripts/setup-all.command installs to).
//

import Foundation

struct EngineLocator: Equatable {
    /// Root of the installed engine (contains scripts/, qsum/, .venv/, config.json).
    let directory: URL

    /// Test hook / override variable name.
    static let envKey = "QS_APP_DIR"
    /// Default location, relative to the home folder.
    static let defaultRelativePath = "QuotationSummary/quotation-summary"

    /// Resolve the engine folder from the environment. Pure: `env` and `home` are injected.
    static func resolve(env: [String: String] = ProcessInfo.processInfo.environment,
                        home: URL = FileManager.default.homeDirectoryForCurrentUser) -> EngineLocator {
        if let raw = env[envKey]?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty {
            let expanded = (raw as NSString).expandingTildeInPath
            return EngineLocator(directory: URL(fileURLWithPath: expanded, isDirectory: true))
        }
        return EngineLocator(directory: home.appendingPathComponent(defaultRelativePath, isDirectory: true))
    }

    var scriptsDirectory: URL { directory.appendingPathComponent("scripts", isDirectory: true) }
    /// scripts/status.sh: read-only JSON status.
    var statusScript: URL { scriptsDirectory.appendingPathComponent("status.sh") }
    /// scripts/run.sh: starts a real run.
    var runScript: URL { scriptsDirectory.appendingPathComponent("run.sh") }

    /// True when the scripts the app needs are there. A missing engine is reported as "not installed".
    func isInstalled(fileManager: FileManager = .default) -> Bool {
        fileManager.fileExists(atPath: statusScript.path) && fileManager.fileExists(atPath: runScript.path)
    }
}

/// The app's own log file for runs it starts (contract C4).
enum RunLog {
    /// ~/Library/Logs/QuotationSummary
    static func defaultDirectory(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> URL {
        home.appendingPathComponent("Library/Logs/QuotationSummary", isDirectory: true)
    }

    static let fileName = "app-run.log"

    /// Shown in messages.
    static let displayPath = "~/Library/Logs/QuotationSummary/app-run.log"

    /// `=== <ISO 8601 local, seconds> app run ===`
    static func header(at date: Date = Date(), timeZone: TimeZone = .current) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = timeZone
        f.formatOptions = [.withInternetDateTime]    // e.g. 2026-11-10T09:30:00+07:00 when the zone has an offset
        return "=== \(f.string(from: date)) app run ==="
    }
}
