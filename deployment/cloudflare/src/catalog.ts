import { DurableObject } from "cloudflare:workers";

type Value = string | number | null;
type Statement = { sql: string; params: Value[] };
type QueryResult = { columns: string[]; rows: Value[][]; rowcount: number; more: boolean };
type StageRef = { id: string; count: number; digest: string };
type Stage = { revision: number; count: number; digest: string; touched: number };
type CatalogRequest = {
	revision: number;
	pending?: Statement[];
	statement: Statement;
	id: string;
	statements: Statement[];
	root?: { repository: string; phase: string };
	staged?: StageRef;
	sequence: number;
	previous: string;
	chunk: string;
	table: string;
	after?: string | number;
	offset?: number;
};
const MAX_BODY = 8 * 1024 * 1024;
const MAX_FRAME = 512 * 1024;
const STAGE_TTL = 24 * 60 * 60 * 1000;
const EXPORT_KEYS: Record<string, string> = {
	forge_meta: "id", audit_log: "seq", http_tokens: "key", principals: "key",
	teams: "key", roles: "key", role_bindings: "key", view_descriptors: "key",
	views: "key", transactions: "key", changes: "key", projection_jobs: "key",
};

class Conflict extends Error {}

function reply(body: unknown, status = 200): Response {
	return Response.json(body, { status, headers: { "Cache-Control": "no-store" } });
}

async function sha256(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
	return Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), b => b.toString(16).padStart(2, "0")).join("");
}

/** One catalog per forge. Only the Container's outbound binding can reach it. */
export class ForgeCatalog extends DurableObject {
	constructor(ctx: DurableObjectState, env: Cloudflare.Env) {
		super(ctx, env);
		this.ctx.storage.sql.exec(`
			CREATE TABLE IF NOT EXISTS _bastion_revision (id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
			INSERT OR IGNORE INTO _bastion_revision VALUES (1, 0);
			CREATE TABLE IF NOT EXISTS _bastion_receipts (id TEXT PRIMARY KEY, digest TEXT NOT NULL, revision INTEGER NOT NULL);
			CREATE TABLE IF NOT EXISTS _bastion_root (id INTEGER PRIMARY KEY, config TEXT NOT NULL);
			CREATE TABLE IF NOT EXISTS _bastion_staged (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, count INTEGER NOT NULL, digest TEXT NOT NULL, touched INTEGER NOT NULL);
			CREATE TABLE IF NOT EXISTS _bastion_frames (id TEXT NOT NULL, sequence INTEGER NOT NULL, chunk BLOB NOT NULL, digest TEXT NOT NULL, PRIMARY KEY (id, sequence));
		`);
	}

	private revision(): number {
		return this.ctx.storage.sql.exec<{ revision: number }>("SELECT revision FROM _bastion_revision WHERE id = 1").one().revision;
	}

	private fence(revision: number): void {
		if (revision !== this.revision()) throw new Conflict("catalog revision changed");
	}

	private validate(statement: Statement): void {
		// Internal metadata is never accessible through the SQL adapter. No SQL
		// transaction control: every operation is enclosed in transactionSync.
		if (!statement || typeof statement.sql !== "string" || !Array.isArray(statement.params) ||
			statement.params.some((v) => v !== null && typeof v !== "string" && (typeof v !== "number" || !Number.isFinite(v))) ||
			!/^\s*(SELECT|INSERT|UPDATE|DELETE|CREATE (?:TABLE|INDEX|UNIQUE INDEX))\b/i.test(statement.sql) ||
			/_bastion_|sqlite_|\b(?:PRAGMA|ATTACH|DETACH|TRIGGER)\b/i.test(statement.sql)) {
			throw new Error("unsupported catalog statement");
		}
	}

