//
// Labels.swift
// All Thai wording of the window in one place, as pure functions (so the tests can check the text without a UI).
//

import Foundation

enum Labels {
    // MARK: file status badges

    /// Text of the status badge in the table.
    static func badge(_ status: FileStatus) -> String {
        switch status {
        case .done: return "ทำแล้ว"
        case .check: return "ควรตรวจ"
        case .waiting: return "รอรอบถัดไป"
        case .error: return "อ่านไม่ได้"
        }
    }

    /// Plain-language explanation of a badge (tooltip).
    static func badgeHelp(_ status: FileStatus) -> String {
        switch status {
        case .done: return "อ่านด้วย Claude แล้ว และข้อมูลดูปกติ"
        case .check: return "อ่านแล้ว แต่ควรเทียบกับ PDF อีกครั้ง (ดูรายละเอียด)"
        case .waiting: return "ยังไม่ได้อ่าน จะอ่านในรอบถัดไป"
        case .error: return "รอบที่แล้วอ่านไฟล์นี้ไม่สำเร็จ รอบถัดไปจะลองใหม่"
        }
    }

    // MARK: filter

    /// nil = show every file.
    static func filterTitle(_ filter: FileStatus?) -> String {
        guard let filter = filter else { return "ทั้งหมด" }
        return badge(filter)
    }

    /// Text shown next to the filter, e.g. "ทั้งหมด 9" or "ควรตรวจ 4".
    static func filterTitle(_ filter: FileStatus?, counts: Counts) -> String {
        let n: Int
        switch filter {
        case nil: n = counts.total
        case .some(.done): n = counts.done
        case .some(.check): n = counts.check
        case .some(.waiting): n = counts.waiting
        case .some(.error): n = counts.error
        }
        return "\(filterTitle(filter)) \(n)"
    }

    /// One line with all four counts for the table footer.
    static func countsLine(_ c: Counts) -> String {
        "ทั้งหมด \(c.total) ไฟล์: ทำแล้ว \(c.done), ควรตรวจ \(c.check), รอรอบถัดไป \(c.waiting), อ่านไม่ได้ \(c.error)"
    }

    // MARK: header

    /// "กำลังทำงาน 3/10: <file>" while a run is going.
    static func runningLine(_ p: ProgressInfo?) -> String {
        guard let p = p, p.total > 0 else { return "กำลังเริ่มทำงาน..." }
        if let file = p.currentFile, !file.isEmpty {
            // done = files finished, so the one in progress is the next number (never above the total)
            return "กำลังทำงาน \(min(p.done + 1, p.total))/\(p.total): \(file)"
        }
        return "กำลังทำงาน \(p.done)/\(p.total)"
    }

    static func triggerName(_ trigger: String?) -> String {
        switch trigger {
        case "schedule": return "ตั้งเวลา"
        case "app": return "กดจากแอป"
        case "manual": return "สั่งเอง"
        default: return "ไม่ทราบที่มา"
        }
    }

    /// Short result words of a finished run.
    static func resultName(_ run: LastRun) -> String {
        if run.interrupted { return "สะดุดกลางทาง (โปรแกรมถูกปิดหรือเครื่องดับ)" }
        switch run.exitCode {
        case 0: return "เรียบร้อย"
        case 2: return "เสร็จแล้ว แต่มี PDF บางไฟล์อ่านไม่ได้"
        case 1: return "ไม่สำเร็จ"
        case .some(let code): return "จบด้วยรหัส \(code)"
        case nil: return "ไม่ทราบผล"
        }
    }

    /// "รอบล่าสุด: จันทร์ 9 พ.ย. 08:00 (ตั้งเวลา): เรียบร้อย" or "ยังไม่เคยรัน".
    static func lastRunLine(_ run: LastRun?, timeZone: TimeZone = .current) -> String {
        guard let run = run else { return "ยังไม่เคยรัน" }
        let when = DateText.parse(run.finishedAt ?? run.startedAt).map { DateText.thai($0, timeZone: timeZone) }
        let head = when.map { "รอบล่าสุด: \($0)" } ?? "รอบล่าสุด"
        return "\(head) (\(triggerName(run.trigger))): \(resultName(run))"
    }

    /// "ครั้งถัดไป: จันทร์ 17 พ.ย. 08:00 (ถ้าเครื่องหลับ จะรันตอนเครื่องตื่น)".
    static func nextRunLine(_ schedule: Schedule?, timeZone: TimeZone = .current) -> String {
        guard let s = schedule, s.installed else { return "ยังไม่ได้ตั้งเวลารันอัตโนมัติ" }
        guard let next = DateText.parse(s.nextRun) else { return "ตั้งเวลารันอัตโนมัติแล้ว" }
        return "ครั้งถัดไป: \(DateText.thai(next, timeZone: timeZone)) (ถ้าเครื่องหลับ จะรันตอนเครื่องตื่น)"
    }

    // MARK: banners

    enum BannerKind: Equatable {
        case info, success, warning, error
    }

    struct Banner: Equatable {
        var kind: BannerKind
        var text: String
    }

