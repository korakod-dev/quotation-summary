//
// EngineService.swift
// Talks to the Python engine through two scripts (contracts C3 and C4). No business logic lives here.
//   status()   /bin/bash <engine>/scripts/status.sh      -> EngineStatus (read-only, safe to poll)
//   startRun() /bin/bash <engine>/scripts/run.sh         -> exit code (blocks until the run ends)
//

import Foundation

struct EngineService {
    let locator: EngineLocator
    let runner: ProcessRunner
    let logDirectory: URL
    /// A status call that takes longer than this is abandoned (hashing a big folder is still far below it).
    var statusTimeout: TimeInterval = 60

    init(locator: EngineLocator, runner: ProcessRunner,
         logDirectory: URL = RunLog.defaultDirectory(), statusTimeout: TimeInterval = 60) {
        self.locator = locator
        self.runner = runner
        self.logDirectory = logDirectory
        self.statusTimeout = statusTimeout
    }

    // MARK: status

    /// The command used for polling.
    var statusCommand: ProcessCommand {
        ProcessCommand(executable: URL(fileURLWithPath: "/bin/bash"), arguments: [locator.statusScript.path],
                       timeout: statusTimeout)
    }

    /// Ask the engine for its status. Never throws: any failure becomes a status with an error code, so the
    /// window can always show something readable.
    func status() async -> EngineStatus {
        guard locator.isInstalled() else {
            return .synthetic(errorCode: .notInstalled,
                              error: "ยังไม่ได้ติดตั้ง: ไม่พบโปรแกรมที่ \(locator.directory.path)")
        }
        let result = await runner.run(statusCommand)
        return Self.interpret(result)
    }

    /// Turn the process result of status.sh into a status. Pure (tested directly).
    static func interpret(_ result: ProcessResult) -> EngineStatus {
        if let launchError = result.launchError {
            return .synthetic(errorCode: .unknown("launch"), error: "เรียกโปรแกรมไม่ได้: \(launchError)")
        }
        if result.timedOut {
            return .synthetic(errorCode: .unknown("timeout"), error: "โปรแกรมตอบช้าเกินไป (เกินเวลาที่กำหนด)")
        }
        // status.sh exits 1 for "not ok" but still prints the JSON, so the exit code does not decide anything.
        guard !result.stdout.isEmpty, let status = try? EngineStatus.decode(result.stdout) else {
            let code = result.exitCode.map(String.init) ?? "-"
            return .synthetic(errorCode: .unknown("bad_output"),
                              error: "อ่านสถานะจากโปรแกรมไม่ได้ (รหัสออก \(code)) ลองรัน ตรวจระบบ.command")
        }
        return status
    }

    // MARK: run

    /// The command for "เริ่มสรุปตอนนี้": run.sh with QS_TRIGGER=app, output appended to the app log.
    func runCommand(now: Date = Date()) -> ProcessCommand {
        ProcessCommand(executable: URL(fileURLWithPath: "/bin/bash"), arguments: [locator.runScript.path],
                       extraEnvironment: ["QS_TRIGGER": "app"],
                       logFile: logDirectory.appendingPathComponent(RunLog.fileName),
                       logHeader: RunLog.header(at: now))
    }

    /// Start a real run and wait for it to end. Progress is NOT read from here: the caller polls status().
    func startRun() async -> ProcessResult {
        guard locator.isInstalled() else {
            return ProcessResult(exitCode: nil, launchError: "ยังไม่ได้ติดตั้งโปรแกรม (ไม่พบ \(locator.runScript.path))")
        }
        return await runner.run(runCommand())
    }
}
