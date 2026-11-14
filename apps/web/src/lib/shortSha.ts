/* The commit as people read it — seven characters, or null before there is one. One home for Compliance
 * (repo/cell sha), Customize > Agents (resolved commits), Audit > Sessions, Production QA, a hosted app's last
 * deploy and a project's Memory and Merge lane tabs; do not hand-roll another. */

export function shortSha(sha: string | null | undefined): string | null {
  return sha ? sha.slice(0, 7) : null
}
