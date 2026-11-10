//
// EngineTests.swift
// Engine folder resolution, the commands the app builds (C3/C4), interpreting status.sh output, poll intervals,
// the run log. Uses FakeRunner: no engine script is ever started.
//

import Foundation
import Testing
@testable import QuotationSummaryApp

@Suite("Engine locator")
struct EngineLocatorTests {
    let home = URL(fileURLWithPath: "/Users/example", isDirectory: true)

    @Test func defaultIsInstalledFolderUnderHome() {
        let e = EngineLocator.resolve(env: [:], home: home)
        #expect(e.directory.path == "/Users/example/QuotationSummary/quotation-summary")
        #expect(e.statusScript.path == "/Users/example/QuotationSummary/quotation-summary/scripts/status.sh")
        #expect(e.runScript.path == "/Users/example/QuotationSummary/quotation-summary/scripts/run.sh")
    }

    @Test func envOverridesTheDefault() {
        let e = EngineLocator.resolve(env: ["QS_APP_DIR": "/tmp/engine x"], home: home)
        #expect(e.directory.path == "/tmp/engine x")
        #expect(e.statusScript.path == "/tmp/engine x/scripts/status.sh")
    }

    @Test func emptyOrBlankEnvFallsBackToTheDefault() {
        #expect(EngineLocator.resolve(env: ["QS_APP_DIR": ""], home: home).directory.path.hasSuffix("QuotationSummary/quotation-summary"))
        #expect(EngineLocator.resolve(env: ["QS_APP_DIR": "   "], home: home).directory.path.hasSuffix("QuotationSummary/quotation-summary"))
    }

    @Test func tildeInEnvIsExpanded() {
        let e = EngineLocator.resolve(env: ["QS_APP_DIR": "~/eng"], home: home)
        #expect(e.directory.path == NSString(string: "~/eng").expandingTildeInPath)
        #expect(!e.directory.path.contains("~"))
    }

    @Test func isInstalledNeedsBothScripts() throws {
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let e = EngineLocator(directory: dir)
        #expect(e.isInstalled())
        try FileManager.default.removeItem(at: e.runScript)
        #expect(!e.isInstalled())
        #expect(!EngineLocator(directory: dir.appendingPathComponent("nope")).isInstalled())
    }
}

@Suite("Poll policy")
struct PollPolicyTests {
    @Test func intervals() {
        #expect(PollPolicy.interval(running: true) == 2)
        #expect(PollPolicy.interval(running: false) == 30)
        #expect(PollPolicy.runningInterval < PollPolicy.idleInterval)
    }
}

@Suite("Engine service")
struct EngineServiceTests {
    func service(_ dir: URL, runner: FakeRunner, logs: URL? = nil) -> EngineService {
        EngineService(locator: EngineLocator(directory: dir), runner: runner,
                      logDirectory: logs ?? dir.appendingPathComponent("logs"))
    }

    @Test func statusRunsStatusScriptWithBashAndDecodesIt() async throws {
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = try fixtureData("status-v1.json")
        let runner = FakeRunner { _ in ProcessResult(exitCode: 0, stdout: json) }
        let status = await service(dir, runner: runner).status()
        #expect(status.ok)
        #expect(status.files.count == 9)
        let cmd = try #require(runner.commands.first)
        #expect(runner.commands.count == 1)
        #expect(cmd.executable.path == "/bin/bash")
        #expect(cmd.arguments == [dir.appendingPathComponent("scripts/status.sh").path])
        #expect(cmd.logFile == nil)                                  // captured, not logged
        #expect(cmd.extraEnvironment.isEmpty)
        #expect(cmd.timeout != nil)
    }

    @Test func nonZeroExitWithJSONStillDecodes() async throws {
        // status.sh exits 1 for "not ok" but still prints the document
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = try fixtureData("status-error.json")
        let runner = FakeRunner { _ in ProcessResult(exitCode: 1, stdout: json) }
        let status = await service(dir, runner: runner).status()
        #expect(status.errorCode == .sourceMissing)
    }

    @Test func engineFolderMissingIsNotInstalledWithoutRunningAnything() async throws {
        let runner = FakeRunner()
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent("no-engine-\(UUID().uuidString)")
        let status = await service(missing, runner: runner).status()
        #expect(status.errorCode == .notInstalled)
        #expect(runner.commands.isEmpty)
        #expect(Labels.banner(for: status)?.text == "ยังไม่ได้ติดตั้ง: ดับเบิลคลิก ติดตั้ง.command ก่อน")
    }