	private execute(statement: Statement, offset = 0): QueryResult {
		this.validate(statement);
		const reading = /^\s*SELECT\b/i.test(statement.sql);
		if (!Number.isSafeInteger(offset) || offset < 0 || (!reading && offset !== 0)) throw new Error("invalid query offset");
		// Bulk catalog readers retain their existing SQL semantics while each
		// native response stays bounded. The revision fence makes all pages one
		// logical read; no live SQL cursor crosses a request boundary.
		const sql = reading ? `SELECT * FROM (${statement.sql.trim().replace(/;$/, "")}) LIMIT 256 OFFSET ${offset}` : statement.sql;
		const cursor = this.ctx.storage.sql.exec(sql, ...statement.params);
		const columns = cursor.columnNames;
		const rows: Value[][] = [];
		let size = new TextEncoder().encode(JSON.stringify(columns)).byteLength;
		let more = false;
		for (const row of cursor.raw()) {
			const bytes = new TextEncoder().encode(JSON.stringify(row)).byteLength + 1;
			if (size + bytes > MAX_BODY - 1024) {
				if (!reading || !rows.length) throw new Error("catalog result exceeds size limit");
				more = true;
				break;
			}
			size += bytes;
			rows.push(row as Value[]);
		}
		const rowcount = /^\s*(INSERT|UPDATE|DELETE)\b/i.test(statement.sql)
			? this.ctx.storage.sql.exec<{ count: number }>("SELECT changes() AS count").one().count : -1;
		return { columns, rows, rowcount, more: more || (reading && rows.length === 256) };
	}

	private replay(statements: Iterable<Statement>): void {
		for (const statement of statements) {
			this.validate(statement);
			// Commit/replay does not need row counts or query results. Consume
			// synchronously, avoiding full response materialization per write.
			for (const _row of this.ctx.storage.sql.exec(statement.sql, ...statement.params)) {}
		}
	}

	private *stagedStatements(reference: StageRef, revision: number): Generator<Statement> {
		const stage = this.ctx.storage.sql.exec<Stage>("SELECT revision, count, digest, touched FROM _bastion_staged WHERE id = ?", reference.id).toArray()[0];
		if (!stage || stage.revision !== revision || stage.count !== reference.count || stage.digest !== reference.digest || stage.touched < Date.now() - STAGE_TTL) throw new Conflict("staged transaction changed or expired");
		const decoder = new TextDecoder("utf-8", { fatal: true });
		let buffered = "";
		let count = 0;
		for (const frame of this.ctx.storage.sql.exec<{ chunk: ArrayBuffer }>("SELECT chunk FROM _bastion_frames WHERE id = ? ORDER BY sequence", reference.id)) {
			buffered += decoder.decode(frame.chunk, { stream: true });
			let newline: number;
			while ((newline = buffered.indexOf("\n")) !== -1) {
				yield JSON.parse(buffered.slice(0, newline));
				buffered = buffered.slice(newline + 1);
			}
			if (buffered.length > 16 * 1024 * 1024) throw new Error("staged statement too large");
			count++;
		}
		buffered += decoder.decode();
		if (buffered || count !== reference.count) throw new Conflict("incomplete staged transaction");
	}

	private discard(id: string): void {
		this.ctx.storage.sql.exec("DELETE FROM _bastion_frames WHERE id = ?", id);
		this.ctx.storage.sql.exec("DELETE FROM _bastion_staged WHERE id = ?", id);
	}

	override async alarm(): Promise<void> {
		this.ctx.storage.transactionSync(() => {
			this.ctx.storage.sql.exec("DELETE FROM _bastion_frames WHERE id IN (SELECT id FROM _bastion_staged WHERE touched <= ?)", Date.now() - STAGE_TTL);
			this.ctx.storage.sql.exec("DELETE FROM _bastion_staged WHERE touched <= ?", Date.now() - STAGE_TTL);
		});
		const next = this.ctx.storage.sql.exec<{ at: number | null }>("SELECT MIN(touched) AS at FROM _bastion_staged").one().at;
		if (next !== null) await this.ctx.storage.setAlarm(next + STAGE_TTL);
	}

