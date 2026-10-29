// jaah-vz — boots one MicroVM on a macOS (Apple Silicon) node over Apple's Virtualization.framework.
//
// A Cloud Hypervisor-compatible subset, so jaah-microvmd drives it as it drives CH on Linux:
//
//   jaah-vz probe             exit 0, one line {"usable": bool, "error": null|"unsupported"|"entitlement"}
//   jaah-vz run --spec <vmdir>/spec.json
//
// THE SPEC is microvmd's (its module docstring) with `egress: "vsock"` and `tap`/`mac` null
// or absent:
// a Mac VM has no network device, its only way out is the vsock egress relay. `owner` on a share and
// `pane_log` are accepted and not used here (microvmd pumps the console into the pane log).
//
// THE VM: EFI boot with its own variable store `<vmdir>/efi-vars`; virtio-blk raw root (rw) + cidata seed
// (ro); one virtio-fs device per share tag; virtio entropy and balloon; one virtio-socket device.
//
// VSOCK, CH's hybrid semantics: a guest connection to a spec port is spliced to the unix socket
// `<vmdir>/vsock.sock_<port>`; a port whose socket is absent is reset at once, with one line in
// `<vmdir>/vmm.log` (never retried or queued). Host-initiated: connect to `<vmdir>/vsock.sock`, send
// `CONNECT <port>\n`, read `OK <port>\n`, then the stream is the guest port's.
//
// THE CONSOLE is a virtio console on a pty; its path is written to `<vmdir>/console.pty` before the ready line.
//
// OUTPUT: exactly one stdout line {"console": "<pty>", "ready": true} once the VM runs; any failure is one
// stderr line {"detail": "…", "error": "<code>"} and the exit code (JaahVZCore.ExitCode): 64 bad spec, 69
// Virtualization.framework unsupported, 70 validation/start failure (a missing entitlement included: error
// "entitlement"), 75 guest stopped with an error, 0 clean.
//
// STOP: the first SIGTERM asks the guest to power off (ACPI); a second stops the VM at once, as does the first
// when the guest cannot take the request yet (still starting). A failed stop exits 70 (error "stop").
//
// ENTITLEMENT: `com.apple.security.virtualization` only (`jaah-vz.entitlements`), never
// `com.apple.vm.networking` — the macOS signing check's `verify` asserts both.
import Darwin
import Foundation
import JaahVZCore
import Security
import Virtualization

func fail(_ code: ExitCode, _ error: String, _ detail: String) -> Never {
    FileHandle.standardError.write(Data(errorLine(error, detail).utf8))
    exit(code.rawValue)
}

func hasVirtualizationEntitlement() -> Bool {
    guard let task = SecTaskCreateFromSelf(nil),
          let value = SecTaskCopyValueForEntitlement(task, "com.apple.security.virtualization" as CFString, nil)
    else { return false }
    return (value as? Bool) == true
}

/// nil when this process can run a VM, else the probe's error code.
func probeError() -> String? {
    // The entitlement first: without it the framework's own answers cannot be trusted.
    if !hasVirtualizationEntitlement() { return "entitlement" }
    return VZVirtualMachine.isSupported ? nil : "unsupported"
}

// ── plumbing: vmm.log, unix sockets, the byte splice ──────────────────────────────────────────────────

final class VmmLog {
    let path: String
    init(vmdir: String) { path = vmdir + "/vmm.log" }

    func line(_ text: String) {
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_CLOEXEC, 0o600)
        guard fd >= 0 else { return }
        let stamped = "\(ISO8601DateFormatter().string(from: Date())) \(text)\n"
        _ = stamped.withCString { write(fd, $0, strlen($0)) }
        close(fd)
    }
}

func unixAddress(_ path: String) -> sockaddr_un? {
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < maxSocketPath else { return nil }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        raw.copyBytes(from: bytes)
        raw[bytes.count] = 0
    }
    return addr
}

