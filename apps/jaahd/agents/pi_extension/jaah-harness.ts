/**
 * jaah-harness.ts — the fleet's hook layer and question tool inside a Pi runner: a thin shim, translation only.
 *
 * Loaded by `agents/pi_rpc.py` (`--extension <this file>`, from the packaged, root-owned tree; `--no-extensions`
 * keeps the worktree's own `.pi/` extensions off). Everything it knows comes from the run's `JAAH_PI_LAUNCH` document
 * (`agents/pi.py::PiAdapter.acp_launch`): the engine's hook file (`hooks`), THE neutral dispatcher's path
 * and the interpreter that runs it (`dispatcher`, `hook_bridge_core.py`; `python`), Pi's tool-input spellings (`tool_inputs`), its session file's turn mark
 * (`turn_mark`), and the question tool's name and no-answer result (`ask_tool`, `no_answer`). Every decision is the
 * dispatcher's:
 *
 *   tool_call               -> dispatch PreToolUse; a deny blocks the call with its reason, and so does a dispatcher
 *                              that cannot run (fail closed: the guards are unseen).
 *   tool_result             -> dispatch PostToolUse (PostToolUseFailure on an error); the context joins the result.
 *   session_before_compact  -> dispatch PreCompact (the read-once guard's reset).
 *   the question tool       -> one `ctx.ui.select` per question; pi-acp asks it as a `session/request_permission`
 *                              (`pi-ui-<id>`, `AcpDialect.question_call_prefix`) the ACP host files as an operator
 *                              round, and the answers return as the tool's result.
 *
 * The lifecycle events (SessionStart, UserPromptSubmit, Stop, SessionEnd) are the ACP host's (`acp/lifecycle.py`).
 */
import { spawn } from "node:child_process";
import { Type } from "typebox";

/** The outer wall clock on one dispatch (`acp/lifecycle.py::DISPATCH_BUDGET_S`); each hook's own is the dispatcher's. */
const DISPATCH_BUDGET_MS = 600_000;
/** The heading of hook context added to a tool result (`acp/host.py::GUIDANCE_HEADING`). */
const GUIDANCE_HEADING = "Platform guidance:";
/** Removed from the dispatcher's environment: names that load code into the interpreter before it runs. */
const SCRUBBED_ENV = ["LD_PRELOAD", "LD_AUDIT", "LD_LIBRARY_PATH"];

type Verdict = { decision: string; reason: string; context: string };
type Launch = {
	hooks?: unknown;
	dispatcher?: string;
	python?: string;
	tool_inputs?: Record<string, Record<string, string>>;
	turn_mark?: string;
	ask_tool?: string;
	no_answer?: string;
};

function launch(): Launch {
	try {
		const doc = JSON.parse(process.env.JAAH_PI_LAUNCH ?? "");
		return doc && typeof doc === "object" ? doc : {};
	} catch {
		return {};
	}
}

/** One Claude event through the dispatcher (`hook_bridge_core.py dispatch --hooks-stdin`); its verdict. */
function dispatch(doc: Launch, event: string, payload: Record<string, unknown>): Promise<Verdict> {
	return new Promise((resolve, reject) => {
		const env = { ...process.env };
		for (const name of SCRUBBED_ENV) delete env[name];
		const cwd = typeof payload.cwd === "string" && payload.cwd ? payload.cwd : undefined;
		const child = spawn(doc.python || "python3", ["-I", String(doc.dispatcher), "dispatch", "--event", event, "--hooks-stdin"],
			{ cwd, env, stdio: ["pipe", "pipe", "inherit"], timeout: DISPATCH_BUDGET_MS, killSignal: "SIGKILL" });
		let out = "";
		child.stdout.setEncoding("utf8");
		child.stdout.on("data", (chunk) => { out += chunk; });
		child.on("error", reject);
		// A dispatcher that exits before reading its stdin is the `close` below (no verdict), never an unhandled EPIPE.
		child.stdin.on("error", () => undefined);
		child.on("close", () => {
			try {
				const result = JSON.parse(out.trim().split("\n").pop() ?? "");
				resolve({ decision: result.decision === "deny" ? "deny" : "allow", reason: String(result.reason ?? ""),
					context: String(result.context ?? "") });
			} catch (exc) {
				reject(exc);
			}
		});
		child.stdin.end(JSON.stringify({ hooks: doc.hooks, payload }));
	});
}

/** An object's keys in Claude's spelling (`names`: `{pi key: Claude key}`), the objects in its arrays too (an `edit`'s
 * `edits[]`). A key already spelled as one of the targets is dropped, so the model cannot hand the guards a `file_path`
 * other than the `path` Pi acts on; other keys pass as they are. */
