//
// LabelTests.swift
// Thai wording: badges, header lines, banners, exit-code messages, import summary.
//

import Foundation
import Testing
@testable import QuotationSummaryApp

@Suite("Labels")
struct LabelTests {
    let bangkok = TimeZone(secondsFromGMT: 7 * 3600)!

    @Test func badges() {
        #expect(Labels.badge(.done) == "ทำแล้ว")
        #expect(Labels.badge(.check) == "ควรตรวจ")
        #expect(Labels.badge(.waiting) == "รอรอบถัดไป")
        #expect(Labels.badge(.error) == "อ่านไม่ได้")
        for s in FileStatus.allCases { #expect(!Labels.badgeHelp(s).isEmpty) }
    }

    @Test func filterTitlesAndCounts() {
        let c = Counts(total: 9, done: 3, check: 4, waiting: 1, error: 1)
        #expect(Labels.filterTitle(nil) == "ทั้งหมด")
        #expect(Labels.filterTitle(nil, counts: c) == "ทั้งหมด 9")
        #expect(Labels.filterTitle(.done, counts: c) == "ทำแล้ว 3")
        #expect(Labels.filterTitle(.check, counts: c) == "ควรตรวจ 4")
        #expect(Labels.filterTitle(.waiting, counts: c) == "รอรอบถัดไป 1")
        #expect(Labels.filterTitle(.error, counts: c) == "อ่านไม่ได้ 1")
        #expect(Labels.countsLine(c) == "ทั้งหมด 9 ไฟล์: ทำแล้ว 3, ควรตรวจ 4, รอรอบถัดไป 1, อ่านไม่ได้ 1")
    }

    @Test func runningLine() {
        let p = ProgressInfo(total: 10, done: 2, currentFile: "ใบ A.pdf", startedAt: nil, trigger: "app")
        #expect(Labels.runningLine(p) == "กำลังทำงาน 3/10: ใบ A.pdf")                  // the 3rd file is in progress
        #expect(Labels.runningLine(ProgressInfo(total: 10, done: 10, currentFile: "x.pdf", startedAt: nil, trigger: nil))
                == "กำลังทำงาน 10/10: x.pdf")                                            // never above the total
        #expect(Labels.runningLine(ProgressInfo(total: 10, done: 4, currentFile: nil, startedAt: nil, trigger: nil))
                == "กำลังทำงาน 4/10")
        #expect(Labels.runningLine(ProgressInfo(total: 0, done: 0, currentFile: nil, startedAt: nil, trigger: nil))
                == "กำลังเริ่มทำงาน...")
        #expect(Labels.runningLine(nil) == "กำลังเริ่มทำงาน...")
    }

    @Test func nextRunLineMatchesTheSpecExample() throws {
        let s = try loadStatus("status-v1.json")               // next_run 2026-11-16T08:00:00+07:00 = a Monday
        #expect(Labels.nextRunLine(s.schedule, timeZone: bangkok)
                == "ครั้งถัดไป: จันทร์ 16 พ.ย. 08:00 (ถ้าเครื่องหลับ จะรันตอนเครื่องตื่น)")
        let sun = Schedule(installed: true, label: "l", weekday: 0, hour: 8, minute: 5, nextRun: "2026-11-15T08:05:00+07:00")
        #expect(Labels.nextRunLine(sun, timeZone: bangkok) == "ครั้งถัดไป: อาทิตย์ 15 พ.ย. 08:05 (ถ้าเครื่องหลับ จะรันตอนเครื่องตื่น)")
        #expect(Labels.nextRunLine(nil) == "ยังไม่ได้ตั้งเวลารันอัตโนมัติ")
        #expect(Labels.nextRunLine(Schedule(installed: false, label: nil, weekday: nil, hour: nil, minute: nil, nextRun: nil))
                == "ยังไม่ได้ตั้งเวลารันอัตโนมัติ")
        #expect(Labels.nextRunLine(Schedule(installed: true, label: "l", weekday: 1, hour: 8, minute: 0, nextRun: nil))
                == "ตั้งเวลารันอัตโนมัติแล้ว")
    }

    @Test func nextRunIsShownInTheViewersTimeZone() {
        // 08:00 in Bangkok is 01:00 in UTC: the line follows the zone passed in (the Mac's zone in the app)
        let s = Schedule(installed: true, label: "l", weekday: 1, hour: 8, minute: 0, nextRun: "2026-11-16T08:00:00+07:00")
        #expect(Labels.nextRunLine(s, timeZone: TimeZone(secondsFromGMT: 0)!).contains("01:00"))
    }

    @Test func lastRunLine() throws {
        let s = try loadStatus("status-v1.json")
        #expect(Labels.lastRunLine(s.lastRun, timeZone: bangkok)
                == "รอบล่าสุด: จันทร์ 9 พ.ย. 08:04 (ตั้งเวลา): เสร็จแล้ว แต่มี PDF บางไฟล์อ่านไม่ได้")
        #expect(Labels.lastRunLine(nil) == "ยังไม่เคยรัน")
        let ok = LastRun(startedAt: "2026-11-10T09:00:00+07:00", finishedAt: "2026-11-10T09:01:00+07:00", trigger: "app",
                         exitCode: 0, interrupted: false, stats: nil, issues: [])
        #expect(Labels.lastRunLine(ok, timeZone: bangkok) == "รอบล่าสุด: อังคาร 10 พ.ย. 09:01 (กดจากแอป): เรียบร้อย")
        let crashed = LastRun(startedAt: "2026-11-10T09:00:00+07:00", finishedAt: nil, trigger: "manual",
                              exitCode: nil, interrupted: true, stats: nil, issues: [])
        #expect(Labels.lastRunLine(crashed, timeZone: bangkok).contains("สะดุดกลางทาง"))
        #expect(Labels.lastRunLine(crashed, timeZone: bangkok).contains("สั่งเอง"))
        let failed = LastRun(startedAt: nil, finishedAt: nil, trigger: nil, exitCode: 1, interrupted: false, stats: nil, issues: [])
        #expect(Labels.lastRunLine(failed) == "รอบล่าสุด (ไม่ทราบที่มา): ไม่สำเร็จ")
    }

    @Test func thaiDates() {
        #expect(DateText.thaiDate("2026-11-03") == "3 พ.ย. 2026")
        #expect(DateText.thaiDate("2026-01-15T10:00:00") == "15 ม.ค. 2026")
        #expect(DateText.thaiDate("garbage") == "garbage")
        #expect(DateText.thaiDate(nil) == "")
        #expect(DateText.parse("2026-11-16T08:00:00+07:00") != nil)
        #expect(DateText.parse("2026-11-16T08:00:00.123+07:00") != nil)
        #expect(DateText.parse("nonsense") == nil)
        #expect(DateText.parse(nil) == nil)
    }

    // MARK: banners

    @Test func bannerPerErrorCode() throws {
        #expect(Labels.banner(for: try loadStatus("status-v1.json")) == nil)
        #expect(Labels.banner(for: try loadStatus("status-running.json")) == nil)
        let notInstalled = try #require(Labels.banner(for: try loadStatus("status-not-installed.json")))
        #expect(notInstalled.text == "ยังไม่ได้ติดตั้ง: ดับเบิลคลิก ติดตั้ง.command ก่อน")
        #expect(notInstalled.kind == .warning)
        let missing = try #require(Labels.banner(for: try loadStatus("status-error.json")))
        #expect(missing.kind == .error)
        #expect(missing.text.contains("ไม่พบโฟลเดอร์ PDF"))
        #expect(missing.text.contains("ใบเสนอราคาที่ขายงานได้"))
        let cfg = Labels.banner(for: .synthetic(errorCode: .config, error: "JSON ผิดรูปแบบ"))
        #expect(cfg?.text.contains("JSON ผิดรูปแบบ") == true)
        let unreadable = Labels.banner(for: .synthetic(errorCode: .sourceUnreadable, error: "Operation not permitted"))
        #expect(unreadable?.text.contains("macOS") == true)
        let unknown = Labels.banner(for: .synthetic(errorCode: .unknown("disk_full"), error: "e"))
        #expect(unknown?.text.contains("disk_full") == true)
    }

    // MARK: run messages

    @Test func exitCodeMessages() {
        #expect(Labels.runResultMessage(exitCode: 0).kind == .success)
        #expect(Labels.runResultMessage(exitCode: 0).text == "สรุปเสร็จแล้ว")
        #expect(Labels.runResultMessage(exitCode: 1).kind == .error)
        #expect(Labels.runResultMessage(exitCode: 1).text.contains("app-run.log"))
        #expect(Labels.runResultMessage(exitCode: 2).kind == .warning)
        #expect(Labels.runResultMessage(exitCode: 2).text.contains("อ่านไม่ได้"))
        #expect(Labels.runResultMessage(exitCode: 3).text == "กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา)")
        #expect(Labels.runResultMessage(exitCode: 3).kind == .warning)
        #expect(Labels.runResultMessage(exitCode: 42).text.contains("42"))
        #expect(Labels.runResultMessage(exitCode: nil).kind == .error)
        #expect(Labels.runResultMessage(exitCode: nil, launchError: "no such file").text.contains("no such file"))
    }

    // MARK: import banner

    @Test func importBanner() {
        let two = Labels.importBanner([.copied("a.pdf"), .renamed("b (2).pdf")])
        #expect(two.text == "เพิ่ม 2 ไฟล์แล้ว กด เริ่มสรุปตอนนี้ หรือรอรอบตั้งเวลา")
        #expect(two.kind == .success)
        let mixed = Labels.importBanner([.copied("a.pdf"), .alreadyThere("b.pdf"), .notPDF("c.txt"), .failed("d.pdf", "disk full")])
        #expect(mixed.text.contains("เพิ่ม 1 ไฟล์แล้ว"))
        #expect(mixed.text.contains("มีไฟล์เดียวกันอยู่แล้ว 1"))
        #expect(mixed.text.contains("ข้าม 1 ไฟล์ที่ไม่ใช่ PDF"))
        #expect(mixed.text.contains("d.pdf: disk full"))
        #expect(mixed.kind == .error)
        let none = Labels.importBanner([.alreadyThere("a.pdf")])
        #expect(none.kind == .info)
        #expect(!none.text.contains("เพิ่ม"))
        #expect(Labels.importBanner([]).text == "ไม่มีไฟล์ให้เพิ่ม")
    }
}
