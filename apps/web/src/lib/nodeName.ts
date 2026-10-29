// Node names are often FQDN-ish (e.g. "mac-studio.local", "host.lan.example.com"). Table "Node"
// cells only need to disambiguate the host, so display just the first dot-separated segment to save
// horizontal space. The full node_id stays available via a `title` tooltip at each
// call site, and remains the value used for filtering — this is display-only.
export function shortNode(nodeId: string): string {
  return nodeId.split('.')[0] || nodeId
}
