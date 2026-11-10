//
// ProcessRunner.swift
// The only place that launches child processes. Everything else talks to the `ProcessRunner` protocol, so the
// tests inject a fake and never start a real script (no launchd, no Keychain, no Claude).
//

import Foundation

/// What to run.
struct ProcessCommand: Equatable {
    var executable: URL
    var arguments: [String]
    /// Added to (and overriding) the inherited environment of the app.
    var extraEnvironment: [String: String] = [:]
    /// nil = capture stdout in `ProcessResult.stdout` (stderr is dropped). Set = append stdout + stderr to this file.
    var logFile: URL? = nil
    /// First line written to the log file before the process starts (only with `logFile`).
    var logHeader: String? = nil
    /// Kill the process after this many seconds (nil = no limit).
    var timeout: TimeInterval? = nil
}

struct ProcessResult: Equatable {
    /// nil when the process never started, was killed by a signal, or timed out.
    var exitCode: Int32?
    var stdout: Data = Data()
    /// Why the process could not be launched (e.g. file missing), nil otherwise.
    var launchError: String? = nil
    var timedOut: Bool = false
}

protocol ProcessRunner {
    /// Run to completion (async: does not block the main thread).
    func run(_ command: ProcessCommand) async -> ProcessResult
}

/// Real implementation on top of Foundation.Process. stdin is always /dev/null.
struct SystemProcessRunner: ProcessRunner {
    func run(_ command: ProcessCommand) async -> ProcessResult {
        await withCheckedContinuation { (continuation: CheckedContinuation<ProcessResult, Never>) in
            // Blocking work (pipes, waitUntilExit) stays off the cooperative thread pool.
            DispatchQueue.global(qos: .userInitiated).async {
                continuation.resume(returning: SystemProcessRunner.runBlocking(command))
            }
        }
    }

    static func runBlocking(_ command: ProcessCommand) -> ProcessResult {
        let process = Process()
        process.executableURL = command.executable
        process.arguments = command.arguments
        process.environment = ProcessInfo.processInfo.environment.merging(command.extraEnvironment) { _, new in new }
        process.standardInput = FileHandle.nullDevice

        var logHandle: FileHandle?
        let outPipe = Pipe()
        if let logFile = command.logFile {
            do {
                logHandle = try LogFile.openForAppend(logFile, header: command.logHeader)
            } catch {
                return ProcessResult(exitCode: nil, launchError: "เปิดไฟล์ log ไม่ได้: \(error.localizedDescription)")
            }
            process.standardOutput = logHandle
            process.standardError = logHandle
        } else {
            process.standardOutput = outPipe
            process.standardError = FileHandle.nullDevice
        }
        defer { try? logHandle?.close() }

        // Read the pipe on another thread while the process runs, otherwise a big output blocks the child.
        let captured = Box(Data())
        let reader = DispatchGroup()
        if command.logFile == nil {
            reader.enter()
            DispatchQueue.global(qos: .userInitiated).async {
                captured.value = outPipe.fileHandleForReading.readDataToEndOfFile()
                reader.leave()
            }
        }

        do {
            try process.run()
        } catch {
            try? outPipe.fileHandleForWriting.close()      // lets the reader finish
            if command.logFile == nil { reader.wait() }
            return ProcessResult(exitCode: nil, launchError: error.localizedDescription)
        }
        // The parent's copy of the write end must be closed, or readDataToEndOfFile never sees EOF.
        if command.logFile == nil { try? outPipe.fileHandleForWriting.close() }

        let timedOut = Box(false)
        if let limit = command.timeout {
            let killer = DispatchWorkItem {
                timedOut.value = true
                process.terminate()
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + limit, execute: killer)
            process.waitUntilExit()
            killer.cancel()
        } else {
            process.waitUntilExit()
        }
        if command.logFile == nil { reader.wait() }

        let signalled = process.terminationReason == .uncaughtSignal
        return ProcessResult(exitCode: signalled ? nil : process.terminationStatus, stdout: captured.value,
                             launchError: nil, timedOut: timedOut.value)
    }
}

/// A tiny lock-protected cell for handing a value from a background thread back to the caller.
final class Box<T>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: T
    init(_ value: T) { stored = value }
    var value: T {
        get { lock.lock(); defer { lock.unlock() }; return stored }
        set { lock.lock(); defer { lock.unlock() }; stored = newValue }
    }
}

/// Appending to the run log.
enum LogFile {
    /// Above this size the log is cut down before a run (keeps ~Library/Logs small, like run.sh does for run.log).
    static let maxBytes: UInt64 = 1_048_576
    static let keepBytes: UInt64 = 262_144

    /// Create the folder, trim an oversized log, write the header and return a handle positioned at the end.
    static func openForAppend(_ url: URL, header: String?, fileManager: FileManager = .default) throws -> FileHandle {
        try fileManager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        if !fileManager.fileExists(atPath: url.path) {
            guard fileManager.createFile(atPath: url.path, contents: nil) else {
                throw CocoaError(.fileWriteUnknown)
            }
        }
        try trimIfNeeded(url)
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        if let header = header {
            // blank line first so runs are visually separated, then the header
            try handle.write(contentsOf: Data(("\n" + header + "\n").utf8))
        }
        return handle
    }

    /// If the file is bigger than `maxBytes`, keep only the last `keepBytes` (starting at a line start).
    static func trimIfNeeded(_ url: URL, maxBytes: UInt64 = LogFile.maxBytes, keepBytes: UInt64 = LogFile.keepBytes) throws {
        let attrs = try FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attrs[.size] as? NSNumber)?.uint64Value ?? 0
        guard size > maxBytes else { return }
        let reader = try FileHandle(forReadingFrom: url)
        defer { try? reader.close() }
        try reader.seek(toOffset: size - keepBytes)
        var tail = try reader.readToEnd() ?? Data()
        if let newline = tail.firstIndex(of: 0x0A) {       // drop the partial first line
            tail = tail.suffix(from: tail.index(after: newline))
        }
        try tail.write(to: url, options: .atomic)
    }
}
