//
// FileTableView.swift
// Per-file status table with a filter and the counts line.
//

import SwiftUI

struct FileTableView: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Text("สถานะไฟล์").font(.headline)
                Spacer()
                filterPicker
            }
            if model.visibleFiles.isEmpty {
                emptyState
            } else {
                Table(model.visibleFiles) {
                    TableColumn("ชื่อไฟล์") { row in
                        Text(row.name).lineLimit(1).truncationMode(.middle).help(row.name)
                    }
                    .width(min: 160, ideal: 260)
                    TableColumn("สถานะ") { row in
                        StatusBadge(status: row.status)
                    }
                    .width(min: 90, ideal: 100, max: 120)
                    TableColumn("วันที่เพิ่ม") { row in
                        Text(DateText.thaiDate(row.added)).foregroundColor(.secondary)
                    }
                    .width(min: 90, ideal: 100, max: 120)
                    TableColumn("รายละเอียด") { row in
                        Text(row.detail).lineLimit(2).help(row.detail).foregroundColor(.secondary)
                    }
                }
                .frame(minHeight: 200)
            }
            Text(Labels.countsLine(model.status.counts)).font(.caption).foregroundColor(.secondary)
        }
    }

    /// Segmented-style filter: ทั้งหมด / ทำแล้ว / ควรตรวจ / รอรอบถัดไป / อ่านไม่ได้ (each with its count).
    private var filterPicker: some View {
        Picker("กรอง", selection: filterBinding) {
            ForEach(AppViewModel.filterOptions, id: \.self) { option in
                Text(Labels.filterTitle(option.status, counts: model.status.counts)).tag(option)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
        .frame(maxWidth: 520)
    }

    private var filterBinding: Binding<AppViewModel.FilterOption> {
        Binding(get: { AppViewModel.FilterOption(status: model.filter) },
                set: { model.filter = $0.status })
    }

    private var emptyState: some View {
        VStack(spacing: 6) {
            Text(model.hasLoadedOnce ? "ไม่มีไฟล์ในรายการนี้" : "กำลังโหลด...").foregroundColor(.secondary)
            if model.hasLoadedOnce, model.filter == nil, model.status.ok {
                Text("ลากไฟล์ PDF มาวางด้านบน หรือวางไฟล์ในโฟลเดอร์ PDF").font(.caption).foregroundColor(.secondary)
            }
        }
        .frame(maxWidth: .infinity, minHeight: 120)
    }
}

/// Coloured pill with the Thai status word.
struct StatusBadge: View {
    let status: FileStatus

    var body: some View {
        Text(Labels.badge(status))
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(Capsule().fill(color.opacity(0.18)))
            .foregroundColor(color)
            .help(Labels.badgeHelp(status))
    }

    private var color: Color {
        switch status {
        case .done: return .green
        case .check: return .orange
        case .waiting: return .gray
        case .error: return .red
        }
    }
}

extension AppViewModel {
    /// Hashable wrapper so the Picker can use "all" (nil) as a choice.
    struct FilterOption: Hashable {
        let status: FileStatus?
        init(status: FileStatus?) { self.status = status }
    }

    static let filterOptions: [FilterOption] =
        [FilterOption(status: nil)] + [FileStatus.error, .waiting, .check, .done].map { FilterOption(status: $0) }
}
