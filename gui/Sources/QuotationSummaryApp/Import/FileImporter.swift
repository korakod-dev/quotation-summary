//
// FileImporter.swift
// Copies dropped / chosen PDFs into the engine's source folder. Never overwrites anything.
//
// Rules, per file:
//   not a PDF (extension)                              -> .notPDF
//   name starts with "." or "~$" (the engine always skips these) -> .failed (rename it first)
//   no file with that name in the folder                -> copy                          -> .copied
//   same name, same content (SHA-256)                   -> nothing to do                 -> .alreadyThere
//   same name, different content                        -> copy as "name (2).pdf"        -> .renamed("name (2).pdf")
//     (if "name (2).pdf" is taken, try (3), (4), ...; if one of them has the same content -> .alreadyThere)
//   any I/O problem                                     -> .failed
//

import CryptoKit
import Foundation

enum ImportResult: Equatable {
    /// Copied under its own name (the name in the folder).
    case copied(String)
    /// Same name and same content were already in the folder: skipped.
    case alreadyThere(String)
    /// Name was taken by a different file: copied as the new name (e.g. "ใบเสนอราคา (2).pdf").
    case renamed(String)
    /// Not a .pdf file (original file name).
    case notPDF(String)
    /// Could not copy: (file name, reason in Thai).
    case failed(String, String)
}

enum FileImporter {
    /// Import the given files into `folder` (which must already exist; it is never created here, because a wrong
    /// path would then silently create a stray folder).
    static func importPDFs(_ urls: [URL], into folder: URL, fileManager: FileManager = .default) -> [ImportResult] {
        var isDir: ObjCBool = false
        let folderOK = fileManager.fileExists(atPath: folder.path, isDirectory: &isDir) && isDir.boolValue
        return urls.map { url in
            let name = url.lastPathComponent
            guard url.pathExtension.lowercased() == "pdf" else { return .notPDF(name) }
            guard !hasIgnoredPrefix(name) else {
                return .failed(name, "ชื่อไฟล์ขึ้นต้นด้วย . หรือ ~$ โปรแกรมสรุปจะข้ามไฟล์แบบนี้เสมอ เปลี่ยนชื่อไฟล์ก่อนแล้วลากมาใหม่")
            }
            guard folderOK else { return .failed(name, "ไม่พบโฟลเดอร์ปลายทาง \(folder.path)") }
            var srcIsDir: ObjCBool = false
            guard fileManager.fileExists(atPath: url.path, isDirectory: &srcIsDir), !srcIsDir.boolValue else {
                return .notPDF(name)       // a folder named x.pdf, or a file that vanished
            }
            do {
                return try importOne(url, into: folder, fileManager: fileManager)
            } catch {
                return .failed(name, (error as? ImportError)?.message ?? error.localizedDescription)
            }
        }
    }

    /// The engine's list_pdfs skips names starting with "." (hidden) or "~$" (Office lock files); keep both in sync.
    static func hasIgnoredPrefix(_ name: String) -> Bool {
        name.hasPrefix(".") || name.hasPrefix("~$")
    }

    /// Prefix of the temporary copy. Starts with "." so the engine (list_pdfs) and a status check never see it.
    static let tempPrefix = ".qs-import-"

    private struct ImportError: Error { let message: String }

    private static func importOne(_ src: URL, into folder: URL, fileManager: FileManager) throws -> ImportResult {
        let name = src.lastPathComponent
        let srcHash = try sha256(of: src)
        let base = (name as NSString).deletingPathExtension
        let ext = (name as NSString).pathExtension          // keeps the original case, e.g. "PDF"
        var n = 1
        while n < 1000 {
            let candidate = n == 1 ? name : "\(base) (\(n)).\(ext)"
            let dest = folder.appendingPathComponent(candidate)
            if fileManager.fileExists(atPath: dest.path) {
                if (try? sha256(of: dest)) == srcHash { return .alreadyThere(candidate) }
                n += 1
                continue
            }
            // Copy to a hidden temporary name first, then rename: a run or status check going on right now never sees
            // a half-copied PDF. moveItem refuses to overwrite: if the name appeared meanwhile, try the next one.
            let temp = folder.appendingPathComponent("\(tempPrefix)\(UUID().uuidString).pdf")
            do {
                try fileManager.copyItem(at: src, to: temp)
                try fileManager.moveItem(at: temp, to: dest)
            } catch let error as CocoaError where error.code == .fileWriteFileExists {
                try? fileManager.removeItem(at: temp)
                continue
            } catch {
                try? fileManager.removeItem(at: temp)
                throw error
            }
            return n == 1 ? .copied(candidate) : .renamed(candidate)
        }
        throw ImportError(message: "หาชื่อไฟล์ว่างไม่เจอ")
    }

    /// SHA-256 of a file, streamed in 1 MB chunks (PDFs can be large).
    static func sha256(of url: URL) throws -> String {
        let handle = try FileHandle(forReadingFrom: url)
        defer { try? handle.close() }
        var hasher = SHA256()
        while true {
            let chunk = try handle.read(upToCount: 1 << 20) ?? Data()
            if chunk.isEmpty { break }
            hasher.update(data: chunk)
        }
        return hasher.finalize().map { String(format: "%02x", $0) }.joined()
    }
}