/// A connection to the unix socket at `path`, only when it IS a socket this uid owns — never through a
/// symlink, so nothing planted in the VM directory can steer a guest stream to another host socket.
func connectUnix(_ path: String) -> Int32? {
    var st = stat()
    guard lstat(path, &st) == 0, (st.st_mode & S_IFMT) == S_IFSOCK, st.st_uid == getuid(),
          var addr = unixAddress(path) else { return nil }
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
        }
    }
    if !ok { close(fd); return nil }
    return fd
}

func listenUnix(_ path: String) -> Int32? {
    guard var addr = unixAddress(path) else { return nil }
    unlink(path)
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    let mask = umask(0o077)   // born 0600: no window before the chmod below
    let bound = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) == 0
        }
    }
    umask(mask)
    guard bound, chmod(path, 0o600) == 0, listen(fd, 16) == 0 else { close(fd); return nil }
    return fd
}

func writeAll(_ fd: Int32, _ bytes: [UInt8]) -> Bool {
    var offset = 0
    while offset < bytes.count {
        let n = bytes[offset...].withUnsafeBytes { write(fd, $0.baseAddress, $0.count) }
        if n < 0 && errno == EINTR { continue }
        if n <= 0 { return false }
        offset += n
    }
    return true
}

/// Copies bytes both ways between a guest connection and a host socket until both directions end; an end
/// in one direction half-closes the other side, so a request/response stream drains fully.
final class Splice {
    private let connection: VZVirtioSocketConnection   // retained: the guest stream closes with it
    private let host: Int32
    private let lock = NSLock()
    private var open = 2

    init(_ connection: VZVirtioSocketConnection, host: Int32) {
        self.connection = connection
        self.host = host
    }

    func start() {
        let guest = connection.fileDescriptor
        Thread { self.pump(from: guest, to: self.host) }.start()
        Thread { self.pump(from: self.host, to: guest) }.start()
    }

    private func pump(from src: Int32, to dst: Int32) {
        var buffer = [UInt8](repeating: 0, count: 65536)
        while true {
            let n = buffer.withUnsafeMutableBytes { read(src, $0.baseAddress, $0.count) }
            if n < 0 && errno == EINTR { continue }
            if n <= 0 || !writeAll(dst, Array(buffer[0..<n])) { break }
        }
        shutdown(dst, SHUT_WR)
        lock.lock()
        open -= 1
        let last = open == 0
        lock.unlock()
        if last {
            close(host)
            connection.close()
        }
    }
}

// ── the VM ────────────────────────────────────────────────────────────────────────────────────────────

func configuration(_ spec: Spec) throws -> VZVirtualMachineConfiguration {
    let config = VZVirtualMachineConfiguration()
    config.cpuCount = spec.vcpus
    config.memorySize = UInt64(spec.ramMiB) * 1024 * 1024
    config.platform = VZGenericPlatformConfiguration()

    let boot = VZEFIBootLoader()
    let store = URL(fileURLWithPath: spec.vmdir + "/efi-vars")
    if FileManager.default.fileExists(atPath: store.path) {
        boot.variableStore = VZEFIVariableStore(url: store)
    } else {
        boot.variableStore = try VZEFIVariableStore(creatingVariableStoreAt: store)
    }
    config.bootLoader = boot

    config.storageDevices = [
        VZVirtioBlockDeviceConfiguration(attachment: try VZDiskImageStorageDeviceAttachment(
            url: URL(fileURLWithPath: spec.diskPath), readOnly: false)),
        VZVirtioBlockDeviceConfiguration(attachment: try VZDiskImageStorageDeviceAttachment(
            url: URL(fileURLWithPath: spec.seedPath), readOnly: true)),
    ]
    config.directorySharingDevices = try spec.shares.map { share in
        try VZVirtioFileSystemDeviceConfiguration.validateTag(share.tag)
        let device = VZVirtioFileSystemDeviceConfiguration(tag: share.tag)
        device.share = VZSingleDirectoryShare(directory: VZSharedDirectory(
            url: URL(fileURLWithPath: share.hostPath), readOnly: share.readOnly))
        return device
    }
    config.socketDevices = [VZVirtioSocketDeviceConfiguration()]
    config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
    config.memoryBalloonDevices = [VZVirtioTraditionalMemoryBalloonDeviceConfiguration()]
    return config
}

