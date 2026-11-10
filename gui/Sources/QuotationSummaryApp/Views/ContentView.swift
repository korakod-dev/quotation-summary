//
// ContentView.swift
// The one window: header, banner, drop zone, action buttons, file table.
//

import AppKit
import SwiftUI

struct ContentView: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HeaderView(model: model)
            if let banner = model.activeBanner {
                BannerView(banner: banner, dismissable: model.activeBannerIsDismissable) { model.dismissBanner() }
            }
            DropZoneView(model: model)
            ActionBar(model: model)
            FileTableView(model: model)
        }
        .padding(16)
        .frame(minWidth: 820, minHeight: 640)
        // Refresh when the window comes back to the front (the user may have changed files in Finder meanwhile).
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.requestRefresh()
        }
        .onAppear { model.startPolling() }
    }
}

extension AppViewModel {
    /// Status errors stay while the cause is there; only action messages can be closed with the x.
    var activeBannerIsDismissable: Bool {
        !(hasLoadedOnce && Labels.banner(for: status) != nil)
    }
}

// MARK: - header

struct HeaderView: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("สรุปใบเสนอราคา").font(.title2.weight(.semibold))
                Spacer()
                if model.isRunning { ProgressView().controlSize(.small) }
            }
            if model.isRunning {
                Text(runningText)
                    .font(.headline)
                    .foregroundColor(.accentColor)
                if let progress = model.status.progress, progress.total > 0 {
                    ProgressView(value: Double(min(progress.done, progress.total)), total: Double(progress.total))
                }
            }
            Text(Labels.lastRunLine(model.status.lastRun)).font(.callout).foregroundColor(.secondary)
            Text(Labels.nextRunLine(model.status.schedule)).font(.callout).foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var runningText: String {
        // The run may have been started by the app a moment ago and the engine has not written `current` yet.
        model.status.running ? Labels.runningLine(model.status.progress) : "กำลังเริ่มทำงาน..."
    }
}

// MARK: - banner

struct BannerView: View {
    let banner: Labels.Banner
    let dismissable: Bool
    let onDismiss: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: icon).foregroundColor(color)
            Text(banner.text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            if dismissable {
                Button(action: onDismiss) { Image(systemName: "xmark") }
                    .buttonStyle(.plain)
                    .help("ปิดข้อความ")
            }
        }
        .padding(10)
        .background(RoundedRectangle(cornerRadius: 8).fill(color.opacity(0.14)))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(color.opacity(0.5)))
    }

    private var color: Color {
        switch banner.kind {
        case .info: return .blue
        case .success: return .green
        case .warning: return .orange
        case .error: return .red
        }
    }

    private var icon: String {
        switch banner.kind {
        case .info: return "info.circle.fill"
        case .success: return "checkmark.circle.fill"
        case .warning: return "exclamationmark.triangle.fill"
        case .error: return "xmark.octagon.fill"
        }
    }
}

// MARK: - actions

struct ActionBar: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        HStack(spacing: 10) {
            Button {
                Task { await model.startRun() }
            } label: {
                Label("เริ่มสรุปตอนนี้", systemImage: "play.fill")
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            .disabled(!model.canRun)
            .keyboardShortcut(.defaultAction)

            Button {
                model.openExcel()
            } label: {
                Label("เปิด Excel", systemImage: "tablecells")
            }
            .disabled(!model.canOpenExcel)

            Button {
                model.openSourceFolder()
            } label: {
                Label("เปิดโฟลเดอร์ PDF", systemImage: "folder")
            }
            .disabled(!model.canOpenFolder)
            Spacer()
        }
    }
}
