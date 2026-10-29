import Foundation
import JaahVZCore
import XCTest

final class HandshakeTests: XCTestCase {
    func testConnectLineNamesItsPort() {
        XCTAssertEqual(parseConnect("CONNECT 1025"), 1025)
        XCTAssertEqual(parseConnect("CONNECT 1025\r"), 1025)
        XCTAssertEqual(parseConnect("CONNECT 4294967295"), UInt32.max)
    }

    func testAnythingElseIsRefused() {
        for line in ["", "CONNECT", "CONNECT ", "CONNECT -1", "CONNECT 1025 x", "connect 1025", "CONNECT  1025",
                     "CONNECT 4294967296", "CONNECT 12345678901", "CONNECT +5", "OK 1025", "CONNECT ١٢"] {
            XCTAssertNil(parseConnect(line), line)
        }
    }

    func testTheReplyEchoesThePort() {
        XCTAssertEqual(connectReply(port: 1025), "OK 1025\n")
    }
}

final class PortPathTests: XCTestCase {
    func testGuestPortsMapToCloudHypervisorsHybridSocketNames() {
        XCTAssertEqual(guestSocketPath(vmdir: "/v/vm1", port: 1024), "/v/vm1/vsock.sock_1024")
        XCTAssertEqual(guestSocketPath(vmdir: "/v/vm1", port: 15053), "/v/vm1/vsock.sock_15053")
        XCTAssertEqual(hostSocketPath(vmdir: "/v/vm1"), "/v/vm1/vsock.sock")
    }

    func testTheAllowedPorts() {
        for port: UInt32 in [1024, 1025, 3000, 3999, 15001, 15053] { XCTAssertTrue(allowedPort(port), "\(port)") }
        for port: UInt32 in [0, 22, 1023, 1026, 2999, 4000, 15000, 15002, 15052] {
            XCTAssertFalse(allowedPort(port), "\(port)")
        }
    }
}

final class SpecTests: XCTestCase {
    var dir = ""

    override func setUpWithError() throws {
        dir = (NSTemporaryDirectory() as NSString).appendingPathComponent("vz-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(atPath: dir + "/share", withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: dir + "/root.raw", contents: Data())
        FileManager.default.createFile(atPath: dir + "/seed.raw", contents: Data())
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(atPath: dir) }

    func spec(_ overrides: [String: Any] = [:], drop: [String] = []) -> [String: Any] {
        var raw: [String: Any] = [
            "vm_id": "vm1", "vcpus": 2, "ram_mib": 2048, "disk_path": dir + "/root.raw",
            "seed_path": dir + "/seed.raw", "tap": NSNull(), "mac": NSNull(), "egress": "vsock",
            "shares": [["tag": "token", "host_path": dir + "/share", "ro": false, "owner": [501, 20]]],
            "vsock_ports": [1024, 1025, 3000, 15001, 15053], "pane_log": dir + "/pane.log", "generation": 3,
        ]
        for key in drop { raw.removeValue(forKey: key) }
        raw.merge(overrides) { _, new in new }
        return raw
    }

    func parse(_ raw: [String: Any]) throws -> Spec {
        try parseSpec(try JSONSerialization.data(withJSONObject: raw), vmdir: dir)
    }

    func testAValidSpecParses() throws {
        let parsed = try parse(spec())
        XCTAssertEqual(parsed.vcpus, 2)
        XCTAssertEqual(parsed.ramMiB, 2048)
        XCTAssertEqual(parsed.shares, [Share(tag: "token", hostPath: dir + "/share", readOnly: false)])
        XCTAssertEqual(parsed.vsockPorts, [1024, 1025, 3000, 15001, 15053])
        XCTAssertEqual(listenerPorts(parsed), [1024, 3000, 15001, 15053])   // 1025 is host -> guest
    }

    func testTapAndMacMustBeNull() {
        XCTAssertThrowsError(try parse(spec(["tap": "jvm0"])))
        XCTAssertThrowsError(try parse(spec(["mac": "02:00:00:00:00:01"])))
        XCTAssertNoThrow(try parse(spec(drop: ["tap", "mac"])))
    }

    func testEgressMustBeVsock() {
        XCTAssertThrowsError(try parse(spec(["egress": "tap"])))
        XCTAssertThrowsError(try parse(spec(drop: ["egress"])))
    }

    func testRefusals() {
        let bad: [[String: Any]] = [
            spec(["surprise": 1]), spec(["vcpus": 0]), spec(["vcpus": true]), spec(["ram_mib": 1.5]),
            spec(["disk_path": "relative.raw"]), spec(["disk_path": dir + "/absent.raw"]),
            spec(["seed_path": dir + "/../seed.raw"]), spec(["vsock_ports": [22]]), spec(["vsock_ports": "1024"]),
            spec(["shares": [["tag": "token", "host_path": dir + "/root.raw"]]]),
            spec(["shares": [["tag": "a", "host_path": dir + "/share"], ["tag": "a", "host_path": dir + "/share"]]]),
            spec(["shares": [["tag": "a", "host_path": dir + "/share", "ro": "yes"]]]),
            spec(["pane_log": dir + "/nowhere/pane.log"]),
        ]
        for raw in bad { XCTAssertThrowsError(try parse(raw), "\(raw)") }
        XCTAssertThrowsError(try parseSpec(Data("[1]".utf8), vmdir: dir))
        XCTAssertThrowsError(try parseSpec(Data("{".utf8), vmdir: dir))
    }

    func testAVmdirTooLongForASocketPathIsRefused() {
        let long = "/" + String(repeating: "d", count: 100)
        XCTAssertThrowsError(try parseSpec(try JSONSerialization.data(withJSONObject: spec()), vmdir: long))
    }
}

final class OutputTests: XCTestCase {
    func object(_ line: String) throws -> [String: Any] {
        XCTAssertTrue(line.hasSuffix("\n"))
        XCTAssertEqual(line.filter { $0 == "\n" }.count, 1)
        return try XCTUnwrap(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
    }

    func testReadyLine() throws {
        let ready = try object(readyLine(console: "/dev/ttys004"))
        XCTAssertEqual(ready["ready"] as? Bool, true)
        XCTAssertEqual(ready["console"] as? String, "/dev/ttys004")
    }

    func testProbeLine() throws {
        let usable = try object(probeLine(usable: true, error: nil))
        XCTAssertEqual(usable["usable"] as? Bool, true)
        XCTAssertTrue(usable["error"] is NSNull)
        XCTAssertEqual(try object(probeLine(usable: false, error: "unsupported"))["error"] as? String, "unsupported")
    }

    func testErrorLine() throws {
        let error = try object(errorLine("spec", "tap must be null\non macOS"))
        XCTAssertEqual(error["error"] as? String, "spec")
        XCTAssertEqual(error["detail"] as? String, "tap must be null\non macOS")
    }
}
