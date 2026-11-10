//
// FileImporterTests.swift
// Copy rules: new file, same file again, same name with different content, not a PDF, errors. Never overwrites.
//

import Foundation
import Testing
@testable import QuotationSummaryApp

@Suite("File importer")
struct FileImporterTests {
    /// Makes a destination folder and a separate "Downloads" folder with the given source files.
    func setup(_ files: [String: String]) throws -> (dest: URL, sources: [String: URL], cleanup: () -> Void) {
        let root = try makeTempDir("qsum-import")
        let dest = root.appendingPathComponent("dest", isDirectory: true)
        let srcDir = root.appendingPathComponent("downloads", isDirectory: true)
        try FileManager.default.createDirectory(at: dest, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: srcDir, withIntermediateDirectories: true)
        var sources: [String: URL] = [:]
        for (name, content) in files {
            let url = srcDir.appendingPathComponent(name)
            try write(content, to: url)
            sources[name] = url
        }
        return (dest, sources, { try? FileManager.default.removeItem(at: root) })
    }

    func names(_ dir: URL) throws -> [String] {
        try FileManager.default.contentsOfDirectory(atPath: dir.path).sorted()
    }

    @Test func copiesANewPDF() throws {
        let t = try setup(["ใบเสนอราคา 001.pdf": "AAA"])
        defer { t.cleanup() }
        let r = FileImporter.importPDFs([t.sources["ใบเสนอราคา 001.pdf"]!], into: t.dest)
        #expect(r == [.copied("ใบเสนอราคา 001.pdf")])
        #expect(try read(t.dest.appendingPathComponent("ใบเสนอราคา 001.pdf")) == "AAA")
        #expect(FileManager.default.fileExists(atPath: t.sources["ใบเสนอราคา 001.pdf"]!.path))      // a copy, not a move
    }

    @Test func sameNameSameContentIsAlreadyThere() throws {
        let t = try setup(["a.pdf": "AAA"])
        defer { t.cleanup() }
        _ = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        #expect(r == [.alreadyThere("a.pdf")])
        #expect(try names(t.dest) == ["a.pdf"])
    }

    @Test func sameNameDifferentContentIsRenamedAndNeverOverwrites() throws {
        let t = try setup(["a.pdf": "NEW"])
        defer { t.cleanup() }
        try write("OLD", to: t.dest.appendingPathComponent("a.pdf"))
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        #expect(r == [.renamed("a (2).pdf")])
        #expect(try read(t.dest.appendingPathComponent("a.pdf")) == "OLD")                  // untouched
        #expect(try read(t.dest.appendingPathComponent("a (2).pdf")) == "NEW")
    }

    @Test func importingTheRenamedFileAgainDoesNotMakeACopyNumber3() throws {
        let t = try setup(["a.pdf": "NEW"])
        defer { t.cleanup() }
        try write("OLD", to: t.dest.appendingPathComponent("a.pdf"))
        _ = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        let again = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        #expect(again == [.alreadyThere("a (2).pdf")])
        #expect(try names(t.dest) == ["a (2).pdf", "a.pdf"])
    }

