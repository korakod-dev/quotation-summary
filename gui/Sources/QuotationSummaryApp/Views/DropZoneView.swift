//
// DropZoneView.swift
// Drag PDFs onto the window, or choose them with the file panel. The copying itself is FileImporter.
//

import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct DropZoneView: View {
    @ObservedObject var model: AppViewModel

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "arrow.down.doc")
                .font(.system(size: 28))
                .foregroundColor(model.isDropTargeted ? .accentColor : .secondary)
            Text("ลากไฟล์ใบเสนอราคา PDF มาวางที่นี่").font(.headline)
            Button("เลือกไฟล์...") { chooseFiles() }
                .disabled(!model.canImport)
        }
        .frame(maxWidth: .infinity, minHeight: 110)
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(model.isDropTargeted ? Color.accentColor.opacity(0.12) : Color.secondary.opacity(0.06)))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .strokeBorder(model.isDropTargeted ? Color.accentColor : Color.secondary.opacity(0.5),
                          style: StrokeStyle(lineWidth: 2, dash: [7, 5])))
        .onDrop(of: [UTType.fileURL], isTargeted: dropTargetBinding) { providers in
            guard model.canImport else { return false }
            DropZoneView.loadURLs(from: providers) { urls in
                Task { await model.importFiles(urls) }
            }
            return true
        }
    }

    private var dropTargetBinding: Binding<Bool> {
        Binding(get: { model.isDropTargeted }, set: { model.isDropTargeted = $0 })
    }

    /// NSOpenPanel limited to PDF files, several at once.
    private func chooseFiles() {
        let panel = NSOpenPanel()
        panel.title = "เลือกไฟล์ใบเสนอราคา PDF"
        panel.prompt = "เลือก"
        panel.allowedContentTypes = [.pdf]
        panel.allowsMultipleSelection = true
        panel.canChooseDirectories = false
        panel.canChooseFiles = true
        guard panel.runModal() == .OK else { return }
        let urls = panel.urls
        Task { await model.importFiles(urls) }
    }

    /// Resolve the dropped item providers into file URLs (keeps the drop order).
    static func loadURLs(from providers: [NSItemProvider], completion: @escaping ([URL]) -> Void) {
        let lock = NSLock()
        var found: [Int: URL] = [:]
        let group = DispatchGroup()
        for (index, provider) in providers.enumerated() where provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            group.enter()
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                var url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let direct = item as? URL {
                    url = direct
                }
                if let url = url {
                    lock.lock()
                    found[index] = url
                    lock.unlock()
                }
                group.leave()
            }
        }
        group.notify(queue: .main) {
            lock.lock()
            let ordered = found.keys.sorted().compactMap { found[$0] }
            lock.unlock()
            completion(ordered)
        }
    }
}