function respelled(value: Record<string, unknown>, names: Record<string, string>): Record<string, unknown> {
	const targets = new Set(Object.values(names));
	const out: Record<string, unknown> = {};
	for (const [key, item] of Object.entries(value ?? {})) {
		if (targets.has(key) && !(key in names)) continue;
		out[names[key] ?? key] = Array.isArray(item)
			? item.map((each) => (each && typeof each === "object" && !Array.isArray(each) ? respelled(each, names) : each))
			: item;
	}
	return out;
}

/** A Pi tool's input in Claude's spelling (`tool_inputs`: `{tool: {pi key: Claude key}}`). */
function claudeInput(doc: Launch, tool: string, input: Record<string, unknown>): Record<string, unknown> {
	return respelled(input, doc.tool_inputs?.[tool] ?? {});
}

function resultText(content: { type: string; text?: string }[]): string {
	return (content ?? []).filter((block) => block.type === "text").map((block) => block.text ?? "").join("\n");
}

export default function (pi: any) {
	const doc = launch();
	const base = (ctx: any, event: string) => ({
		hook_event_name: event,
		session_id: ctx.sessionManager.getSessionId(),
		transcript_path: ctx.sessionManager.getSessionFile() ?? "",
		cwd: ctx.cwd,
		...(doc.turn_mark ? { transcript_turn_mark: doc.turn_mark } : {}),
	});

	pi.on("tool_call", async (event: any, ctx: any) => {
		let verdict: Verdict;
		try {
			verdict = await dispatch(doc, "PreToolUse", {
				...base(ctx, "PreToolUse"), tool_name: event.toolName,
				tool_input: claudeInput(doc, event.toolName, event.input),
			});
		} catch (exc) {
			return { block: true, reason: `the fleet's hook layer could not run (${exc}), so this call cannot be checked` };
		}
		if (verdict.decision === "deny") return { block: true, reason: verdict.reason || "blocked by a fleet guard" };
		return undefined;
	});

	pi.on("tool_result", async (event: any, ctx: any) => {
		const name = event.isError ? "PostToolUseFailure" : "PostToolUse";
		const text = resultText(event.content);
		let verdict: Verdict;
		try {
			verdict = await dispatch(doc, name, {
				...base(ctx, name), tool_name: event.toolName, tool_input: claudeInput(doc, event.toolName, event.input),
				tool_response: { stdout: text, is_error: Boolean(event.isError) }, ...(event.isError ? { error: text } : {}),
			});
		} catch {
			return undefined; // nothing left to stop: fail open
		}
		if (!verdict.context.trim()) return undefined;
		return {
			content: [...(event.content ?? []), { type: "text", text: `${GUIDANCE_HEADING}\n${verdict.context}` }],
			...(event.structuredContent === undefined ? {} : { structuredContent: event.structuredContent }),
		};
	});

	pi.on("session_before_compact", async (event: any, ctx: any) => {
		try {
			await dispatch(doc, "PreCompact", {
				...base(ctx, "PreCompact"), trigger: event.reason === "manual" ? "manual" : "auto",
				custom_instructions: event.customInstructions ?? "",
			});
		} catch {
			// fail open: a compaction never waits on a hook that cannot run
		}
		return undefined;
	});

	if (!doc.ask_tool) return;
	const Option = Type.Object({
		label: Type.String({ description: "The answer, as the operator sees it" }),
		description: Type.Optional(Type.String({ description: "What choosing it means" })),
	});
	pi.registerTool({
		name: doc.ask_tool,
		label: "Ask the operator",
		description: "Ask the operator one or more questions, each answered by choosing ONE of its options; waits for "
			+ "the answers.",
		parameters: Type.Object({
			questions: Type.Array(Type.Object({
				question: Type.String({ description: "The question" }),
				header: Type.Optional(Type.String({ description: "A short label for it" })),
				options: Type.Array(Option, { description: "The answers to choose from" }),
			})),
		}),
		executionMode: "sequential",
		async execute(_id: string, params: any, _signal: unknown, _update: unknown, ctx: any) {
			const answers: string[] = [];
			for (const q of params.questions ?? []) {
				const labels = (q.options ?? []).map((o: any) => String(o.label));
				const answer = labels.length ? await ctx.ui.select(String(q.question), labels) : undefined;
				answers.push(`${JSON.stringify(String(q.question))} = ${JSON.stringify(answer ?? doc.no_answer ?? "")}`);
			}
			return { content: [{ type: "text", text: answers.join("\n") }], details: undefined };
		},
	});
}
