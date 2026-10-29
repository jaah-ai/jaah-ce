"""The `lease_slot` executor: build or resume a slot's tree and launch its runner, closing a hand-off."""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import uuid

import control_plane_api
import git_env
import ce_caps
import session_user
import shared_storage
import slot_repos
from worktree_mechanics import core, ports as wm_ports, lifecycle
from reconcile import vocab, tick_wire, context, enact_reset
from reconcile.vocab import GIT_FAST_TIMEOUT, OUTCOME_DONE, OUTCOME_FAILED, OUTCOME_REFUSED_PRECONDITION
from reconcile.tick_wire import RosterSlot, SlotIntent
from reconcile.context import ReconcileContext

log = logging.getLogger(__name__)


# A console hand-off (`lease_slot` payload `handoff`): how long `_close_for_handoff` waits for the old ACP host to
# close its session and end before the launch's kill fallback — the host's `CLOSE_GRACE_S` (30) for the
# cancelled turn plus its `STOP_GRACE_S` (5) for the engine, and a margin. Polled once a second.
HANDOFF_CLOSE_WAIT_S = 45.0


async def _launch_into_slot(
    ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent, obs: dict | None,
) -> tuple[str, str, dict]:
    """The `lease_slot` enactment body — the only dispatch verb.

    `payload["resume"]` True: the tree holds this task's previous attempt and is left as-is, only the port
    band re-applied (no tree on disk: taken as False). False: `claim_mechanics` builds or resets it detached at base (D10); the runner makes its
    own `fleet/task-<id>`. Then the CP's `payload["prompt_bundle"]`, then `launch_runner`.

    The ack extras carry the `generation`/`dispatch_id` fence plus the launch identity for `record_launch`.
    Failures `failed`, vetoes `refused_precondition`; neither retires."""
    import agent_providers  # noqa: PLC0415 — the provider vocabulary: what an unnamed provider is
    import agents  # noqa: PLC0415 — the runner-adapter registry: which vendors this node can launch
    import fleet_dispatch  # noqa: PLC0415 — deferred: daemon-space sibling, needed only on this verb
    import bedrock_launch  # noqa: PLC0415 — deferred, as `fleet_dispatch`: the Jaah-account plan
    import acp_runtime  # noqa: PLC0415 — deferred, as `fleet_dispatch`: the runtime dispatch fact
    from acp import launch as acp_launch_doc  # noqa: PLC0415 — deferred, as above: a hand-off's native id shape

    payload = intent.payload
    task_id = payload.get("task_id")
    # No `branch` read (D10): an older CP that sends one is ignored, not refused (version skew).
    generation = payload.get("generation")
    dispatch_id = str(payload.get("dispatch_id") or "")
    if task_id is None or generation is None or not dispatch_id:
        return OUTCOME_FAILED, (
            f"payload is missing one of task_id/generation/dispatch_id — cannot launch or fence "
            f"an ack (got keys {sorted(payload)})"
        ), {}
    fence = {"generation": int(generation), "dispatch_id": dispatch_id}
    # The console toggling the slot between its chat and terminal fronts (`apps/api/app/routes/console_runtime.py`):
    # a resume of this same task that reopens the engine's own session `native_session_ref` on the other front,
    # after the old front (`from_session_id`) let go of it. Absent for every other dispatch.
    handoff = payload.get("handoff")
    if handoff is not None and not (
            isinstance(handoff, dict) and isinstance(handoff.get("native_session_ref"), str)
            and acp_launch_doc.NATIVE_REF.match(handoff["native_session_ref"])):
        return OUTCOME_REFUSED_PRECONDITION, (
            "the CP's hand-off names no valid native session to reopen — refusing rather than starting the task "
            "over or resuming some other conversation"
        ), {}

    # No "someone else holds this" refusal: the slot is held by this very dispatch, arbitrated by the CP's
    # atomic claim. The fences here are path agreement and the ack's `generation`/`dispatch_id`.
    config = slot.pool_config()
    if config.wt_path(slot.number) != slot.path:
        return OUTCOME_REFUSED_PRECONDITION, (
            f"this node derives {config.wt_path(slot.number)} for {slot.project}#{slot.number} but the CP "
            f"declared {slot.path} — refusing to act on a path the two sides disagree about"
        ), {}
    kind_refusal = tick_wire.slot_kind_mismatch(slot, intent)
    if kind_refusal:
        return OUTCOME_REFUSED_PRECONDITION, kind_refusal, {}
    # This very dispatch already launched here (its ack was lost, e.g. across a daemon restart, and the CP
    # re-served the intent): re-ack that runner. Before anything else, above all the resume kill below, which
    # would otherwise take the runner this intent just launched and then lose the spent launch-claim.
    relaunched = await _launched_by(slot.path, fence)
    if relaunched is not None:
        return OUTCOME_DONE, f"already launched task-{task_id} into {slot.path} for this dispatch", {
            **fence, **relaunched}

    # An SDK AGENT (`agent_process`): the CP names a verified bundle and no prompt, account or
    # provider. Refused before anything is built when this node will not run it.
    import agent_process  # noqa: PLC0415 — deferred, as `fleet_dispatch`: the launch-kind vocabulary

    agent_launch = None
    launch = payload.get("launch")
    if launch is not None:
        kind = launch.get("kind") if isinstance(launch, dict) else None
        if kind == agent_process.LAUNCH_KIND_AGENT:
            try:
                agent_launch = agent_process.AgentLaunch.from_payload(launch)
            except agent_process.AgentLaunchRefused as exc:
                return OUTCOME_REFUSED_PRECONDITION, f"refusing the SDK agent launch: {exc}", {}
        else:
            return OUTCOME_REFUSED_PRECONDITION, (
                f"the CP named a launch this node cannot run (kind "
                f"{kind if isinstance(launch, dict) else type(launch).__name__!r})"
            ), {}
    # An SDK agent runs no model of ours: no provider, account, credential or prompt gate below applies to it.
    no_model = agent_launch is not None

    raw_ports = payload.get("ports")
    band = dict(tick_wire.port_band_from_wire(raw_ports)) if isinstance(raw_ports, dict) else None
    config_dir = payload.get("config_dir")
    # The account's token store, when its token is there — resolved on this node below
    # (`shared_storage.securestorage_dir`); the intent's `securestorage_dir` is an older CP's view, unread.
    securestorage_dir = ""
    # An API-key account's slug, or `""` for OAuth (`apps/api/app/account_auth.py`). The intent never carries
    # the key; for API-key dispatches `config_dir`/`securestorage_dir` are empty (no directory exists). Absent
    # from an older CP ⇒ OAuth.
    api_key_account = str(payload.get("api_key_account") or "")
    # The committer the CP resolved; None (older CP) leaves identity to git config.
    git_author = git_env.parse_identity(payload.get("git_author"))
    # Whose CLI: the leased account's `accounts.provider`, spelled by `app/fleet_verdicts.build_lease_intent`.
    # Absent (older CP) means `agent_providers.DEFAULT_PROVIDER` — a data default, not a vendor fallback: an
    # unregistered provider is refused by name below.
    provider = str(payload.get("provider") or "") or agent_providers.DEFAULT_PROVIDER
    if not no_model and provider not in agents.registered_providers():
        # A refusal, not `failed` ("the node broke"): an older package cannot launch that vendor; retried and
        # parked with the reason.
        return OUTCOME_REFUSED_PRECONDITION, (
            f"the CP named provider {provider!r} for this dispatch but this node has no runner adapter "
            f"for it (this node can launch: "
            f"{', '.join(agents.registered_providers()) or 'nothing'}) — refusing rather than "
            f"launching some other vendor's binary against this account"
        ), {}
    # The Jaah account (K2, `bedrock_launch`): no credential directory and no key — the default CLI on the
    # platform Bedrock account, its credentials per request from the broker. A present but malformed block
    # is refused, never read as "no block", which would launch on whatever account the rest names.
    try:
        # An SDK agent runs no model of ours, so it never carries a Jaah-account block.
        bedrock = None if no_model else bedrock_launch.optional_plan(payload.get("bedrock"))
    except bedrock_launch.PlanRefused as exc:
        return OUTCOME_REFUSED_PRECONDITION, f"the CP's Jaah-account block cannot be launched: {exc}", {}
    if bedrock is not None:
        if payload.get("account_ref") != bedrock.account_ref:
            return OUTCOME_REFUSED_PRECONDITION, (
                "the CP's Jaah-account block names a different session than the dispatch's account — "
                "refusing to act on two answers to one question"
            ), {}
        if not bedrock_launch.admits(agent_providers.capabilities_of(provider)) or api_key_account or config_dir:
            return OUTCOME_REFUSED_PRECONDITION, (
                "the CP named a Jaah-account dispatch together with another account or a non-Claude "
                "provider — refusing: on the Jaah account the tenant's own accounts are never used"
            ), {}
    # The OAuth gates concern a credential directory, which an API-key account never has, so it skips all
    # three. There is deliberately no API-key pre-flight: the broker's only answer is the key itself, which
    # would then sit in the daemon's address space (logs, state, exception locals). The `launch.sh` guard
    # (`build_auth_lines`: an empty fetch exits before `exec`, named on the pane) is the whole fail-closed
    # story.
    account_dir = ""
    # An SDK agent binds no account, so none of the credential gates applies to it.
    if not no_model and not api_key_account and bedrock is None:
        # A credential, not a resolving path: an unauthenticated runner exits at once, repeatedly, until an
        # OOM takes the login that would fix it. No account at all is refused too (the runner
        # would open the default dir). `provider` because each CLI writes a different credential file
        # (`fleet_dispatch.local_account_dir`).
        account_dir = fleet_dispatch.local_account_dir(config_dir, require_credential=True, provider=provider)
        if not account_dir:
            named = config_dir if isinstance(config_dir, str) and config_dir else ""
            whose = (f"the CP named account {named} but this node holds no credential for it" if named
                     else "the CP named no credential account for this dispatch")
            return OUTCOME_REFUSED_PRECONDITION, (
                f"{whose} — refusing to launch a runner that would start unauthenticated and exit "
                f"immediately"
            ), {}
        # Where the token is: the token store, else the config dir (`shared_storage.token_dir`). Only the
        # store is named to the runner; a token only in the config dir exports nothing. A thread: NFS.
        securestorage_dir = await asyncio.to_thread(shared_storage.securestorage_dir, account_dir)
        # A `shared_token_link` provider whose credential is in the store reads it through a link in its
        # per-node home (the probe above asked the store itself). Kept current here, on every launch.
        # Off the loop: a first link lays the vendor layer, which runs the vendor CLI.
        link_refusal = await asyncio.to_thread(
            fleet_dispatch.ensure_shared_credential_link, account_dir, securestorage_dir, provider)
        if link_refusal:
            return OUTCOME_REFUSED_PRECONDITION, (
                f"this account's credential could not be linked into {account_dir}: {link_refusal} "
                f"— refusing to launch a runner that would open no credential or a stale local one"
            ), {}

    # `resume` means only "this slot's `task_id` is this task" (a DB fact), nothing about HEAD — a rebase can
    # leave any branch. Branch policy is the runner's; resume leaves the tree exactly as the dead runner left
    # it and touches nothing `reset_to_base` would.
    resume = bool(payload.get("resume"))
    if resume and obs is None and not os.path.isdir(slot.path):
        # No tree means no previous attempt's work to preserve (a first claim that died at `git worktree
        # add`), so build it as a fresh claim would. Refusing instead re-served the same resume forever: the
        # CP keys resume off the slot's `task_id`, which no node refusal changes. Re-checked on
        # disk, not only `obs`: a tree that appeared since the observation is resumed, never reset.
        log.info(
            "resume %s #%s: worktree %s is absent on disk — nothing to resume, claiming it fresh for task %s",
            slot.project, slot.number, slot.path, task_id,
        )
        resume = False
    if handoff is not None and not resume:
        # A hand-off reopens a conversation in the tree it ran in; with no resume there is no such tree.
        return OUTCOME_REFUSED_PRECONDITION, (
            f"hand-off into {slot.path}: the slot's tree is gone (or the dispatch is no resume) — nothing to hand over"
        ), {}
    # The slot's repository set converges HERE, at the session's start and nowhere else (D7): after
    # the superseded runner is gone on a resume, before `claim_mechanics` on a fresh claim. None = a project
    # with nothing to converge (one repository, today's slot).
    manifest: dict | None = None
    slot_env = slot_repos.SlotEnv(
        band=band, env_vars=payload.get("env_vars"), auto_ports=ctx.auto_ports, node_id=ctx.node_id,
        stack_down_cmd=slot.stack_down_cmd,
    )
    if resume:
        # Kill the runner this one replaces first: a quota-walled default CLI parks rather than exits, and two
        # sessions on one tree and credential is data loss the CP's session fence cannot stop (it fences
        # reports, not git). Only here, bound to a replacement definitely launching — never a separate intent
        # or the `StopFailure` hook.
        # Resolved live (`fresh_window_id_for`, stable `#{window_id}`; `expect_cwd` re-proves the slot). No
        # window: proceed; a window that will not die fails the enactment.

        # THIS IS THE ONLY PLACE THE KILL BELONGS. Not a second CP intent, which can land after the
        # relaunch and take the LIVE runner; not the `StopFailure` hook killing itself, since that also
        # fires on transient errors the session's own retry survives. Killing here binds it to the one
        # moment a replacement is definitely launching: no replacement, no kill.

        # RESOLVED LIVE, never off `obs`: an observation is up to a pass old and a renumbered window index
        # names a NEIGHBOUR, so `fresh_window_id_for` reads the stable `#{window_id}` at kill time and
        # `expect_cwd` re-proves the window lives in this slot. No window found means nothing to kill and
        # the launch proceeds; a window that IS there and does NOT die fails the enactment instead.
        kill_on: dict = {}
        if handoff is not None:
            # A hand-off already asked the old front to let go, off the pass (`_handoff_then_ack`). What is left
            # to kill is that front's OWN window, never whatever else stands in the slot (an operator's pane);
            # still failing the enactment rather than run two fronts on one native session.
            from_session = str(handoff.get("from_session_id") or "")

            async def stale_window_now() -> str:
                front = await _old_front_pane(slot.path, from_session)
                if front is None:
                    return ""
                # Killed on the server that lists it, and only while it still holds that front's pane.
                kill_on.update(socket=front[0], expect_pane_pid=front[2])
                return front[1]

            if await _old_front_window(slot.path, from_session) == "":
                # Live, yet tmux shows no window for its pane: nothing this node can target, so no second front.
                return OUTCOME_FAILED, (
                    f"hand-off into {slot.path}: the old front (session {from_session}) is still running but has "
                    "no tmux window to close — refusing to open a second front on its native session"
                ), {}
        else:
            async def stale_window_now() -> str:
                return await context.fresh_window_id_for(slot.path)
        stale_window = await stale_window_now()
        if stale_window:
            import kill_gates  # noqa: PLC0415 — deferred: subprocess-heavy sibling, needed only here

            killed = await asyncio.to_thread(
                kill_gates.guarded_window_kill, stale_window,
                f"resume {slot.project} #{slot.number}: replacing the broken runner on task {task_id}",
                expect_cwd=slot.path, **kill_on,
            )
            if killed is not True:
                # `kill_session` answers False for both "could not kill" and "already gone": re-probe, and
                # only a window still there blocks.
                if await stale_window_now():
                    return OUTCOME_FAILED, (
                        f"the superseded runner's window ({stale_window}) is still live in {slot.path} "
                        f"and would not die — refusing to launch a second session into the same tree"
                    ), {}
                log.info(
                    "resume %s #%s: the superseded runner's window %s was already gone by kill time",
                    slot.project, slot.number, stale_window,
                )
            else:
                log.info(
                    "resume %s #%s: killed the superseded runner's window %s before relaunching task %s",
                    slot.project, slot.number, stale_window, task_id,
                )
        if handoff is not None:
            # The old front is gone. A terminal front's transcript is the one the new front resumes and appends
            # to: its row's legacy fold stops here (`session_sweep._WindowedReader`). No note: an ACP front.
            import session_vendor  # noqa: PLC0415 — deferred: a leaf read only on this path

            await asyncio.to_thread(session_vendor.close_usage_window, from_session)
        # Re-applying is a no-op in band; kept tier-2 ports are judged against this pass's range
        # (`ctx.auto_ports`). No `claim_mechanics` here — it would destroy the uncommitted work resume
        # preserves. A folder slot has no `.env` and no band: it is resumed exactly as it is.
        if slot.has_repo:
            try:
                manifest = await asyncio.to_thread(
                    slot_repos.converge, config, slot.number, mode=slot_repos.MODE_RESUME, task_id=task_id,
                    env=slot_env,
                )
            except core.ResetRefused as exc:
                return OUTCOME_REFUSED_PRECONDITION, f"repository converge refused: {exc}", {}
            try:
                await asyncio.to_thread(
                    wm_ports.apply_port_band, slot.path, band,
                    port_vars=config.port_vars, auto_ports=ctx.auto_ports,
                )
            except Exception as exc:  # noqa: BLE001 — a band that will not apply is a failed enactment
                return OUTCOME_FAILED, f"could not re-apply the port band: {exc}", {}
    else:
        try:
            if slot.has_repo:
                manifest = await asyncio.to_thread(
                    slot_repos.converge, config, slot.number, mode=slot_repos.MODE_CLAIM, task_id=task_id,
                    env=slot_env,
                )
            await asyncio.to_thread(
                lifecycle.claim_mechanics,
                config, slot.number,
                # No `holder=`/`branch=` (D10): `detach_base=True` leaves HEAD detached at base; the runner
                # makes its own branch.
                account=account_dir, detach_base=True,
                task_id=task_id, node_id=ctx.node_id, band=band,
                auto_ports=ctx.auto_ports,
                # The CP's resolved project environment (§8.1), verbatim, this leg only. Absent (older CP, no
                # rows) is `None`, "never rebuild" — except that on a slot an earlier overlay marked it
                # withdraws the marked variables (how deleting the last Environment row reaches a slot).
                env_vars=payload.get("env_vars"),
            )
        except core.ResetRefused as exc:
            # Refusals here are things the node cannot do (unresolvable clone, bad band), never a live-safety
            # call: `app/slot_state.py::lease` must not have assigned an occupied slot.
            return OUTCOME_REFUSED_PRECONDITION, f"claim mechanics refused: {exc}", {}

    # No enactment-time ask: the CP composes `payload["prompt_bundle"]` at decide time. Never partial: a
    # missing or malformed bundle fails before any network call.
    bundle = payload.get("prompt_bundle") if not no_model else {}
    if not no_model and (
            not isinstance(bundle, dict) or not bundle.get("system_prompt") or not bundle.get("task_prompt")):
        # Key names, never the bundle: it carries prior gate answers, and `why` reaches the log,
        # `slot_intents.reason` and the console. A repr would also exceed `SlotIntentAckIn.detail`'s 8000
        # chars, 422ing the ack into an endless re-serve.
        return OUTCOME_FAILED, (
            f"intent carries no usable prompt bundle at payload['prompt_bundle'] (got "
            f"{type(bundle).__name__} with keys "
            f"{sorted(str(k) for k in bundle) if isinstance(bundle, dict) else 'n/a'}) — "
            f"never launching a runner with an empty or half charter"
        ), {}

    # No pre-spawn holder declare: `slot_state.lease()` binds `state`/`task_id` CP-side in the statement that
    # decided this dispatch. Only the `last_used` stamp remains.
    await enact_reset._stamp_slot_last_used(ctx, slot)

    attachments = bundle.get("attachments") or []
    if attachments:
        await asyncio.to_thread(
            fleet_dispatch.materialize_attachments,
            list(attachments), str(bundle.get("attachments_dir") or ""),
        )

    # Required inputs may not degrade (unlike attachments): the prompt names each path. Failed, not degraded;
    # above the launch claim, which consumes the hold's one permission.
    input_artifacts = bundle.get("input_artifacts") or []
    inputs_dir = str(bundle.get("input_artifacts_dir") or "")
    # Gated on the directory, not the rows: after a Reset nothing may survive, yet the task-keyed dir still
    # holds the previous attempt's document and must be wiped.
    if inputs_dir or input_artifacts:
        try:
            await asyncio.to_thread(
                fleet_dispatch.materialize_prerequisite_artifacts,
                list(input_artifacts), inputs_dir,
            )
        except Exception as exc:  # noqa: BLE001 — every failure is one refusal; the CP counts the attempt
            kinds = ", ".join(str(a.get("kind") or "?") for a in input_artifacts) or "none staged"
            return OUTCOME_FAILED, (
                f"could not stage the required prerequisite input(s) [{kinds}] into {inputs_dir!r}: "
                f"{exc} — never launching a runner whose prompt names an input that is not on disk"
            ), {}

    # An SDK agent's three pre-claim steps (§4.4): bundle, inputs, per-run copy. Above the claim, like the
    # inputs above. Only the run dir and the required inputs raise (fail-loud, as for any runner); a bundle
    # that cannot be staged is recorded and reported by the run through `task.fail` (`agent_process`).
    staged_agent = None
    if agent_launch is not None:
        try:
            staged_agent = await asyncio.to_thread(agent_process.stage, agent_launch, task_id)
        except Exception as exc:  # noqa: BLE001 — every failure is one refusal; nothing was launched
            return OUTCOME_FAILED, (
                f"could not stage SDK agent {agent_launch.display_name}'s run for task {task_id}: {exc} — "
                f"never launching an agent whose inputs are not on disk"
            ), {}

    ref_key = f"task-{task_id}"
    # Last before the spawn: may this dispatch still have the slot? The ack's fence only reports
    # after the fact; this refuses, and consuming the permission in one UPDATE makes it single-flight.
    # `refused_precondition`, not `failed`, for truthfulness (same fate): a superseded dispatch is not a
    # broken node.
    # Do not move this earlier: every other failure (tree build, credential gates, bundle) must come first, or
    # a gate-3 refusal would spend the generation's only permission.

    # `refused_precondition`, not `failed`: TRUTHFULNESS, not durability — both are non-retiring and feed
    # the same park budget, so the row's fate is identical. The CP's hold IS a precondition of this intent
    # and the node just re-verified it; recording a superseded dispatch as a node that BROKE is what sends
    # a reader to a wedged-node diagnosis instead of to the fence.

    # **DO NOT MOVE THIS EARLIER.** The permission is CONSUMED by the asking, and every remaining way to
    # fail is ABOVE — the tree build (whose `reset_to_base` gate 3 is where the wedge itself refuses), the
    # credential/path gates, the prompt-bundle gate. Asked before those, a gate-3 refusal would spend the
    # hold's one permission and refuse anyway, so the retry after a human pushed the branch would be
    # refused too, for the whole life of that generation. Claim last, when only the spawn is left.
    may_launch, refusal = await asyncio.to_thread(
        functools.partial(
            control_plane_api.claim_slot_launch,
            intent.id, generation=int(generation), dispatch_id=dispatch_id,
        )
    )
    if not may_launch:
        if staged_agent is not None:
            await asyncio.to_thread(agent_process.discard, staged_agent)
        return OUTCOME_REFUSED_PRECONDITION, refusal, {}
    # `launch_runner` does not refuse an occupied tree (that would re-decide the CP's assignment); a real
    # failure propagates as `failed`.
    started = await asyncio.to_thread(
        fleet_dispatch.launch_runner,
        project=slot.project,
        repo_path=slot.repo_path,
        worktree=slot.path,
        task_id=task_id,
        ref_key=ref_key,
        system_prompt=str(bundle.get("system_prompt") or ""),
        task_prompt=str(bundle.get("task_prompt") or ""),
        model=str(bundle.get("model") or ""),
        title=str(bundle.get("title") or ""),
        account_dir=account_dir,
        # Read as a negative for version skew: only an explicit `false` disables; otherwise None keeps
        # `build_settings`' `VISIBLE_AUTOCLOSE` default (a positive True would override an operator's `=0`).
        autoclose=False if bundle.get("autoclose") is False else None,
        # A merge-lane run only: the CP spells it, the turn-end refusal rides its hook chain.
        lane=bundle.get("lane") is True,
        securestorage_dir=securestorage_dir,
        # Which vendor's CLI; a default-provider dispatch's `launch.sh` and `--settings` are byte-identical
        # (`tests/test_agent_adapters.py` goldens).
        provider=provider,
        # The slug, never the key; when set, `account_dir` is `""` and `launch.sh` fetches the key at run time
        # (`fleet_dispatch.build_auth_lines`).
        api_key_account=api_key_account,
        git_author=git_author,
        bedrock=bedrock,
        has_repo=slot.has_repo,
        # Project memory's ids, only on an ON dispatch (`project_memory_bundle`); a non-dict is ignored.
        memory=payload.get("memory") if isinstance(payload.get("memory"), dict) else None,
        # The Architectural Guard stanza, only where it is available for the org (`guard_bundle`); a non-dict is
        # ignored. The slot's own base is the integration branch the push backstop measures a new branch from.
        guard=payload.get("guard") if isinstance(payload.get("guard"), dict) else None,
        integration_branch=slot.base,
        # Every non-primary repository's worktree (`permissions.additionalDirectories`); `()` for one repository.
        extra_dirs=slot_repos.extra_dirs(manifest),
        # The CP's resolution of the operator's runtime switch; absent (an older CP) is legacy (`acp_runtime`).
        runtime=acp_runtime.wire_runtime(payload),
        # A console hand-off's engine session, reopened on the new front instead of the task prompt.
        resume_ref=str(handoff["native_session_ref"]) if handoff is not None else "",
        # An SDK agent's staged run, or nothing for every other launch.
        agent=staged_agent,
        # The fence, recorded with the pane itself (`runner_registry.record`): a daemon death after the spawn
        # still leaves a runner `_launched_by` recognises.
        generation=int(generation),
        dispatch_id=dispatch_id,
    )
    if started.get("refused") == "capacity":
        # Nothing launched: the control plane reads the marker, returns the hold and requeues the task.
        if staged_agent is not None:
            await asyncio.to_thread(agent_process.discard, staged_agent)
        return OUTCOME_REFUSED_PRECONDITION, ce_caps.capacity_text(started), dict(fence)
    raw_pid = str(started.get("pid") or "")
    extras = dict(fence)
    extras["session_id"] = started.get("session_id") or None
    extras["pid"] = int(raw_pid) if raw_pid.isdigit() else None
    # Already canonical (`proc_started_at`), the spelling the CP compares for equality.
    extras["pid_start_time"] = started.get("started_at") or None
    return OUTCOME_DONE, f"launched {ref_key} into {slot.path}", extras