    @Test func notInstalledJSONFromTheScriptIsPassedThrough() async throws {
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let json = try fixtureData("status-not-installed.json")
        let status = await service(dir, runner: FakeRunner { _ in ProcessResult(exitCode: 1, stdout: json) }).status()
        #expect(status.errorCode == .notInstalled)
    }

    @Test func interpretHandlesEveryFailureShape() {
        let launch = EngineService.interpret(ProcessResult(exitCode: nil, launchError: "boom"))
        #expect(launch.errorCode == .unknown("launch") && launch.error?.contains("boom") == true)
        let timeout = EngineService.interpret(ProcessResult(exitCode: nil, timedOut: true))
        #expect(timeout.errorCode == .unknown("timeout"))
        let empty = EngineService.interpret(ProcessResult(exitCode: 127, stdout: Data()))
        #expect(empty.errorCode == .unknown("bad_output") && empty.error?.contains("127") == true)
        let garbage = EngineService.interpret(ProcessResult(exitCode: 0, stdout: Data("Traceback (most recent call last)".utf8)))
        #expect(garbage.errorCode == .unknown("bad_output"))
        let killed = EngineService.interpret(ProcessResult(exitCode: nil, stdout: Data()))
        #expect(killed.error?.contains("-") == true)
        #expect(Labels.banner(for: garbage) != nil)
    }

    @Test func runCommandSetsTriggerLogAndHeader() throws {
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let logs = URL(fileURLWithPath: "/tmp/qsum-logs", isDirectory: true)
        let svc = service(dir, runner: FakeRunner(), logs: logs)
        let now = Date(timeIntervalSince1970: 1_794_277_800)
        let cmd = svc.runCommand(now: now)
        #expect(cmd.executable.path == "/bin/bash")
        #expect(cmd.arguments == [dir.appendingPathComponent("scripts/run.sh").path])
        #expect(cmd.extraEnvironment == ["QS_TRIGGER": "app"])
        #expect(cmd.logFile?.path == "/tmp/qsum-logs/app-run.log")
        let header = try #require(cmd.logHeader)
        #expect(header.hasPrefix("=== "))
        #expect(header.hasSuffix(" app run ==="))
        #expect(header.range(of: #"^=== \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(Z|[+-]\d{2}:\d{2}) app run ===$"#, options: .regularExpression) != nil)
        #expect(cmd.timeout == nil)                                  // a run may take many minutes
    }

    @Test func startRunUsesTheRunnerAndReturnsItsResult() async throws {
        let dir = try makeFakeEngine()
        defer { try? FileManager.default.removeItem(at: dir) }
        let runner = FakeRunner { _ in ProcessResult(exitCode: 3) }
        let result = await service(dir, runner: runner).startRun()
        #expect(result.exitCode == 3)
        #expect(runner.commands.count == 1)
        #expect(runner.commands[0].arguments.first?.hasSuffix("scripts/run.sh") == true)
    }

    @Test func startRunWithoutEngineDoesNotRunAnything() async {
        let runner = FakeRunner()
        let missing = FileManager.default.temporaryDirectory.appendingPathComponent("no-engine-\(UUID().uuidString)")
        let result = await service(missing, runner: runner).startRun()
        #expect(result.launchError != nil)
        #expect(runner.commands.isEmpty)
    }
}

@Suite("Run log")
struct RunLogTests {
    @Test func headerFormat() {
        let utc = TimeZone(secondsFromGMT: 0)!
        #expect(RunLog.header(at: Date(timeIntervalSince1970: 0), timeZone: utc) == "=== 1970-01-01T00:00:00Z app run ===")
        let bkk = TimeZone(secondsFromGMT: 7 * 3600)!
        #expect(RunLog.header(at: Date(timeIntervalSince1970: 0), timeZone: bkk) == "=== 1970-01-01T07:00:00+07:00 app run ===")
    }

    @Test func defaultLogDirectory() {
        let d = RunLog.defaultDirectory(home: URL(fileURLWithPath: "/Users/example"))
        #expect(d.path == "/Users/example/Library/Logs/QuotationSummary")
        #expect(RunLog.fileName == "app-run.log")
    }

