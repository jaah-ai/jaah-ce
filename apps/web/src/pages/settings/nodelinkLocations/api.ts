// Settings > NodeLink storage's ONE API client (`/api/operator/nodelink-locations`): the operator's storage
// behind each Jaah-managed location. Operator-only; a refusal's `detail` is the server's own sentence.

import { del, get, post, put } from '../../../api'
import type { NodeLinkStore, OrgRef } from '../../../types'
import type { CreateStoreBody, ExistingStoreBody, ManagedLocation, NodeLinkStatus, StorePolicy } from '../../nodes/nodeLinkStore/api'

export const LOCATIONS_PATH = '/api/operator/nodelink-locations'

/** Settings > NodeLink storage, scrolled to `location`'s card. */
export const locationUrl = (location: ManagedLocation) =>
  `/settings?area=infrastructure&section=nodelink-storage&location=${location}`

/** Settings > Infrastructure > NodeLink storage: the routed-VPC knobs the overlap warning reads, above the locations. */
export const ROUTED_KNOBS_URL = '/settings?area=infrastructure&section=nodelink-storage'

/** Another org's NodeLink a "Use existing" save moves onto the location's store. */
export type AbsorbedNodeLink = {
  id: string
  name: string
  status: NodeLinkStatus
  /** Why it may not move; while any NodeLink has one, Save is refused. */
  problem: string | null
}

/** One org whose storages on the file system a "Use existing" save absorbs. */
export type AbsorbedOrg = { org_id: string; org_name: string; stores: string[]; nodelinks: AbsorbedNodeLink[] }

export type AbsorbPreview = {
  location: ManagedLocation
  orgs: AbsorbedOrg[]
  /** What Save answers now (a 409), null when it may proceed. */
  refused: string | null
  /** What moves: `setLocation` sends it back, so Save absorbs exactly what was previewed. */
  fingerprint: string
}

/** One NodeLink on a location's store, any org's: what a Repair admits. */
export type LocationNodeLink = {
  id: string
  name: string
  status: NodeLinkStatus
  in_policy: boolean
  has_access_point: boolean
  org: OrgRef | null
}

export type NodeLinkLocation = {
  location: ManagedLocation
  store: NodeLinkStore | null
  /** The AWS account the store's credential reads as; never typed. */
  account: string | null
  nodelinks: number
  tenants: number
  /** Every NodeLink on it, by org then name. */
  links?: LocationNodeLink[]
  /** The orgs the save that answered this absorbed (a save's answer only). */
  absorbed?: AbsorbedOrg[]
  /** The labels of the routed-VPC knobs left unset: tenants' overlap warning is off until they are set. */
  routed_unset?: string[]
}

/** Exactly one: a file system the CP creates, or one the account already has. */
export type LocationBody = { create: CreateStoreBody } | { existing: ExistingStoreBody }

const locationPath = (location: ManagedLocation) => `${LOCATIONS_PATH}/${encodeURIComponent(location)}`

export const listLocations = () => get<NodeLinkLocation[]>(LOCATIONS_PATH)
/** Set or replace the location's store. 409: the current one still has NodeLinks, or (`fingerprint` of a
 *  preview) what Save absorbs is no longer what was previewed. */
export const setLocation = (location: ManagedLocation, body: LocationBody, fingerprint?: string) =>
  put<NodeLinkLocation>(fingerprint ? `${locationPath(location)}?absorb_preview=${encodeURIComponent(fingerprint)}` : locationPath(location), body)
/** What `setLocation(location, body)` would absorb; writes nothing. */
export const previewLocation = (location: ManagedLocation, body: LocationBody) =>
  put<AbsorbPreview>(`${locationPath(location)}?dry_run=1`, body)
/** One non-blocking pass over a create-new store; press again while `provisioning`. */
export const provisionLocation = (location: ManagedLocation) =>
  post<NodeLinkLocation>(`${locationPath(location)}/provision`, {})
/** Rewrite the store's policy to admit every org's live NodeLink on it; `access_point_count` is how many it
 *  admits. 409 while none needs it. */
export const repairLocationPolicy = (location: ManagedLocation) =>
  post<NodeLinkLocation & { access_point_count: number }>(`${locationPath(location)}/repair-policy`, {})
export const LOCATIONS_KEY = ['nodelink-locations']
export const locationPolicyKey = (location: ManagedLocation) => ['nodelink-location-policy', location]
/** The store's file-system policy read live from AWS; `current` says whether Repair would leave it as is (null
 *  while Jaah writes none there). 409 before it has a file system; 502: AWS's text. */
export const getLocationPolicy = (location: ManagedLocation) =>
  get<StorePolicy>(`${locationPath(location)}/policy`)
/** 409 while NodeLinks use it. */
export const removeLocation = (location: ManagedLocation) => del<null>(locationPath(location))
