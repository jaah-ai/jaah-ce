// The pure half of jaah-vz: the spec it accepts, the host-initiated vsock handshake, the vsock socket paths
// and the one-line JSON it prints. No Virtualization.framework here, so `swift test` runs on any Mac.
import Foundation

/// The process's exit status, the contract microvmd reads (sysexits.h values).
public enum ExitCode: Int32 {
    case clean = 0          // the VM stopped cleanly (guest power-off or a stop request)
    case badSpec = 64       // EX_USAGE: spec.json missing, malformed, or outside the contract
    case unsupported = 69   // EX_UNAVAILABLE: this Mac cannot run Virtualization.framework
    case vmFailure = 70     // EX_SOFTWARE: configuration validation or start failed
    case guestError = 75    // EX_TEMPFAIL: the guest stopped with an error
}

public struct SpecError: Error, Equatable {
    public let detail: String
    public init(_ detail: String) { self.detail = detail }
}

public struct Share: Equatable {
    public let tag: String
    public let hostPath: String
    public let readOnly: Bool

    public init(tag: String, hostPath: String, readOnly: Bool) {
        self.tag = tag
        self.hostPath = hostPath
        self.readOnly = readOnly
    }
}

public struct Spec: Equatable {
    public let vmdir: String
    public let vcpus: Int
    public let ramMiB: Int
    public let diskPath: String
    public let seedPath: String
    public let shares: [Share]
    public let vsockPorts: [UInt32]
    public let paneLog: String
}

/// The guest -> host vsock ports a spec may name: control (1024), the winsize port (1025, host -> guest
/// only, so it gets no listener), forwarded ports (3000-3999), and the egress relay's HTTP (15001) and
/// DNS (15053) ports.
public let controlPort: UInt32 = 1024
public let winsizePort: UInt32 = 1025
public let egressPorts: [UInt32] = [15001, 15053]

public func allowedPort(_ port: UInt32) -> Bool {
    port == controlPort || port == winsizePort || (3000...3999).contains(port) || egressPorts.contains(port)
}

/// sockaddr_un.sun_path on Darwin, its terminating NUL included.
public let maxSocketPath = 104

/// Cloud Hypervisor's hybrid-vsock layout: a guest connection to `port` is spliced to
/// `<vmdir>/vsock.sock_<port>`; host-initiated connections arrive on `<vmdir>/vsock.sock`.
public func hostSocketPath(vmdir: String) -> String { vmdir + "/vsock.sock" }

public func guestSocketPath(vmdir: String, port: UInt32) -> String { "\(vmdir)/vsock.sock_\(port)" }

/// The ports that get a guest-connection listener: every spec port except the host -> guest winsize port.
public func listenerPorts(_ spec: Spec) -> [UInt32] { spec.vsockPorts.filter { $0 != winsizePort } }

private let specKeys: Set<String> = ["vm_id", "vcpus", "ram_mib", "disk_path", "seed_path", "tap", "mac",
                                     "shares", "vsock_ports", "pane_log", "generation", "egress"]

private func isBool(_ value: Any) -> Bool {
    guard let number = value as? NSNumber else { return false }
    return CFGetTypeID(number) == CFBooleanGetTypeID()
}

private func positiveInt(_ raw: [String: Any], _ key: String) throws -> Int {
    guard let value = raw[key], !isBool(value), let number = value as? NSNumber,
          number.doubleValue == Double(number.intValue), number.intValue > 0 else {
        throw SpecError("\(key) must be a positive integer")
    }
    return number.intValue
}

private func absolutePath(_ raw: [String: Any], _ key: String, mustExist: Bool = true) throws -> String {
    // Canonical by its spelling: no empty, `.` or `..` component (NSString.standardizingPath would also
    // fold `/private`, which is how macOS spells `/var` and `/tmp`).
    guard let value = raw[key] as? String, value.hasPrefix("/"), value.count > 1, !value.contains("\0"),
          !value.split(separator: "/", omittingEmptySubsequences: false).dropFirst()
              .contains(where: { $0.isEmpty || $0 == "." || $0 == ".." }) else {
        throw SpecError("\(key) must be an absolute, canonical path")
    }
    if mustExist && !FileManager.default.fileExists(atPath: value) {
        throw SpecError("\(key) \(value) does not exist")
    }
    return value
}

private func isDirectory(_ path: String) -> Bool {
    var dir: ObjCBool = false
    return FileManager.default.fileExists(atPath: path, isDirectory: &dir) && dir.boolValue
}

