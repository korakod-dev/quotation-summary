//
// StatusModels.swift
// Decodable mirror of the JSON printed by `python -m qsum.status --json` (contract C3, schema 1).
//
// Decoding is TOLERANT on purpose: every field is optional (a missing key, a null or a value of an
// unexpected type becomes nil / a default instead of failing the whole document) and unknown keys are
// ignored. A newer engine can therefore add fields without breaking an older app.
//

import Foundation

// MARK: - lenient decoding helper

extension KeyedDecodingContainer {
    /// Decode a value, returning nil when the key is missing, null, or of another type.
    func lenient<T: Decodable>(_ type: T.Type, forKey key: Key) -> T? {
        (try? decodeIfPresent(type, forKey: key)) ?? nil
    }
}

// MARK: - enums

/// files[].status. The raw values are part of the contract (checked by the Python contract test).
enum FileStatus: String, Equatable, CaseIterable {
    case error
    case waiting
    case check
    case done
}

/// error_code of the whole document. `.unknown` keeps a code a newer engine might add.
enum ErrorCode: Equatable {
    case notInstalled
    case config
    case sourceMissing
    case sourceUnreadable
    case unknown(String)

    init(raw: String) {
        switch raw {
        case "not_installed": self = .notInstalled
        case "config": self = .config
        case "source_missing": self = .sourceMissing
        case "source_unreadable": self = .sourceUnreadable
        default: self = .unknown(raw)
        }
    }
}

// MARK: - pieces

struct ProgressInfo: Equatable {
    var total: Int
    var done: Int
    var currentFile: String?
    var startedAt: String?
    var trigger: String?
}

struct RunStats: Equatable {
    var cached: Int
    var claude: Int
    var text: Int
    var failed: Int
}

struct LastRun: Equatable {
    var startedAt: String?
    var finishedAt: String?
    var trigger: String?
    var exitCode: Int?
    var interrupted: Bool
    var stats: RunStats?
    var issues: [String]
}

struct Schedule: Equatable {
    var installed: Bool
    var label: String?
    var weekday: Int?
    var hour: Int?
    var minute: Int?
    var nextRun: String?
}

struct Counts: Equatable {
    var total: Int
    var done: Int
    var check: Int
    var waiting: Int
    var error: Int

    static let zero = Counts(total: 0, done: 0, check: 0, waiting: 0, error: 0)
}

struct FileRow: Equatable, Identifiable {
    var name: String
    var sha256: String?
    var size: Int?
    var added: String?
    var status: FileStatus
    var method: String?
    var willRetry: Bool
    var detail: String
    var quoteNo: String?
    var company: String?
    var total: Double?

    /// The relative path inside the source folder is unique, so it is the row id.
    var id: String { name }
}

/// The whole status document.
struct EngineStatus: Equatable {
    var schema: Int?
    var engineVersion: String?
    var generatedAt: String?
    var ok: Bool
    var errorCode: ErrorCode?
    var error: String?
    var sourceFolder: String?
    var outputFile: String?
    var outputExists: Bool
    var outputMtime: String?
    var running: Bool
    var progress: ProgressInfo?
    var lastRun: LastRun?
    var schedule: Schedule?
    var counts: Counts
    var files: [FileRow]

    /// A status the app makes up itself when the engine cannot answer (no engine folder, garbage output, ...).
    static func synthetic(errorCode: ErrorCode, error: String) -> EngineStatus {
        EngineStatus(schema: nil, engineVersion: nil, generatedAt: nil, ok: false, errorCode: errorCode, error: error,
                     sourceFolder: nil, outputFile: nil, outputExists: false, outputMtime: nil, running: false,
                     progress: nil, lastRun: nil, schedule: nil, counts: .zero, files: [])
    }

    /// Decode the JSON printed by status.sh. Throws only when the data is not a JSON object at all.
    static func decode(_ data: Data) throws -> EngineStatus {
        try JSONDecoder().decode(EngineStatus.self, from: data)
    }
}

// MARK: - Decodable (in extensions, so the memberwise initialisers stay available)

extension ProgressInfo: Decodable {
    enum CodingKeys: String, CodingKey { case total, done, currentFile = "current_file", startedAt = "started_at", trigger }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = c.lenient(Int.self, forKey: .total) ?? 0
        done = c.lenient(Int.self, forKey: .done) ?? 0
        currentFile = c.lenient(String.self, forKey: .currentFile)
        startedAt = c.lenient(String.self, forKey: .startedAt)
        trigger = c.lenient(String.self, forKey: .trigger)
    }
}

extension RunStats: Decodable {
    enum CodingKeys: String, CodingKey { case cached, claude, text, failed }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        cached = c.lenient(Int.self, forKey: .cached) ?? 0
        claude = c.lenient(Int.self, forKey: .claude) ?? 0
        text = c.lenient(Int.self, forKey: .text) ?? 0
        failed = c.lenient(Int.self, forKey: .failed) ?? 0
    }
}