async def _launched_by(worktree: str, fence: dict) -> "dict | None":
    """The ack's launch identity of the live runner in `worktree` that this dispatch (`fence`) launched, or None.

    Read off `runner_registry` (pane pid AND start ticks, stamped with the dispatch at launch), never off the
    slot's windows: only a pane this node launched for exactly this `dispatch_id` and `generation` counts."""
    import runner_registry  # noqa: PLC0415 — deferred, as `_old_front_pane`
    import fleet_dispatch  # noqa: PLC0415 — deferred, as in `_launch_into_slot`

    ident = await asyncio.to_thread(runner_registry.live_in_worktree, worktree)
    if ident is None or ident.dispatch_id != fence["dispatch_id"] or ident.generation != fence["generation"]:
        return None
    return {
        "session_id": ident.session_id or None,
        "pid": ident.pane_pid,
        "pid_start_time": await asyncio.to_thread(fleet_dispatch.proc_started_at, ident.pane_pid) or None,
    }


async def _close_for_handoff(slot: RosterSlot, from_session_id: str, task_id: object) -> None:
    """Ask the old front's ACP host to close for a hand-off, then wait (`HANDOFF_CLOSE_WAIT_S`) for that front's
    own process to end (`_old_front_window`). Never raises and never decides: a legacy front (no ACP session
    here), a refused enqueue or a front still up at the deadline all fall through to the launch's kill. Runs off
    the pass and outside the slot lock (`_handoff_then_ack`): it touches no tree, only the hub and the registry."""
    if not from_session_id:
        return
    try:
        hub = context._acp_hub()
        if not await asyncio.to_thread(hub.is_acp, from_session_id):
            return  # a terminal front: its TUI holds no session/close to send, the kill ends it
        queued = await asyncio.to_thread(hub.enqueue_control, from_session_id, {
            "id": uuid.uuid4().hex, "type": "close", "payload": {"handoff": True}})
    except Exception as exc:  # noqa: BLE001 — the kill fallback still guarantees one front
        log.warning("hand-off %s #%s: the ACP runtime hub is unavailable (%s: %s); falling back to the kill",
                    slot.project, slot.number, type(exc).__name__, exc)
        return
    if not queued:
        log.warning("hand-off %s #%s: session %s refused the close; falling back to the kill",
                    slot.project, slot.number, from_session_id)
        return
    # Counted polls, not a clock: the wait is bounded however the sleep is driven.
    for _ in range(int(HANDOFF_CLOSE_WAIT_S)):
        if await _old_front_window(slot.path, from_session_id) is None:
            log.info("hand-off %s #%s: session %s closed for task %s", slot.project, slot.number,
                     from_session_id, task_id)
            return
        await asyncio.sleep(1.0)
    log.warning("hand-off %s #%s: session %s still holds the pane after %.0fs; falling back to the kill",
                slot.project, slot.number, from_session_id, HANDOFF_CLOSE_WAIT_S)


