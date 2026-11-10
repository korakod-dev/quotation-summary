// swift-tools-version:5.9
//
// Package.swift
// Desktop app for quotation-summary: a SwiftUI window with a PDF drop zone, a "run now" button and a
// per-file status dashboard. The Python engine (qsum) stays the single source of business logic: this app
// only calls scripts/status.sh (read-only JSON) and scripts/run.sh (start a run) and copies dropped PDFs
// into the source folder.
//
// SwiftUI + AppKit + CryptoKit only, no third-party dependencies.
// Build the .app bundle with ../scripts/build-app.sh (SwiftPM alone only builds the bare executable).
// Tests: `swift test --package-path gui` (swift-testing, which ships with the Command Line Tools; XCTest does not).
//
// Tools version 5.9 (not 6.x) on purpose: Swift 5 language mode keeps the
// strict-concurrency checks as warnings, which is enough for a small single-user app driven from the main actor.

import PackageDescription

let package = Package(
    name: "QuotationSummary",
    platforms: [
        // Info.plist says LSMinimumSystemVersion 13.0 (Window scene, Table).
        .macOS(.v13)
    ],
    products: [
        // The executable is called QuotationSummary (= CFBundleExecutable of the .app).
        .executable(name: "QuotationSummary", targets: ["QuotationSummaryApp"])
    ],
    targets: [
        .executableTarget(
            name: "QuotationSummaryApp",
            path: "Sources/QuotationSummaryApp"
        ),
        .testTarget(
            name: "QuotationSummaryAppTests",
            dependencies: ["QuotationSummaryApp"],
            path: "Tests/QuotationSummaryAppTests",
            // status-*.json: real output of `python -m qsum.status` (see ../tests/make_gui_fixtures.py)
            resources: [.copy("Fixtures")]
        ),
    ]
)