	private exportPage(body: CatalogRequest): Response {
		this.fence(body.revision);
		const key = Object.hasOwn(EXPORT_KEYS, body.table) ? EXPORT_KEYS[body.table] : undefined;
		if (!key || (body.after !== undefined && typeof body.after !== "string" && typeof body.after !== "number")) throw new Error("invalid export table or cursor");
		const cursor = body.after === undefined
			? this.ctx.storage.sql.exec(`SELECT * FROM ${body.table} ORDER BY ${key} LIMIT 256`)
			: this.ctx.storage.sql.exec(`SELECT * FROM ${body.table} WHERE ${key} > ? ORDER BY ${key} LIMIT 256`, body.after);
		const columns = cursor.columnNames;
		const keyIndex = columns.indexOf(key);
		const rows: Value[][] = [];
		let size = 0;
		let after = body.after;
		for (const row of cursor.raw()) {
			const bytes = new TextEncoder().encode(JSON.stringify(row)).byteLength;
			if (size + bytes > MAX_BODY - 1024) {
				if (!rows.length) throw new Error("catalog row exceeds export limit");
				break;
			}
			rows.push(row as Value[]);
			size += bytes + 1;
			after = row[keyIndex] as string | number;
		}
		// A nonempty last page is followed by an empty page. No unstable SQL
		// cursor is retained across awaits or requests; each page is fenced.
		return reply({ columns, rows, after, done: rows.length === 0 });
	}