/// A raw pty: the VM drives the master, microvmd opens the slave by the path this returns.
func consolePty() throws -> (master: Int32, path: String) {
    var master: Int32 = -1
    var slave: Int32 = -1
    guard openpty(&master, &slave, nil, nil, nil) == 0, let name = ttyname(slave) else {
        throw SpecError("openpty failed: \(String(cString: strerror(errno)))")
    }
    var attrs = termios()
    tcgetattr(slave, &attrs)
    cfmakeraw(&attrs)
    tcsetattr(slave, TCSANOW, &attrs)
    // The slave stays open for the VM's life, so the master never reads EIO while nobody is attached.
    return (master, String(cString: name))
}

final class Runner: NSObject, VZVirtualMachineDelegate, VZVirtioSocketListenerDelegate {
    let spec: Spec
    let vm: VZVirtualMachine
    let log: VmmLog
    let console: String
    private var terms = 0
    private var listeners: [VZVirtioSocketListener] = []

    init(spec: Spec, config: VZVirtualMachineConfiguration, console: String) {
        self.spec = spec
        self.vm = VZVirtualMachine(configuration: config)
        self.log = VmmLog(vmdir: spec.vmdir)
        self.console = console
        super.init()
        vm.delegate = self
    }

    func start() {
        vm.start { result in
            switch result {
            case .failure(let error):
                fail(.vmFailure, "start", error.localizedDescription)
            case .success:
                self.serveVsock()
                do {
                    try self.console.write(toFile: self.spec.vmdir + "/console.pty", atomically: true, encoding: .utf8)
                } catch {
                    fail(.vmFailure, "start", "cannot write console.pty: \(error.localizedDescription)")
                }
                FileHandle.standardOutput.write(Data(readyLine(console: self.console).utf8))
            }
        }
    }

    // First SIGTERM: ACPI power button. Second (or a guest that cannot take the request): stop now.
    func terminate() {
        terms += 1
        if terms == 1 && vm.canRequestStop {
            do { try vm.requestStop(); return } catch { log.line("requestStop failed: \(error)") }
        }
        vm.stop { error in
            if let error { fail(.vmFailure, "stop", error.localizedDescription) }
            exit(ExitCode.clean.rawValue)
        }
    }

    func guestDidStop(_ virtualMachine: VZVirtualMachine) {
        exit(ExitCode.clean.rawValue)
    }

    func virtualMachine(_ virtualMachine: VZVirtualMachine, didStopWithError error: Error) {
        fail(.guestError, "guest", error.localizedDescription)
    }

    private var device: VZVirtioSocketDevice? { vm.socketDevices.first as? VZVirtioSocketDevice }

    private func serveVsock() {
        guard let device else { fail(.vmFailure, "start", "the VM has no virtio-socket device") }
        for port in listenerPorts(spec) {
            let listener = VZVirtioSocketListener()
            listener.delegate = self
            device.setSocketListener(listener, forPort: port)
            listeners.append(listener)
        }
        let path = hostSocketPath(vmdir: spec.vmdir)
        guard let fd = listenUnix(path) else { fail(.vmFailure, "start", "cannot listen on \(path)") }
        Thread { self.acceptHost(fd) }.start()
    }

    // Guest -> host: splice to `vsock.sock_<port>`, or reset when nothing listens there.
    func listener(_ listener: VZVirtioSocketListener, shouldAcceptNewConnection connection: VZVirtioSocketConnection,
                  from socketDevice: VZVirtioSocketDevice) -> Bool {
        let port = connection.destinationPort
        guard let host = connectUnix(guestSocketPath(vmdir: spec.vmdir, port: port)) else {
            log.line("guest vsock port \(port): no host listener, connection reset")
            return false
        }
        Splice(connection, host: host).start()
        return true
    }