    @Test func openForAppendCreatesFolderWritesHeaderAndKeepsOldContent() throws {
        let root = try makeTempDir("qsum-log")
        defer { try? FileManager.default.removeItem(at: root) }
        let log = root.appendingPathComponent("deep/er/app-run.log")
        let h1 = try LogFile.openForAppend(log, header: "=== first ===")
        try h1.write(contentsOf: Data("output 1\n".utf8))
        try h1.close()
        let h2 = try LogFile.openForAppend(log, header: "=== second ===")
        try h2.close()
        #expect(try read(log) == "\n=== first ===\noutput 1\n\n=== second ===\n")
    }

    @Test func oversizedLogIsTrimmedToItsTailAtALineStart() throws {
        let root = try makeTempDir("qsum-log")
        defer { try? FileManager.default.removeItem(at: root) }
        let log = root.appendingPathComponent("app-run.log")
        let lines = (0..<2000).map { "line \($0) xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" }.joined(separator: "\n") + "\n"
        try write(lines, to: log)
        try LogFile.trimIfNeeded(log, maxBytes: 10_000, keepBytes: 2_000)
        let after = try read(log)
        #expect(after.utf8.count <= 2_000)
        #expect(after.hasPrefix("line "))                         // starts on a whole line
        #expect(after.hasSuffix("line 1999 xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx\n"))
        // a small log is left alone
        try write("small\n", to: log)
        try LogFile.trimIfNeeded(log, maxBytes: 10_000, keepBytes: 2_000)
        #expect(try read(log) == "small\n")
    }
}

/// SystemProcessRunner itself, with harmless system tools only (/bin/echo, /bin/sh): NOT the engine scripts.
@Suite("System process runner")
struct SystemProcessRunnerTests {
    let runner = SystemProcessRunner()

    @Test func capturesStdoutAndExitCode() async {
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/bin/echo"), arguments: ["สวัสดี", "x"]))
        #expect(r.exitCode == 0)
        #expect(String(decoding: r.stdout, as: UTF8.self) == "สวัสดี x\n")
        #expect(r.launchError == nil)
    }

    @Test func reportsExitCodesAndPassesExtraEnvironment() async {
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/bin/sh"),
                                                arguments: ["-c", "echo \"$QS_TRIGGER\"; exit 3"],
                                                extraEnvironment: ["QS_TRIGGER": "app"]))
        #expect(r.exitCode == 3)
        #expect(String(decoding: r.stdout, as: UTF8.self) == "app\n")
    }

    @Test func stdinIsDevNull() async {
        // `cat` returns at once with empty output only if stdin is /dev/null (otherwise it would hang on the test's stdin)
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/bin/cat"), arguments: [], timeout: 10))
        #expect(r.exitCode == 0)
        #expect(r.stdout.isEmpty)
        #expect(!r.timedOut)
    }

    @Test func largeOutputDoesNotDeadlock() async {
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/bin/sh"),
                                                arguments: ["-c", "head -c 1000000 /dev/zero | tr '\\0' 'a'"], timeout: 30))
        #expect(r.exitCode == 0)
        #expect(r.stdout.count == 1_000_000)
    }

    @Test func missingExecutableIsALaunchErrorNotACrash() async {
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/nonexistent/tool"), arguments: []))
        #expect(r.exitCode == nil)
        #expect(r.launchError != nil)
    }

    @Test func timeoutKillsTheProcess() async {
        let r = await runner.run(ProcessCommand(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["30"], timeout: 0.5))
        #expect(r.timedOut)
        #expect(r.exitCode == nil)
    }

    @Test func logModeAppendsStdoutAndStderrWithHeader() async throws {
        let root = try makeTempDir("qsum-runner")
        defer { try? FileManager.default.removeItem(at: root) }
        let log = root.appendingPathComponent("logs/app-run.log")
        let cmd = ProcessCommand(executable: URL(fileURLWithPath: "/bin/sh"), arguments: ["-c", "echo out; echo err 1>&2; exit 2"],
                                 logFile: log, logHeader: "=== H ===")
        let r = await runner.run(cmd)
        #expect(r.exitCode == 2)
        #expect(r.stdout.isEmpty)                                    // went to the file
        let text = try read(log)
        #expect(text.hasPrefix("\n=== H ===\n"))
        #expect(text.contains("out\n") && text.contains("err\n"))
        _ = await runner.run(cmd)                                    // appended, not replaced
        #expect(try read(log).components(separatedBy: "=== H ===").count == 3)
    }
}
