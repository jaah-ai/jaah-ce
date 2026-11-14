import { useCallback, useEffect, useLayoutEffect, useRef, useState, type DependencyList, type UIEvent } from 'react'

/** Within this many px of the bottom counts as "at the newest line". */
const STICK_PX = 48

/** A log or chat box that follows its newest line while the reader is at the bottom, and stops once they scroll
 *  back. `content` changing re-follows before paint, as does the box resizing (a hidden tab shown). `toBottom`
 *  follows again; `keepPlace` before revealing earlier rows holds the reader's distance from the bottom. `ref` is a
 *  callback ref: a box that mounts later, or again (the Help chat's thread behind its History view), is watched too.
 *  `stuck` is read by a caller's own effect after this hook's (declared later in the component), as task activity's
 *  window rule does. Used by the console chat, task activity and the Help chat. */
export function useStickToBottom<T extends HTMLElement>(content: DependencyList) {
  const box = useRef<T | null>(null)
  const [node, setNode] = useState<T | null>(null)
  const stuck = useRef(true)
  const fromBottom = useRef<number | null>(null)
  const [away, setAway] = useState(false)
  const follow = () => { if (box.current && stuck.current) box.current.scrollTop = box.current.scrollHeight }
  const ref = useCallback((el: T | null) => {
    box.current = el
    setNode(el)
  }, [])

  useLayoutEffect(follow, content)
  useLayoutEffect(() => {
    const el = box.current
    if (el && fromBottom.current !== null) el.scrollTop = el.scrollHeight - fromBottom.current
    fromBottom.current = null
  })
  useEffect(() => {
    if (!node || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(follow)
    observer.observe(node)
    return () => observer.disconnect()
  }, [node])

  return {
    ref,
    /** The box, for a caller's own measure. */
    box,
    stuck,
    away,
    onScroll: ({ currentTarget: el }: UIEvent<T>) => {
      stuck.current = el.scrollHeight - el.scrollTop - el.clientHeight < STICK_PX
      setAway(!stuck.current)
    },
    toBottom: () => {
      stuck.current = true
      setAway(false)
      follow()
    },
    keepPlace: () => { if (box.current) fromBottom.current = box.current.scrollHeight - box.current.scrollTop },
  }
}