    // Host -> guest: `CONNECT <port>\n` on `vsock.sock`, answered `OK <port>\n`.
    private func acceptHost(_ fd: Int32) {
        while true {
            let client = accept(fd, nil, nil)
            if client < 0 {
                if errno == EINTR || errno == ECONNABORTED { continue }
                return
            }
            Thread { self.handshake(client) }.start()
        }
    }

    private func handshake(_ client: Int32) {
        // A client that never finishes its request line must not hold a thread.
        var timeout = timeval(tv_sec: 5, tv_usec: 0)
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        var line: [UInt8] = []
        var byte: UInt8 = 0
        while line.count <= maxConnectLine {
            let n = read(client, &byte, 1)
            if n < 0 && errno == EINTR { continue }
            if n <= 0 || byte == UInt8(ascii: "\n") { break }
            line.append(byte)
        }
        guard byte == UInt8(ascii: "\n"), let port = parseConnect(String(decoding: line, as: UTF8.self)) else {
            close(client)
            return
        }
        timeout = timeval(tv_sec: 0, tv_usec: 0)   // the spliced stream itself may idle for as long as it likes
        setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &timeout, socklen_t(MemoryLayout<timeval>.size))
        DispatchQueue.main.async {
            guard let device = self.device else { close(client); return }
            device.connect(toPort: port) { result in
                switch result {
                case .failure(let error):
                    self.log.line("host vsock connect to guest port \(port) failed: \(error.localizedDescription)")
                    close(client)
                case .success(let connection):
                    guard writeAll(client, Array(connectReply(port: port).utf8)) else {
                        close(client)
                        connection.close()
                        return
                    }
                    Splice(connection, host: client).start()
                }
            }
        }
    }
}

// ── main ──────────────────────────────────────────────────────────────────────────────────────────────

signal(SIGPIPE, SIG_IGN)
let args = Array(CommandLine.arguments.dropFirst())

switch args.first {
case "probe"? where args.count == 1:
    let error = probeError()
    FileHandle.standardOutput.write(Data(probeLine(usable: error == nil, error: error).utf8))
    exit(ExitCode.clean.rawValue)

case "run"? where args.count == 3 && args[1] == "--spec":
    let specPath = args[2]
    let vmdir = (specPath as NSString).deletingLastPathComponent
    let spec: Spec
    do {
        guard specPath.hasPrefix("/"), let data = FileManager.default.contents(atPath: specPath) else {
            throw SpecError("\(specPath) is not a readable absolute path")
        }
        spec = try parseSpec(data, vmdir: vmdir)
    } catch let error as SpecError {
        fail(.badSpec, "spec", error.detail)
    } catch {
        fail(.badSpec, "spec", "\(error)")
    }
    switch probeError() {
    case "entitlement"?: fail(.vmFailure, "entitlement", "jaah-vz lacks com.apple.security.virtualization")
    case "unsupported"?: fail(.unsupported, "unsupported", "this Mac cannot run Virtualization.framework VMs")
    default: break
    }
    let runner: Runner
    do {
        let config = try configuration(spec)
        let pty = try consolePty()
        let port = VZVirtioConsoleDeviceSerialPortConfiguration()
        let handle = FileHandle(fileDescriptor: pty.master, closeOnDealloc: false)
        port.attachment = VZFileHandleSerialPortAttachment(fileHandleForReading: handle, fileHandleForWriting: handle)
        config.serialPorts = [port]
        try config.validate()
        runner = Runner(spec: spec, config: config, console: pty.path)
    } catch {
        fail(.vmFailure, "validation", "\(error)")
    }
    signal(SIGTERM, SIG_IGN)
    let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
    sigterm.setEventHandler { runner.terminate() }
    sigterm.resume()
    runner.start()
    dispatchMain()

default:
    fail(.badSpec, "usage", "usage: jaah-vz probe | jaah-vz run --spec <vmdir>/spec.json")
}
