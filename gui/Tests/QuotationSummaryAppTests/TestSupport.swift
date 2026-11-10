//
// TestSupport.swift
// Shared helpers: fixture loading, temp folders, a fake ProcessRunner.
//

import Foundation
@testable import QuotationSummaryApp

enum TestError: Error { case missingFixture(String) }

/// Load a fixture from Tests/QuotationSummaryAppTests/Fixtures.
func fixtureData(_ name: String) throws -> Data {
    guard let url = Bundle.module.url(forResource: name, withExtension: nil, subdirectory: "Fixtures") else {
        throw TestError.missingFixture(name)
    }
    return try Data(contentsOf: url)
}

func loadStatus(_ name: String) throws -> EngineStatus {
    try EngineStatus.decode(fixtureData(name))
}

/// A fresh empty temp folder (removed by the caller with `defer { try? FileManager.default.removeItem(at:) }`).
func makeTempDir(_ label: String = "qsum-gui-test") throws -> URL {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("\(label)-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

/// A fake engine folder: scripts/status.sh and scripts/run.sh exist (empty, never executed).
func makeFakeEngine() throws -> URL {
    let dir = try makeTempDir("qsum-gui-engine")
    let scripts = dir.appendingPathComponent("scripts", isDirectory: true)
    try FileManager.default.createDirectory(at: scripts, withIntermediateDirectories: true)
    for name in ["status.sh", "run.sh"] {
        try Data("#!/bin/bash\n".utf8).write(to: scripts.appendingPathComponent(name))
    }
    return dir
}

func write(_ text: String, to url: URL) throws {
    try Data(text.utf8).write(to: url)
}

func read(_ url: URL) throws -> String {
    String(decoding: try Data(contentsOf: url), as: UTF8.self)
}

/// Records every command it is given and answers from a script. NEVER starts a real process.
final class FakeRunner: ProcessRunner, @unchecked Sendable {
    private let lock = NSLock()
    private var _commands: [ProcessCommand] = []
    /// Called for each command; returns the canned result.
    var handler: (ProcessCommand) -> ProcessResult

    init(handler: @escaping (ProcessCommand) -> ProcessResult = { _ in ProcessResult(exitCode: 0) }) {
        self.handler = handler
    }

    var commands: [ProcessCommand] {
        lock.lock(); defer { lock.unlock() }
        return _commands
    }

    func run(_ command: ProcessCommand) async -> ProcessResult {
        lock.lock()
        _commands.append(command)
        lock.unlock()
        return handler(command)
    }
}

/// Opens nothing; remembers what it was asked to open.
final class FakeOpener: FileOpening {
    var workbooks: [URL] = []
    var folders: [URL] = []
    var result = true
    func openWorkbook(_ url: URL) -> Bool { workbooks.append(url); return result }
    func openFolder(_ url: URL) -> Bool { folders.append(url); return result }
}