/// Validate one spec.json. `vmdir` is the directory holding it. Every refusal names what was wrong.
public func parseSpec(_ data: Data, vmdir: String) throws -> Spec {
    guard let object = try? JSONSerialization.jsonObject(with: data), let raw = object as? [String: Any] else {
        throw SpecError("spec.json is not a JSON object")
    }
    let unknown = Set(raw.keys).subtracting(specKeys)
    if !unknown.isEmpty {
        throw SpecError("spec.json carries unknown keys: \(unknown.sorted())")
    }
    // A Mac VM has no tap: its only network is the vsock egress relay.
    for key in ["tap", "mac"] where raw[key] != nil && !(raw[key] is NSNull) {
        throw SpecError("\(key) must be null on macOS (egress is vsock)")
    }
    guard raw["egress"] as? String == "vsock" else {
        throw SpecError("egress must be \"vsock\"")
    }
    guard let rawShares = raw["shares"] as? [Any] else { throw SpecError("shares must be a list") }
    var shares: [Share] = []
    for item in rawShares {
        guard let share = item as? [String: Any],
              Set(share.keys).isSubset(of: ["tag", "host_path", "ro", "owner"]),
              let tag = share["tag"] as? String, !tag.isEmpty, tag.utf8.count <= 36 else {
            throw SpecError("a share is not {tag, host_path, ro[, owner]} with a tag of 1-36 bytes")
        }
        if shares.contains(where: { $0.tag == tag }) { throw SpecError("share tag \(tag) appears twice") }
        let readOnly: Bool
        switch share["ro"] {
        case nil: readOnly = false
        case let value? where isBool(value): readOnly = (value as! NSNumber).boolValue
        default: throw SpecError("share \(tag) ro must be a boolean")
        }
        let path = try absolutePath(share, "host_path")
        if !isDirectory(path) { throw SpecError("share \(tag) host_path \(path) is not a directory") }
        // `owner` (the token share's uid mapping on Linux) has no meaning to Virtualization.framework.
        shares.append(Share(tag: tag, hostPath: path, readOnly: readOnly))
    }
    let portsValue: Any = raw["vsock_ports"] ?? [Any]()
    guard let rawPorts = portsValue as? [Any] else { throw SpecError("vsock_ports must be a list") }
    var ports: [UInt32] = []
    for value in rawPorts {
        guard !isBool(value), let number = value as? NSNumber, number.doubleValue == Double(number.intValue),
              number.intValue > 0, number.intValue <= Int(UInt32.max), allowedPort(UInt32(number.intValue)) else {
            throw SpecError("vsock port \(value) is not 1024, 1025, 3000-3999, 15001 or 15053")
        }
        let port = UInt32(number.intValue)
        if !ports.contains(port) { ports.append(port) }
    }
    let paneLog = try absolutePath(raw, "pane_log", mustExist: false)
    if !isDirectory((paneLog as NSString).deletingLastPathComponent) {
        throw SpecError("pane_log's directory does not exist")
    }
    let longest = ports.map { guestSocketPath(vmdir: vmdir, port: $0) } + [hostSocketPath(vmdir: vmdir)]
    if let path = longest.first(where: { $0.utf8.count >= maxSocketPath }) {
        throw SpecError("\(path) is longer than a unix socket path may be (\(maxSocketPath - 1) bytes)")
    }
    return Spec(vmdir: vmdir, vcpus: try positiveInt(raw, "vcpus"), ramMiB: try positiveInt(raw, "ram_mib"),
                diskPath: try absolutePath(raw, "disk_path"), seedPath: try absolutePath(raw, "seed_path"),
                shares: shares, vsockPorts: ports, paneLog: paneLog)
}

/// The port of a host-initiated `CONNECT <port>\n` request line (the newline already stripped, a trailing
/// `\r` tolerated), or nil when the line is anything else.
public func parseConnect(_ line: String) -> UInt32? {
    var text = Substring(line)
    if text.hasSuffix("\r") { text = text.dropLast() }
    guard text.hasPrefix("CONNECT ") else { return nil }
    let digits = text.dropFirst("CONNECT ".count)
    guard !digits.isEmpty, digits.count <= 10, digits.allSatisfy({ $0.isASCII && $0.isNumber }),
          let port = UInt32(digits) else { return nil }
    return port
}

public func connectReply(port: UInt32) -> String { "OK \(port)\n" }

/// The longest request line a host-initiated client may send before it is dropped.
public let maxConnectLine = 32

// ── output: exactly one JSON line per outcome ─────────────────────────────────────────────────────────

private func jsonLine(_ object: [String: Any]) -> String {
    let data = try! JSONSerialization.data(withJSONObject: object, options: [.sortedKeys, .withoutEscapingSlashes])
    return String(decoding: data, as: UTF8.self) + "\n"
}

public func readyLine(console: String) -> String { jsonLine(["ready": true, "console": console]) }

public func errorLine(_ code: String, _ detail: String) -> String { jsonLine(["error": code, "detail": detail]) }

/// `probe`'s answer. `error` is nil (usable), "unsupported" or "entitlement".
public func probeLine(usable: Bool, error: String?) -> String {
    let code: Any
    if let error { code = error } else { code = NSNull() }
    return jsonLine(["usable": usable, "error": code])
}