    /// The banner for an error_code of the status, nil when everything is fine.
    static func banner(for status: EngineStatus) -> Banner? {
        guard !status.ok || status.errorCode != nil else { return nil }
        let detail = status.error ?? ""
        switch status.errorCode {
        case .some(.notInstalled):
            return Banner(kind: .warning, text: "ยังไม่ได้ติดตั้ง: ดับเบิลคลิก ติดตั้ง.command ก่อน")
        case .some(.config):
            return Banner(kind: .error, text: "ไฟล์ตั้งค่า (config.json) มีปัญหา: \(detail) ให้ขอไฟล์ใหม่จากผู้ดูแล")
        case .some(.sourceMissing):
            return Banner(kind: .error, text: "ไม่พบโฟลเดอร์ PDF: \(status.sourceFolder ?? detail) แก้ source_folder ใน config.json หรือดับเบิลคลิก ติดตั้ง.command เพื่อสร้างโฟลเดอร์")
        case .some(.sourceUnreadable):
            return Banner(kind: .error, text: "อ่านโฟลเดอร์ PDF ไม่ได้ (macOS อาจกันสิทธิ์ไว้): \(detail)")
        case .some(.unknown(let code)):
            return Banner(kind: .error, text: "สถานะผิดปกติ (\(code)): \(detail)")
        case nil:
            return Banner(kind: .error, text: detail.isEmpty ? "อ่านสถานะจากโปรแกรมไม่ได้" : detail)
        }
    }

    // MARK: running the engine

    /// Message after the app-started run ended (contract C1 exit codes; 3 = lock busy).
    static func runResultMessage(exitCode: Int32?, launchError: String? = nil) -> Banner {
        if let launchError = launchError {
            return Banner(kind: .error, text: "สั่งรันไม่สำเร็จ: \(launchError)")
        }
        switch exitCode {
        case .some(0): return Banner(kind: .success, text: "สรุปเสร็จแล้ว")
        case .some(1): return Banner(kind: .error, text: "สรุปไม่สำเร็จ ดูสาเหตุที่รอบล่าสุดด้านบน (ไฟล์ log: \(RunLog.displayPath))")
        case .some(2): return Banner(kind: .warning, text: "สรุปเสร็จแล้ว แต่มี PDF บางไฟล์อ่านไม่ได้ ดูไฟล์สถานะ \"อ่านไม่ได้\" ในตาราง")
        case .some(3): return Banner(kind: .warning, text: "กำลังทำงานอยู่แล้ว (อาจเป็นรอบตั้งเวลา)")
        case .some(let code): return Banner(kind: .error, text: "สรุปไม่สำเร็จ (รหัสออก \(code)) ดูไฟล์ log: \(RunLog.displayPath)")
        case nil: return Banner(kind: .error, text: "สรุปไม่สำเร็จ: โปรแกรมถูกหยุดกลางทาง")
        }
    }

    // MARK: import

    /// "เพิ่ม N ไฟล์แล้ว กด เริ่มสรุปตอนนี้ หรือรอรอบตั้งเวลา" plus notes about skipped files.
    static func importBanner(_ results: [ImportResult]) -> Banner {
        var added = 0, already = 0, notPDF = 0, failed = 0
        var firstFailure = ""
        for r in results {
            switch r {
            case .copied, .renamed: added += 1
            case .alreadyThere: already += 1
            case .notPDF: notPDF += 1
            case .failed(let name, let message):
                failed += 1
                if firstFailure.isEmpty { firstFailure = "\(name): \(message)" }
            }
        }
        var parts: [String] = []
        if added > 0 { parts.append("เพิ่ม \(added) ไฟล์แล้ว กด เริ่มสรุปตอนนี้ หรือรอรอบตั้งเวลา") }
        if already > 0 { parts.append("มีไฟล์เดียวกันอยู่แล้ว \(already) ไฟล์ (ข้าม)") }
        if notPDF > 0 { parts.append("ข้าม \(notPDF) ไฟล์ที่ไม่ใช่ PDF") }
        if failed > 0 { parts.append("คัดลอกไม่สำเร็จ \(failed) ไฟล์ (\(firstFailure))") }
        if parts.isEmpty { parts.append("ไม่มีไฟล์ให้เพิ่ม") }
        let kind: BannerKind = failed > 0 ? .error : (added > 0 ? .success : .info)
        return Banner(kind: kind, text: parts.joined(separator: " | "))
    }
}

// MARK: - dates

enum DateText {
    private static let weekdays = ["อาทิตย์", "จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์"]
    private static let months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
                                 "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]

    /// Parse an ISO 8601 timestamp with offset (what the engine writes, e.g. 2026-11-16T08:00:00+07:00).
    static func parse(_ s: String?) -> Date? {
        guard let s = s, !s.isEmpty else { return nil }
        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let d = plain.date(from: s) { return d }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: s)
    }

    /// "2026-11-03" -> "3 พ.ย. 2026" (the engine's "added" date). Anything else is returned unchanged.
    static func thaiDate(_ iso: String?) -> String {
        guard let iso = iso else { return "" }
        let parts = iso.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, (1...12).contains(parts[1]) else { return iso }
        return "\(parts[2]) \(months[parts[1] - 1]) \(parts[0])"
    }

    /// "จันทร์ 17 พ.ย. 08:00" (weekday, day, month, time; no year, the Buddhist/Gregorian choice is avoided on purpose).
    static func thai(_ date: Date, timeZone: TimeZone = .current) -> String {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        let c = cal.dateComponents([.weekday, .day, .month, .hour, .minute], from: date)
        let wd = weekdays[(c.weekday ?? 1) - 1]
        let mo = months[(c.month ?? 1) - 1]
        return String(format: "%@ %d %@ %02d:%02d", wd, c.day ?? 0, mo, c.hour ?? 0, c.minute ?? 0)
    }
}