extension LastRun: Decodable {
    enum CodingKeys: String, CodingKey {
        case startedAt = "started_at", finishedAt = "finished_at", trigger
        case exitCode = "exit_code", interrupted, stats, issues
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        startedAt = c.lenient(String.self, forKey: .startedAt)
        finishedAt = c.lenient(String.self, forKey: .finishedAt)
        trigger = c.lenient(String.self, forKey: .trigger)
        exitCode = c.lenient(Int.self, forKey: .exitCode)
        interrupted = c.lenient(Bool.self, forKey: .interrupted) ?? false
        stats = c.lenient(RunStats.self, forKey: .stats)
        issues = c.lenient([String].self, forKey: .issues) ?? []
    }
}

extension Schedule: Decodable {
    enum CodingKeys: String, CodingKey { case installed, label, weekday, hour, minute, nextRun = "next_run" }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        installed = c.lenient(Bool.self, forKey: .installed) ?? false
        label = c.lenient(String.self, forKey: .label)
        weekday = c.lenient(Int.self, forKey: .weekday)
        hour = c.lenient(Int.self, forKey: .hour)
        minute = c.lenient(Int.self, forKey: .minute)
        nextRun = c.lenient(String.self, forKey: .nextRun)
    }
}

extension Counts: Decodable {
    enum CodingKeys: String, CodingKey { case total, done, check, waiting, error }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        total = c.lenient(Int.self, forKey: .total) ?? 0
        done = c.lenient(Int.self, forKey: .done) ?? 0
        check = c.lenient(Int.self, forKey: .check) ?? 0
        waiting = c.lenient(Int.self, forKey: .waiting) ?? 0
        error = c.lenient(Int.self, forKey: .error) ?? 0
    }
}

extension FileRow: Decodable {
    enum CodingKeys: String, CodingKey {
        case name, sha256, size, added, status, method
        case willRetry = "will_retry", detail
        case quoteNo = "quote_no", company, total
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = c.lenient(String.self, forKey: .name) ?? ""
        sha256 = c.lenient(String.self, forKey: .sha256)
        size = c.lenient(Int.self, forKey: .size)
        added = c.lenient(String.self, forKey: .added)
        // An unknown status from a newer engine is shown as "needs checking": the safe reading for the user.
        status = FileStatus(rawValue: c.lenient(String.self, forKey: .status) ?? "") ?? .check
        method = c.lenient(String.self, forKey: .method)
        willRetry = c.lenient(Bool.self, forKey: .willRetry) ?? false
        detail = c.lenient(String.self, forKey: .detail) ?? ""
        quoteNo = c.lenient(String.self, forKey: .quoteNo)
        company = c.lenient(String.self, forKey: .company)
        total = c.lenient(Double.self, forKey: .total)
    }
}

extension EngineStatus: Decodable {
    enum CodingKeys: String, CodingKey {
        case schema, engineVersion = "engine_version", generatedAt = "generated_at", ok
        case errorCode = "error_code", error
        case sourceFolder = "source_folder", outputFile = "output_file", outputExists = "output_exists"
        case outputMtime = "output_mtime", running, progress, lastRun = "last_run", schedule, counts, files
    }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = c.lenient(Int.self, forKey: .schema)
        engineVersion = c.lenient(String.self, forKey: .engineVersion)
        generatedAt = c.lenient(String.self, forKey: .generatedAt)
        errorCode = c.lenient(String.self, forKey: .errorCode).map(ErrorCode.init(raw:))
        error = c.lenient(String.self, forKey: .error)
        // No explicit "ok": it is ok exactly when there is no error code.
        ok = c.lenient(Bool.self, forKey: .ok) ?? (errorCode == nil)
        sourceFolder = c.lenient(String.self, forKey: .sourceFolder)
        outputFile = c.lenient(String.self, forKey: .outputFile)
        outputExists = c.lenient(Bool.self, forKey: .outputExists) ?? false
        outputMtime = c.lenient(String.self, forKey: .outputMtime)
        running = c.lenient(Bool.self, forKey: .running) ?? false
        progress = c.lenient(ProgressInfo.self, forKey: .progress)
        lastRun = c.lenient(LastRun.self, forKey: .lastRun)
        schedule = c.lenient(Schedule.self, forKey: .schedule)
        // Rows that cannot be decoded are dropped one by one, not the whole list.
        files = (c.lenient([LossyRow].self, forKey: .files) ?? []).compactMap { $0.row }
        counts = c.lenient(Counts.self, forKey: .counts) ?? Counts.derived(from: files)
    }
}

/// One element of files[] that may fail to decode without taking the others down.
private struct LossyRow: Decodable {
    let row: FileRow?
    init(from decoder: Decoder) throws {
        row = try? FileRow(from: decoder)
    }
}

extension Counts {
    /// Counts computed from the rows, used when the document has no "counts".
    static func derived(from rows: [FileRow]) -> Counts {
        var c = Counts.zero
        c.total = rows.count
        for r in rows {
            switch r.status {
            case .done: c.done += 1
            case .check: c.check += 1
            case .waiting: c.waiting += 1
            case .error: c.error += 1
            }
        }
        return c
    }
}
