//
// ScreenshotTests.swift
// Renders the main window to PNG files for the README, from invented fixture data (Fixtures/shot-*.json).
//
// Off-screen on purpose: the view is hosted in an NSWindow that is never shown, and drawn with
// `cacheDisplay(in:to:)`. This needs no Screen Recording permission (nothing captures the real screen).
// (ImageRenderer cannot draw AppKit-backed controls such as Table and the segmented Picker, so the hosted
// window route is used instead.)
//
// The test only writes files when QS_SCREENSHOT_DIR is set, so a plain `swift test` leaves nothing behind:
//   QS_SCREENSHOT_DIR=docs/screenshots swift test --package-path gui --filter ScreenshotTests
//

import AppKit
import Foundation
import SwiftUI
import Testing
@testable import QuotationSummaryApp

@MainActor
@Suite("Screenshots (README images, off-screen)")
struct ScreenshotTests {
    /// Build a model whose fake engine prints `fixture` as the status; run.sh exits with `runExit`.
    private func makeModel(fixture: String, runExit: Int32 = 0) throws -> (AppViewModel, URL) {
        let engine = try makeFakeEngine()
        let json = try fixtureData(fixture)
        let runner = FakeRunner { cmd in
            cmd.arguments.first?.hasSuffix("status.sh") == true
                ? ProcessResult(exitCode: 0, stdout: json)
                : ProcessResult(exitCode: runExit)
        }
        let service = EngineService(locator: EngineLocator(directory: engine), runner: runner,
                                    logDirectory: engine.appendingPathComponent("logs"))
        return (AppViewModel(service: service, opener: FakeOpener()), engine)
    }

    /// Draw the window content into a PNG (2x for a sharp image on the README).
    private func render(_ model: AppViewModel, to url: URL) throws {
        _ = NSApplication.shared
        let size = NSSize(width: 900, height: 700)
        let hosting = NSHostingView(rootView: ContentView(model: model).frame(width: size.width, height: size.height))
        hosting.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: hosting.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .aqua)            // light mode, whatever the Mac uses
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        // Let SwiftUI / AppKit finish one layout + display pass (Table fills its rows lazily).
        RunLoop.current.run(until: Date().addingTimeInterval(0.6))
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()

        let pixelsWide = Int(size.width * 2), pixelsHigh = Int(size.height * 2)
        guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: pixelsWide, pixelsHigh: pixelsHigh,
                                         bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                         colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else {
            throw TestError.missingFixture("bitmap")
        }
        rep.size = size                                           // 2x pixels for the same point size
        hosting.cacheDisplay(in: hosting.bounds, to: rep)
        guard let png = rep.representation(using: .png, properties: [:]) else { throw TestError.missingFixture("png") }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try png.write(to: url)
    }

    private var outDir: URL? {
        guard let dir = ProcessInfo.processInfo.environment["QS_SCREENSHOT_DIR"], !dir.isEmpty else { return nil }
        return URL(fileURLWithPath: dir, isDirectory: true)
    }

    /// The shot-*.json fixtures are hand-written (tests/make_gui_fixtures.py only generates status-*.json and
    /// tests/test_contract.py does not look at shot-*), so decode every one of them on each plain `swift test`,
    /// without QS_SCREENSHOT_DIR. A drifted schema fails here instead of silently on the next screenshot run.
    @Test func shotFixturesDecode() throws {
        let dir = try #require(Bundle.module.resourceURL?.appendingPathComponent("Fixtures", isDirectory: true))
        let names = try FileManager.default.contentsOfDirectory(atPath: dir.path)
            .filter { $0.hasPrefix("shot-") && $0.hasSuffix(".json") }.sorted()
        #expect(names == ["shot-error.json", "shot-idle.json", "shot-running.json"])   // new shot file: list it here
        for name in names {
            let status = try loadStatus(name)
            #expect(status.schema == 1, "\(name)")
            #expect(status.ok, "\(name)")
        }
    }

    @Test func idleWindow() async throws {
        guard let dir = outDir else { return }
        let (model, engine) = try makeModel(fixture: "shot-idle.json")
        defer { try? FileManager.default.removeItem(at: engine) }
        await model.refresh()
        #expect(model.isRunning == false)
        try render(model, to: dir.appendingPathComponent("main-idle.png"))
    }

    @Test func runningWindow() async throws {
        guard let dir = outDir else { return }
        let (model, engine) = try makeModel(fixture: "shot-running.json")
        defer { try? FileManager.default.removeItem(at: engine) }
        await model.refresh()
        #expect(model.isRunning)
        try render(model, to: dir.appendingPathComponent("main-running.png"))
    }

    @Test func errorBannerWindow() async throws {
        guard let dir = outDir else { return }
        // The run started from the app ends with exit code 1: a red banner above the (still visible) table.
        let (model, engine) = try makeModel(fixture: "shot-error.json", runExit: 1)
        defer { try? FileManager.default.removeItem(at: engine) }
        await model.refresh()
        await model.startRun()
        #expect(model.activeBanner?.kind == .error)
        try render(model, to: dir.appendingPathComponent("main-error.png"))
    }
}