	override async fetch(request: Request): Promise<Response> {
		if (request.method !== "POST") return reply({ error: "method-not-allowed" }, 405);
		let body: CatalogRequest;
		try {
			const reader = request.body?.getReader();
			if (!reader) return reply({ error: "missing-body" }, 400);
			const chunks: Uint8Array[] = [];
			let size = 0;
			while (true) {
				const { done, value } = await reader.read();
				if (done) break;
				size += value.byteLength;
				if (size > MAX_BODY) { await reader.cancel(); return reply({ error: "body-too-large" }, 413); }
				chunks.push(value);
			}
			const bytes = new Uint8Array(size);
			let offset = 0;
			for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
			body = JSON.parse(new TextDecoder().decode(bytes));
		} catch { return reply({ error: "invalid-body" }, 400); }
		try {
			const path = new URL(request.url).pathname;
			if (path === "/status") {
				const root = this.ctx.storage.sql.exec<{ config: string }>("SELECT config FROM _bastion_root WHERE id = 1").toArray()[0];
				return reply({ revision: this.revision(), size: this.ctx.storage.sql.databaseSize, root: root ? JSON.parse(root.config) : null });
			}
			if (path === "/export") return this.exportPage(body);
			if (path === "/stage") {
				if (typeof body.id !== "string" || !body.id || body.id.length > 64 || !Number.isSafeInteger(body.sequence) || body.sequence < 0 || typeof body.previous !== "string" || typeof body.chunk !== "string" || body.chunk.length > Math.ceil(MAX_FRAME / 3) * 4) throw new Error("invalid staged frame");
				const chunk = Uint8Array.from(atob(body.chunk), c => c.charCodeAt(0));
				if (!chunk.length || chunk.length > MAX_FRAME) throw new Error("invalid staged frame size");
				const prefix = new TextEncoder().encode(body.previous);
				const encoded = new Uint8Array(prefix.length + chunk.length);
				encoded.set(prefix);
				encoded.set(chunk, prefix.length);
				const digest = await sha256(encoded);
				// Schedule before writing a frame: even a crash immediately after
				// staging leaves a durable cleanup alarm, not orphaned storage.
				if (await this.ctx.storage.getAlarm() === null) await this.ctx.storage.setAlarm(Date.now() + STAGE_TTL);
				const staged = this.ctx.storage.transactionSync(() => {
					this.fence(body.revision);
					if (this.ctx.storage.sql.exec("SELECT id FROM _bastion_receipts WHERE id = ?", body.id).toArray().length) throw new Conflict("transaction already committed");
					const session = this.ctx.storage.sql.exec<Stage>("SELECT revision, count, digest, touched FROM _bastion_staged WHERE id = ?", body.id).toArray()[0];
					const prior = this.ctx.storage.sql.exec<{ digest: string }>("SELECT digest FROM _bastion_frames WHERE id = ? AND sequence = ?", body.id, body.sequence).toArray()[0];
					if (prior) {
						if (!session || session.revision !== body.revision || prior.digest !== digest || session.touched < Date.now() - STAGE_TTL) throw new Conflict("frame identity reused");
						return { id: body.id, count: body.sequence + 1, digest };
					}
					if ((session && (session.revision !== body.revision || session.count !== body.sequence || session.digest !== body.previous || session.touched < Date.now() - STAGE_TTL)) || (!session && (body.sequence !== 0 || body.previous !== ""))) throw new Conflict("out-of-order staged frame");
					this.ctx.storage.sql.exec("INSERT INTO _bastion_frames VALUES (?, ?, ?, ?)", body.id, body.sequence, chunk.buffer, digest);
					this.ctx.storage.sql.exec("INSERT OR REPLACE INTO _bastion_staged VALUES (?, ?, ?, ?, ?)", body.id, body.revision, body.sequence + 1, digest, Date.now());
					return { id: body.id, count: body.sequence + 1, digest };
				});
				return reply(staged);
			}
			if (path === "/discard") {
				if (typeof body.id !== "string" || !body.id || body.id.length > 64) throw new Error("invalid transaction identity");
				this.ctx.storage.transactionSync(() => this.discard(body.id));
				return reply({ discarded: true });
			}
			if (path === "/query") {
				let result: QueryResult | undefined;
				const discard = new Error("discard speculative transaction");
				try {
					this.ctx.storage.transactionSync(() => {
						this.fence(body.revision);
						if (body.staged) this.replay(this.stagedStatements(body.staged, body.revision));
						this.replay(body.pending ?? []);
						result = this.execute(body.statement, body.offset);
						throw discard;
					});
				} catch (error) { if (error !== discard) throw error; }
				return reply(result);
			}
			if (path === "/commit") {
				if (typeof body.id !== "string" || !body.id || body.id.length > 64 || !Array.isArray(body.statements) || (body.staged && body.staged.id !== body.id)) throw new Error("invalid commit");
				// A receipt and the writes share one transaction. Retrying a lost
				// response never replays an audit insert or other mutation twice.
				const encoded = new TextEncoder().encode(JSON.stringify({ revision: body.revision, statements: body.statements, staged: body.staged, root: body.root }));
				const digest = await sha256(encoded);
				const revision = this.ctx.storage.transactionSync(() => {
					const receipt = this.ctx.storage.sql.exec<{ digest: string; revision: number }>("SELECT digest, revision FROM _bastion_receipts WHERE id = ?", body.id).toArray()[0];
					if (receipt) {
						if (receipt.digest !== digest) throw new Conflict("commit identity reused");
						return receipt.revision;
					}
					this.fence(body.revision);
					if (body.staged) this.replay(this.stagedStatements(body.staged, body.revision));
					this.replay(body.statements);
					if (body.root !== undefined) {
						if (!body.root || typeof body.root.repository !== "string" || !["bootstrapping", "ready"].includes(body.root.phase)) throw new Error("invalid root config");
						this.ctx.storage.sql.exec("INSERT OR REPLACE INTO _bastion_root VALUES (1, ?)", JSON.stringify(body.root));
					}
					const next = body.revision + 1;
					this.ctx.storage.sql.exec("UPDATE _bastion_revision SET revision = ? WHERE id = 1", next);
					this.ctx.storage.sql.exec("INSERT INTO _bastion_receipts VALUES (?, ?, ?)", body.id, digest, next);
					this.ctx.storage.sql.exec("DELETE FROM _bastion_receipts WHERE revision < ?", next - 256);
					if (body.staged) this.discard(body.id);
					return next;
				});
				return reply({ revision });
			}
			return reply({ error: "not-found" }, 404);
		} catch (error) {
			// SQL, parameter values, and account data never enter error responses.
			return reply({ error: error instanceof Conflict ? "revision-conflict" : "catalog-operation-failed" }, error instanceof Conflict ? 409 : 503);
		}
	}
}
