//
// StatusDecodingTests.swift
// The status JSON of the engine (real qsum.status output, saved as fixtures) decodes into the app's models.
//

import Foundation
import Testing
@testable import QuotationSummaryApp

@Suite("Status decoding")
struct StatusDecodingTests {
    @Test func fullFixtureDecodes() throws {
        let s = try loadStatus("status-v1.json")
        #expect(s.schema == 1)
        #expect(s.ok)
        #expect(s.errorCode == nil)
        #expect(s.engineVersion != nil)
        #expect(s.running == false)
        #expect(s.progress == nil)
        #expect(s.outputExists)
        #expect(s.outputFile?.hasSuffix("สรุปใบเสนอราคา.xlsx") == true)
        #expect(s.counts == Counts(total: 9, done: 3, check: 4, waiting: 1, error: 1))
        #expect(s.files.count == 9)
        // order is the engine's: error, waiting, check, done
        #expect(s.files.map(\.status) == [.error, .waiting, .check, .check, .check, .check, .done, .done, .done])
        let first = try #require(s.files.first)
        #expect(first.name == "เสีย อ่านไม่ได้.pdf")
        #expect(first.detail.contains("หมดเวลา"))
        #expect(first.sha256?.count == 64)
        #expect(first.willRetry)
        let done = try #require(s.files.first { $0.status == .done })
        #expect(done.method == "claude")
        #expect(done.quoteNo?.hasPrefix("QT") == true)
        #expect(done.company == "Company A")
        #expect(done.total == 1070.0)
        #expect(done.added == "2026-11-03")
        let text = try #require(s.files.first { $0.method == "text" })
        #expect(text.status == .check)
        #expect(text.willRetry)
    }

    @Test func lastRunAndSchedule() throws {
        let s = try loadStatus("status-v1.json")
        let run = try #require(s.lastRun)
        #expect(run.trigger == "schedule")
        #expect(run.exitCode == 2)
        #expect(run.interrupted == false)
        #expect(run.stats == RunStats(cached: 6, claude: 2, text: 0, failed: 1))
        #expect(run.issues.count == 1)
        let sched = try #require(s.schedule)
        #expect(sched.installed)
        #expect(sched.weekday == 1 && sched.hour == 8 && sched.minute == 0)
        #expect(sched.label == "com.korakod.quotation-summary")
        #expect(sched.nextRun == "2026-11-16T08:00:00+07:00")
    }

    @Test func runningFixture() throws {
        let s = try loadStatus("status-running.json")
        #expect(s.running)
        let p = try #require(s.progress)
        #expect(p.total == 9 && p.done == 2)
        #expect(p.currentFile == "ใหม่ ยังไม่ได้อ่าน.pdf")
        #expect(p.trigger == "app")
        #expect(s.lastRun?.interrupted == false)           // the previous run, not the one going now
    }

    @Test func errorFixtureHasAllFieldsAndNoFiles() throws {
        let s = try loadStatus("status-error.json")
        #expect(s.ok == false)
        #expect(s.errorCode == .sourceMissing)
        #expect(s.error?.contains("ไม่พบโฟลเดอร์") == true)
        #expect(s.files.isEmpty)
        #expect(s.counts == .zero)
        #expect(s.sourceFolder != nil)
        #expect(s.schedule?.installed == true)
    }

    @Test func notInstalledFixtureHasOnlyTheGuaranteedKeys() throws {
        let s = try loadStatus("status-not-installed.json")
        #expect(s.ok == false)
        #expect(s.errorCode == .notInstalled)
        #expect(s.generatedAt != nil)
        #expect(s.files.isEmpty)
        #expect(s.progress == nil && s.lastRun == nil && s.schedule == nil)
        #expect(s.running == false)
        #expect(s.counts == .zero)
    }

    // MARK: tolerance

    @Test func unknownKeysAreIgnored() throws {
        let json = #"{"schema":1,"ok":true,"future_field":{"a":1},"files":[{"name":"a.pdf","status":"done","new_thing":[1,2]}],"counts":{"total":1,"done":1,"check":0,"waiting":0,"error":0,"extra":5}}"#
        let s = try EngineStatus.decode(Data(json.utf8))
        #expect(s.ok)
        #expect(s.files.map(\.name) == ["a.pdf"])
        #expect(s.counts.done == 1)
    }

    @Test func missingFieldsGetDefaults() throws {
        let s = try EngineStatus.decode(Data("{}".utf8))
        #expect(s.ok)                                   // no error_code => ok
        #expect(s.running == false)
        #expect(s.files.isEmpty && s.counts == .zero)
        #expect(s.outputExists == false)
        let s2 = try EngineStatus.decode(Data(#"{"error_code":"config","error":"x"}"#.utf8))
        #expect(s2.ok == false)                         // an error code without "ok" still means not ok
        #expect(s2.errorCode == .config)
    }

    @Test func wrongTypesBecomeNilNotFailure() throws {
        let json = #"{"ok":"yes","running":"maybe","output_exists":1,"progress":{"total":"x","done":2},"schedule":"nope","files":[{"name":5,"status":"done"}]}"#
        let s = try EngineStatus.decode(Data(json.utf8))
        #expect(s.running == false)
        #expect(s.outputExists == false)
        #expect(s.progress?.total == 0 && s.progress?.done == 2)
        #expect(s.schedule == nil)
        #expect(s.files.count == 1 && s.files[0].name == "")
    }

    @Test func unknownRowStatusIsShownAsCheck() throws {
        let json = #"{"files":[{"name":"a.pdf","status":"quarantined"},{"name":"b.pdf"}]}"#
        let s = try EngineStatus.decode(Data(json.utf8))
        #expect(s.files.map(\.status) == [.check, .check])
        #expect(s.counts.check == 2 && s.counts.total == 2)         // counts derived from the rows when missing
    }

    @Test func aBrokenRowIsDroppedNotTheList() throws {
        let json = #"{"files":[{"name":"a.pdf","status":"done"},"garbage",{"name":"c.pdf","status":"waiting"}]}"#
        let s = try EngineStatus.decode(Data(json.utf8))
        #expect(s.files.map(\.name) == ["a.pdf", "c.pdf"])
    }

    @Test func unknownErrorCodeIsKept() throws {
        let s = try EngineStatus.decode(Data(#"{"ok":false,"error_code":"disk_full","error":"e"}"#.utf8))
        #expect(s.errorCode == .unknown("disk_full"))
    }

    @Test func notJSONThrows() {
        #expect(throws: (any Error).self) { try EngineStatus.decode(Data("hello".utf8)) }
        #expect(throws: (any Error).self) { try EngineStatus.decode(Data("[1,2]".utf8)) }
    }

    @Test func fileStatusRawValuesMatchTheContract() {
        #expect(Set(FileStatus.allCases.map(\.rawValue)) == ["done", "check", "waiting", "error"])
        #expect(ErrorCode(raw: "not_installed") == .notInstalled)
        #expect(ErrorCode(raw: "source_missing") == .sourceMissing)
        #expect(ErrorCode(raw: "source_unreadable") == .sourceUnreadable)
        #expect(ErrorCode(raw: "config") == .config)
    }
}