async def _old_front_window(worktree: str, from_session_id: str) -> "str | None":
    """A hand-off's old front (`_old_front_pane`): None once it is gone (or was never this node's runner), else
    its tmux `#{window_id}` ("" when tmux shows no window for its pane)."""
    front = await _old_front_pane(worktree, from_session_id)
    return None if front is None else front[1]


async def _old_front_pane(worktree: str, from_session_id: str) -> "tuple[str, str, int] | None":
    """A hand-off's old front, as the runner this node launched into `worktree` as `from_session_id`: None once
    it is gone (or was never this node's runner), else `(tmux socket, #{window_id}, pane pid)` (socket and id
    "" when tmux shows no window for its pane). Read off `runner_registry` (pane pid AND start ticks), never off
    the slot's cwd: an operator's own pane in the worktree is no runner, so it neither holds a hand-off up nor
    is killed by one. The socket rides along because window ids are per-server: the kill must be on it."""
    import runner_registry  # noqa: PLC0415 — deferred: a stdlib leaf read only on this path

    if not from_session_id:
        return None
    ident = await asyncio.to_thread(runner_registry.live_in_worktree, worktree)
    if ident is None or ident.session_id != from_session_id:
        return None
    socket, window = await pane_window_for_pid(ident.tmux_pane_pid)
    return socket, window, ident.tmux_pane_pid


async def pane_window_for_pid(pane_pid: int) -> "tuple[str, str]":
    """`(socket, #{window_id})` of the window holding the pane whose `#{pane_pid}` is `pane_pid`, via a live
    `tmux list-panes -a`, or `("", "")`. Same server walk as `fresh_window_id_for`; pane pids are host-unique,
    so the server that lists it is the one to kill on."""
    for socket in session_user.manage_sockets():
        if socket and not os.path.exists(socket):
            continue
        rc, out, _err = await vocab._sh(
            session_user.tmux_cmd(socket, "list-panes", "-a", "-F", "#{pane_pid}\t#{window_id}"),
            GIT_FAST_TIMEOUT,
        )
        if rc != 0:
            continue
        for line in out.splitlines():
            pid, _, wid = line.partition("\t")
            if wid and pid.strip() == str(pane_pid):
                return socket, wid
    return "", ""


async def _enact_lease_slot(ctx: "ReconcileContext", slot: RosterSlot, intent: SlotIntent,
                            obs: dict | None) -> tuple[str, str, dict]:
    return await _launch_into_slot(ctx, slot, intent, obs)
