//
// AppViewModel.swift
// State of the one window: the last status from the engine, the filter, banners, and the actions
// (refresh, run now, import PDFs, open Excel / folder).
//
// ObservableObject + @Published on purpose (no @State / @Observable): the macOS 27 SDK builds those with macros
// that need a full Xcode, and this app builds with the Command Line Tools.
//

import Foundation
import SwiftUI

@MainActor
final class AppViewModel: ObservableObject {
    @Published private(set) var status: EngineStatus
    @Published var filter: FileStatus?
    @Published private(set) var banner: Labels.Banner?
    /// True from the click on "เริ่มสรุปตอนนี้" until run.sh exits.
    @Published private(set) var runInFlight = false
    @Published var isDropTargeted = false
    /// True after the first status arrived (before that the window shows "กำลังโหลด...").
    @Published private(set) var hasLoadedOnce = false

    private let service: EngineService
    private let opener: FileOpening
    /// Seconds to wait between two polls (running or not). A parameter so tests can use short waits.
    private let pollInterval: (Bool) -> TimeInterval
    private var pollTask: Task<Void, Never>?
    private var refreshInFlight = false
    private var refreshQueued = false

    init(service: EngineService, opener: FileOpening = SystemFileOpener(),
         pollInterval: @escaping (Bool) -> TimeInterval = PollPolicy.interval(running:)) {
        self.service = service
        self.opener = opener
        self.pollInterval = pollInterval
        self.status = .synthetic(errorCode: .unknown("loading"), error: "")
    }

    /// The real thing: engine folder from the environment, real processes.
    static func live() -> AppViewModel {
        AppViewModel(service: EngineService(locator: .resolve(), runner: SystemProcessRunner()))
    }

    // MARK: derived state

    var isRunning: Bool { status.running || runInFlight }

    /// "เริ่มสรุปตอนนี้" is enabled when nothing is running and the program is installed.
    var canRun: Bool {
        guard hasLoadedOnce, !isRunning else { return false }
        if case .some(.notInstalled) = status.errorCode { return false }
        return true
    }

    var visibleFiles: [FileRow] {
        guard let filter = filter else { return status.files }
        return status.files.filter { $0.status == filter }
    }

    /// Error banner of the status (not_installed, config, ...) takes priority over an action message.
    var activeBanner: Labels.Banner? {
        if hasLoadedOnce, let statusBanner = Labels.banner(for: status) { return statusBanner }
        return banner
    }

    var canOpenExcel: Bool { status.outputExists && status.outputFile != nil }
    var canOpenFolder: Bool { status.sourceFolder != nil }
    var canImport: Bool { status.sourceFolder != nil && status.errorCode != .some(.notInstalled) }

    func dismissBanner() { banner = nil }

    // MARK: polling

    /// Start the polling loop (2 s while running, 30 s otherwise). Safe to call more than once.
    func startPolling() {
        guard pollTask == nil else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self = self else { return }
                await self.refresh()
                let seconds = self.pollInterval(self.isRunning)
                try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
            }
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Wake the loop now: cancel the sleep it is in (it may be the 30 s idle one) and start over, so the next check
    /// and the following ones use the interval of the CURRENT state. Does nothing when polling is not on.
    private func restartPolling() {
        guard pollTask != nil else { return }
        stopPolling()
        startPolling()
    }

    /// Ask for a status now (window activated, after an import, after a run). Fire and forget.
    func requestRefresh() {
        Task { await refresh() }
    }

    /// Fetch the status once. If a fetch is already going, ask for one more right after it instead of piling up.
    func refresh() async {
        if refreshInFlight {
            refreshQueued = true
            return
        }
        refreshInFlight = true
        repeat {
            refreshQueued = false
            let fresh = await service.status()
            status = fresh
            hasLoadedOnce = true
        } while refreshQueued
        refreshInFlight = false
    }

    // MARK: run now

    /// "เริ่มสรุปตอนนี้". Returns when run.sh has ended; progress meanwhile comes only from polling.
    func startRun() async {
        guard canRun else { return }
        runInFlight = true
        banner = nil
        // The loop may be in its 30 s idle sleep: restart it so it follows the run every 2 s from now on.
        restartPolling()
        // A first status soon after the click, so the header flips to "กำลังทำงาน" without waiting for the next poll.
        let early = Task { [weak self] in
            // Cancelled (the run ended within 1 s) = no early check: the end of startRun refreshes anyway.
            do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            await self?.refresh()
        }
        let result = await service.startRun()
        early.cancel()
        runInFlight = false
        banner = Labels.runResultMessage(exitCode: result.exitCode, launchError: result.launchError)
        await refresh()
    }

    // MARK: import

    /// Copy PDFs into the source folder, show the result, refresh the table.
    @discardableResult
    func importFiles(_ urls: [URL]) async -> [ImportResult] {
        guard let folder = status.sourceFolder, !folder.isEmpty else {
            banner = Labels.Banner(kind: .error, text: "ยังไม่รู้โฟลเดอร์ปลายทางของ PDF (ตรวจการติดตั้งก่อน)")
            return []
        }
        let target = URL(fileURLWithPath: folder, isDirectory: true)
        // File copies can be slow for big PDFs: keep them off the main thread.
        let results = await Task.detached(priority: .userInitiated) {
            FileImporter.importPDFs(urls, into: target)
        }.value
        banner = Labels.importBanner(results)
        await refresh()
        return results
    }

    // MARK: open

    func openExcel() {
        guard canOpenExcel, let path = status.outputFile else {
            banner = Labels.Banner(kind: .info, text: "ยังไม่มีไฟล์ Excel (ยังไม่เคยสรุป) กด เริ่มสรุปตอนนี้ ก่อน")
            return
        }
        if !opener.openWorkbook(URL(fileURLWithPath: path)) {
            banner = Labels.Banner(kind: .error, text: "เปิดไฟล์ Excel ไม่ได้: \(path)")
        }
    }

    func openSourceFolder() {
        guard let path = status.sourceFolder else { return }
        if !opener.openFolder(URL(fileURLWithPath: path, isDirectory: true)) {
            banner = Labels.Banner(kind: .error, text: "เปิดโฟลเดอร์ไม่ได้: \(path)")
        }
    }
}