    @Test func thirdDifferentVersionGetsNumber3() throws {
        let t = try setup(["a.pdf": "THREE"])
        defer { t.cleanup() }
        try write("ONE", to: t.dest.appendingPathComponent("a.pdf"))
        try write("TWO", to: t.dest.appendingPathComponent("a (2).pdf"))
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest)
        #expect(r == [.renamed("a (3).pdf")])
        #expect(try read(t.dest.appendingPathComponent("a (3).pdf")) == "THREE")
    }

    @Test func nonPDFIsRejected() throws {
        let t = try setup(["notes.txt": "x", "sheet.xlsx": "y", "noext": "z"])
        defer { t.cleanup() }
        let urls = ["notes.txt", "sheet.xlsx", "noext"].map { t.sources[$0]! }
        let r = FileImporter.importPDFs(urls, into: t.dest)
        #expect(r == [.notPDF("notes.txt"), .notPDF("sheet.xlsx"), .notPDF("noext")])
        #expect(try names(t.dest).isEmpty)
    }

    @Test func extensionIsCaseInsensitiveAndKept() throws {
        let t = try setup(["SCAN.PDF": "P"])
        defer { t.cleanup() }
        #expect(FileImporter.importPDFs([t.sources["SCAN.PDF"]!], into: t.dest) == [.copied("SCAN.PDF")])
        try write("Q", to: t.sources["SCAN.PDF"]!)                  // same name, new content
        #expect(FileImporter.importPDFs([t.sources["SCAN.PDF"]!], into: t.dest) == [.renamed("SCAN (2).PDF")])
    }

    @Test func aFolderNamedLikeAPDFIsNotCopied() throws {
        let t = try setup([:])
        defer { t.cleanup() }
        let folder = t.dest.deletingLastPathComponent().appendingPathComponent("tricky.pdf", isDirectory: true)
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        #expect(FileImporter.importPDFs([folder], into: t.dest) == [.notPDF("tricky.pdf")])
        #expect(try names(t.dest).isEmpty)
    }

    @Test func missingSourceFileIsNotPDFNotACrash() throws {
        let t = try setup([:])
        defer { t.cleanup() }
        let ghost = t.dest.deletingLastPathComponent().appendingPathComponent("ghost.pdf")
        #expect(FileImporter.importPDFs([ghost], into: t.dest) == [.notPDF("ghost.pdf")])
    }

    @Test func missingDestinationFolderFailsAndCreatesNothing() throws {
        let t = try setup(["a.pdf": "A"])
        defer { t.cleanup() }
        let nowhere = t.dest.appendingPathComponent("no/such/folder", isDirectory: true)
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: nowhere)
        guard case .failed(let name, let message) = r[0] else {
            Issue.record("expected .failed, got \(r[0])")
            return
        }
        #expect(name == "a.pdf")
        #expect(message.contains("ไม่พบโฟลเดอร์ปลายทาง"))
        #expect(!FileManager.default.fileExists(atPath: nowhere.path))
    }

    @Test func destinationThatIsAFileFails() throws {
        let t = try setup(["a.pdf": "A"])
        defer { t.cleanup() }
        let asFile = t.dest.appendingPathComponent("iam.afile")
        try write("x", to: asFile)
        guard case .failed = FileImporter.importPDFs([t.sources["a.pdf"]!], into: asFile)[0] else {
            Issue.record("expected .failed")
            return
        }
    }

    @Test func mixedBatchKeepsOrder() throws {
        let t = try setup(["a.pdf": "A", "b.pdf": "B", "c.txt": "C"])
        defer { t.cleanup() }
        try write("OTHER", to: t.dest.appendingPathComponent("b.pdf"))
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!, t.sources["b.pdf"]!, t.sources["c.txt"]!, t.sources["a.pdf"]!], into: t.dest)
        #expect(r == [.copied("a.pdf"), .renamed("b (2).pdf"), .notPDF("c.txt"), .alreadyThere("a.pdf")])
    }

    @Test func importingFromTheDestinationFolderItselfIsAlreadyThere() throws {
        let t = try setup([:])
        defer { t.cleanup() }
        let inside = t.dest.appendingPathComponent("x.pdf")
        try write("X", to: inside)
        #expect(FileImporter.importPDFs([inside], into: t.dest) == [.alreadyThere("x.pdf")])
    }

    @Test func hiddenAndOfficeLockNamesAreRejectedNotCopied() throws {
        // the engine always skips these, so "added 1 file" would be a lie.
        let t = try setup([".hidden.pdf": "H", "~$quote.pdf": "L", "ok.pdf": "OK"])
        defer { t.cleanup() }
        let r = FileImporter.importPDFs([t.sources[".hidden.pdf"]!, t.sources["~$quote.pdf"]!, t.sources["ok.pdf"]!],
                                        into: t.dest)
        guard case .failed(let n1, let m1) = r[0], case .failed(let n2, _) = r[1] else {
            Issue.record("expected two .failed, got \(r)")
            return
        }
        #expect(n1 == ".hidden.pdf" && n2 == "~$quote.pdf")
        #expect(m1.contains("ขึ้นต้นด้วย"))
        #expect(r[2] == .copied("ok.pdf"))
        #expect(try names(t.dest) == ["ok.pdf"])
        #expect(Labels.importBanner(Array(r[0...1])).kind == .error)
    }

    @Test func copyGoesThroughAHiddenTempNameAndLeavesNoLeftovers() throws {
        // while copying, the destination must only ever hold hidden names (the engine's list_pdfs skips ".x").
        let t = try setup(["a.pdf": "AAA"])
        defer { t.cleanup() }
        final class SpyFM: FileManager, @unchecked Sendable {
            var copyTargets: [String] = []
            override func copyItem(at srcURL: URL, to dstURL: URL) throws {
                copyTargets.append(dstURL.lastPathComponent)
                try super.copyItem(at: srcURL, to: dstURL)
            }
        }
        let fm = SpyFM()
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest, fileManager: fm)
        #expect(r == [.copied("a.pdf")])
        #expect(fm.copyTargets.count == 1)
        #expect(fm.copyTargets[0].hasPrefix(FileImporter.tempPrefix))
        #expect(try names(t.dest) == ["a.pdf"])                      // renamed into place, no temp file left
    }

    @Test func failedCopyLeavesNoTempFileBehind() throws {
        let t = try setup(["a.pdf": "AAA"])
        defer { t.cleanup() }
        final class FailingFM: FileManager, @unchecked Sendable {
            override func moveItem(at srcURL: URL, to dstURL: URL) throws {
                throw CocoaError(.fileWriteOutOfSpace)
            }
        }
        let r = FileImporter.importPDFs([t.sources["a.pdf"]!], into: t.dest, fileManager: FailingFM())
        guard case .failed = r[0] else {
            Issue.record("expected .failed, got \(r[0])")
            return
        }
        #expect(try names(t.dest).isEmpty)
    }

    @Test func sha256IsStreamedAndCorrect() throws {
        let t = try setup(["a.pdf": "abc"])
        defer { t.cleanup() }
        // SHA-256("abc") from FIPS 180-2
        #expect(try FileImporter.sha256(of: t.sources["a.pdf"]!)
                == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        let big = t.dest.appendingPathComponent("big.bin")
        try Data(repeating: 0x61, count: (1 << 20) * 2 + 17).write(to: big)       // crosses the chunk size twice
        #expect(try FileImporter.sha256(of: big).count == 64)
    }
}
