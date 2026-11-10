//
// PollPolicy.swift
// How often the window asks the engine for its status (the only source of progress, contract C4).
//

import Foundation

enum PollPolicy {
    /// While a run is going (or the app just started one): fast, so the progress line follows the engine.
    static let runningInterval: TimeInterval = 2
    /// Idle: slow, status.sh hashes every PDF so it should not run constantly.
    static let idleInterval: TimeInterval = 30

    static func interval(running: Bool) -> TimeInterval {
        running ? runningInterval : idleInterval
    }
}
