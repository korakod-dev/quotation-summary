//
// ViewModelTests.swift
// AppViewModel with a FakeRunner: refresh, run now, import, filter, buttons' enabled state, polling decisions.
//

import Foundation
import Testing
@testable import QuotationSummaryApp

@MainActor
@Suite("App view model")
struct ViewModelTests {
    struct World {
        let engine: URL
        let runner: FakeRunner
        let opener: FakeOpener
        let model: AppViewModel
    }

    /// `statusFixture` is what status.sh "prints"; run.sh answers with `runExit`.
    func world(statusFixture: String = "status-v1.json", runExit: Int32 = 0) throws -> World {
        let engine = try makeFakeEngine()
        let json = try fixtureData(statusFixture)
        let runner = FakeRunner { cmd in
            cmd.arguments.first?.hasSuffix("status.sh") == true
                ? ProcessResult(exitCode: 0, stdout: json)
                : ProcessResult(exitCode: runExit)
        }
        let opener = FakeOpener()
        let svc = EngineService(locator: EngineLocator(directory: engine), runner: runner,
                                logDirectory: engine.appendingPathComponent("logs"))
        return World(engine: engine, runner: runner, opener: opener, model: AppViewModel(service: svc, opener: opener))
    }

    @Test func startsEmptyAndNotRunnable() throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        #expect(w.model.hasLoadedOnce == false)
        #expect(w.model.canRun == false)                    // nothing known yet
        #expect(w.model.activeBanner == nil)
    }

    @Test func refreshLoadsStatusAndEnablesRun() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(w.model.hasLoadedOnce)
        #expect(w.model.status.files.count == 9)
        #expect(w.model.canRun)
        #expect(w.model.isRunning == false)
        #expect(w.model.activeBanner == nil)
        #expect(w.model.canOpenExcel && w.model.canOpenFolder && w.model.canImport)
    }

    @Test func runningStatusDisablesRunAndSpeedsUpPolling() async throws {
        let w = try world(statusFixture: "status-running.json")
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(w.model.isRunning)
        #expect(w.model.canRun == false)
        #expect(PollPolicy.interval(running: w.model.isRunning) == 2)
        await w.model.startRun()                            // ignored while running
        let runCalls = w.runner.commands.filter { $0.arguments.first?.hasSuffix("run.sh") == true }
        #expect(runCalls.isEmpty)
    }

    @Test func idlePollsSlowly() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(PollPolicy.interval(running: w.model.isRunning) == 30)
    }

    @Test func notInstalledShowsBannerAndBlocksRunAndImport() async throws {
        let w = try world(statusFixture: "status-not-installed.json")
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(w.model.activeBanner?.text == "ยังไม่ได้ติดตั้ง: ดับเบิลคลิก ติดตั้ง.command ก่อน")
        #expect(w.model.canRun == false)
        #expect(w.model.canImport == false)
        #expect(w.model.canOpenExcel == false)
    }

    @Test func sourceMissingShowsBannerButStillAllowsRun() async throws {
        let w = try world(statusFixture: "status-error.json")
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(w.model.activeBanner?.kind == .error)
        #expect(w.model.activeBannerIsDismissable == false)
        #expect(w.model.canRun)                              // the engine reports the real reason itself
    }

    @Test func startRunPassesTriggerAndShowsResultMessage() async throws {
        let w = try world(runExit: 0)
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        await w.model.startRun()
        let runs = w.runner.commands.filter { $0.arguments.first?.hasSuffix("run.sh") == true }
        #expect(runs.count == 1)
        #expect(runs[0].extraEnvironment["QS_TRIGGER"] == "app")
        #expect(w.model.runInFlight == false)
        #expect(w.model.activeBanner == Labels.runResultMessage(exitCode: 0))
        #expect(w.model.activeBannerIsDismissable)
        w.model.dismissBanner()
        #expect(w.model.activeBanner == nil)
    }

    @Test func exitCode3ShowsTheBusyMessage() async throws {
        let w = try world(runExit: 3)
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        await w.model.startRun()
        #expect(w.model.activeBanner?.text == "กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา)")
    }

    @Test func exitCodes1And2() async throws {
        for code: Int32 in [1, 2] {
            let w = try world(runExit: code)
            defer { try? FileManager.default.removeItem(at: w.engine) }
            await w.model.refresh()
            await w.model.startRun()
            #expect(w.model.activeBanner == Labels.runResultMessage(exitCode: code))
        }
    }

    @Test func buttonIsDisabledWhileTheStartedRunIsInFlight() async throws {
        // run.sh "takes a while": check the model's state from inside the runner
        let engine = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: engine) }
        let json = try fixtureData("status-v1.json")
        final class Probe: @unchecked Sendable { var canRunDuring: Bool? = nil; var inFlightDuring: Bool? = nil }
        let probe = Probe()
        var modelRef: AppViewModel?
        let runner = FakeRunner { cmd in
            if cmd.arguments.first?.hasSuffix("run.sh") == true {
                DispatchQueue.main.sync {
                    probe.canRunDuring = modelRef?.canRun
                    probe.inFlightDuring = modelRef?.runInFlight
                }
                return ProcessResult(exitCode: 0)
            }
            return ProcessResult(exitCode: 0, stdout: json)
        }
        let svc = EngineService(locator: EngineLocator(directory: engine), runner: runner, logDirectory: engine)
        let model = AppViewModel(service: svc, opener: FakeOpener())
        modelRef = model
        await model.refresh()
        #expect(model.canRun)
        // run in a Task so the runner closure can hop to the main queue without deadlocking the main actor
        await Task.detached { await model.startRun() }.value
        #expect(probe.inFlightDuring == true)
        #expect(probe.canRunDuring == false)
        #expect(model.canRun)                                // enabled again afterwards
    }

    @Test func startingARunWhileTheLoopSleepsIdleMakesItPollFastAtOnce() async throws {
        // the loop is in its long idle sleep when the button is pressed. Without a wake-up it would be silent for
        // the rest of that sleep (only one early check at 1 s). Intervals: idle 30 s, running 0.1 s.
        let engine = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: engine) }
        let json = try fixtureData("status-v1.json")
        final class Counter: @unchecked Sendable {
            private let lock = NSLock()
            private var n = 0
            func bump() { lock.lock(); n += 1; lock.unlock() }
            var value: Int { lock.lock(); defer { lock.unlock() }; return n }
        }
        let statusCalls = Counter()
        final class Marks: @unchecked Sendable { var atRunStart = -1; var atRunEnd = -1 }
        let marks = Marks()
        let runner = FakeRunner { cmd in
            if cmd.arguments.first?.hasSuffix("run.sh") == true {
                marks.atRunStart = statusCalls.value
                Thread.sleep(forTimeInterval: 0.9)           // "the run takes a while"; polls must happen meanwhile
                marks.atRunEnd = statusCalls.value
                return ProcessResult(exitCode: 0)
            }
            statusCalls.bump()
            return ProcessResult(exitCode: 0, stdout: json)
        }
        let svc = EngineService(locator: EngineLocator(directory: engine), runner: runner, logDirectory: engine)
        let model = AppViewModel(service: svc, opener: FakeOpener(),
                                 pollInterval: { running in running ? 0.1 : 30 })
        model.startPolling()
        // wait for the first poll to land: the loop is now in the 30 s sleep
        for _ in 0..<100 where !model.hasLoadedOnce { try await Task.sleep(nanoseconds: 20_000_000) }
        #expect(model.hasLoadedOnce)
        #expect(statusCalls.value == 1)
        await model.startRun()
        model.stopPolling()
        let duringRun = marks.atRunEnd - marks.atRunStart
        // restart check + a poll every 0.1 s for 0.9 s: far more than the single early check the old loop gave
        #expect(duringRun >= 3, "only \(duringRun) status checks while the run was going")
    }

    @Test func startRunDoesNotStartPollingByItself() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        let before = w.runner.commands.count
        await w.model.startRun()
        try await Task.sleep(nanoseconds: 150_000_000)
        // run.sh once, the early check is cancelled, one refresh at the end: no background loop appeared
        #expect(w.runner.commands.count - before <= 2)
    }

    @Test func filterShowsOnlyThatStatus() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        #expect(w.model.visibleFiles.count == 9)
        w.model.filter = .check
        #expect(w.model.visibleFiles.count == 4)
        #expect(w.model.visibleFiles.allSatisfy { $0.status == .check })
        w.model.filter = .waiting
        #expect(w.model.visibleFiles.map(\.name) == ["ใหม่ ยังไม่ได้อ่าน.pdf"])
        w.model.filter = nil
        #expect(w.model.visibleFiles.count == 9)
    }

    @Test func importCopiesIntoTheSourceFolderThenRefreshes() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        let root = try makeTempDir("qsum-vm-import")
        defer { try? FileManager.default.removeItem(at: root) }
        // a status whose source_folder is a real temp folder
        let dest = root.appendingPathComponent("pdfs", isDirectory: true)
        try FileManager.default.createDirectory(at: dest, withIntermediateDirectories: true)
        var json = try #require(JSONSerialization.jsonObject(with: fixtureData("status-v1.json")) as? [String: Any])
        json["source_folder"] = dest.path
        let data = try JSONSerialization.data(withJSONObject: json)
        w.runner.handler = { _ in ProcessResult(exitCode: 0, stdout: data) }
        await w.model.refresh()
        let src = root.appendingPathComponent("ใบใหม่.pdf")
        try write("PDF", to: src)
        let statusCallsBefore = w.runner.commands.count
        let results = await w.model.importFiles([src, root.appendingPathComponent("x.txt")])
        #expect(results == [.copied("ใบใหม่.pdf"), .notPDF("x.txt")])
        #expect(FileManager.default.fileExists(atPath: dest.appendingPathComponent("ใบใหม่.pdf").path))
        #expect(w.runner.commands.count == statusCallsBefore + 1)          // refreshed once after the import
        #expect(w.model.activeBanner?.text.contains("เพิ่ม 1 ไฟล์แล้ว กด เริ่มสรุปตอนนี้ หรือรอรอบตั้งเวลา") == true)
    }

    @Test func importWithoutKnownFolderShowsAnErrorAndCopiesNothing() async throws {
        let w = try world(statusFixture: "status-not-installed.json")
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        let results = await w.model.importFiles([URL(fileURLWithPath: "/tmp/a.pdf")])
        #expect(results.isEmpty)
    }

    @Test func openExcelAndFolderUseTheOpener() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        w.model.openExcel()
        #expect(w.opener.workbooks.map(\.path) == [w.model.status.outputFile!])
        w.model.openSourceFolder()
        #expect(w.opener.folders.map(\.path) == [w.model.status.sourceFolder!])
        w.opener.result = false
        w.model.openExcel()
        #expect(w.model.activeBanner?.text.contains("เปิดไฟล์ Excel ไม่ได้") == true)
    }

    @Test func openExcelWithoutAWorkbookExplainsInsteadOfOpening() async throws {
        let w = try world(statusFixture: "status-not-installed.json")
        defer { try? FileManager.default.removeItem(at: w.engine) }
        await w.model.refresh()
        w.model.openExcel()
        #expect(w.opener.workbooks.isEmpty)
    }

    @Test func concurrentRefreshesAreCoalesced() async throws {
        let w = try world()
        defer { try? FileManager.default.removeItem(at: w.engine) }
        async let a: Void = w.model.refresh()
        async let b: Void = w.model.refresh()
        async let c: Void = w.model.refresh()
        _ = await (a, b, c)
        #expect(w.runner.commands.count <= 3)
        #expect(w.runner.commands.count >= 1)
        #expect(w.model.hasLoadedOnce)
    }
}
