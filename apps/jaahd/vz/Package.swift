// swift-tools-version:5.9
// jaah-vz — the macOS MicroVM helper over Virtualization.framework (contract: Sources/jaah-vz/main.swift).
// JaahVZCore is the pure half (spec, handshake, port paths, output lines), unit-tested without a VM.
import PackageDescription

let package = Package(
    name: "jaah-vz",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "jaah-vz", targets: ["jaah-vz"])],
    targets: [
        .target(name: "JaahVZCore"),
        .executableTarget(name: "jaah-vz", dependencies: ["JaahVZCore"],
                          linkerSettings: [.linkedFramework("Virtualization")]),
        .testTarget(name: "JaahVZCoreTests", dependencies: ["JaahVZCore"]),
    ]
)
