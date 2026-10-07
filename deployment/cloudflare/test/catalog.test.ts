import { env } from "cloudflare:workers";
import { runInDurableObject, runDurableObjectAlarm } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import type { ForgeCatalog } from "../src/catalog";

const binding = (env as { CATALOG: DurableObjectNamespace<ForgeCatalog> }).CATALOG;
const statement = (sql: string, ...params: (string | number | null)[]) => ({ sql, params });

async function call(stub: DurableObjectStub<ForgeCatalog>, path: string, body: unknown) {
	return stub.fetch(new Request(`http://catalog.bastion.internal${path}`, {
		method: "POST", body: JSON.stringify(body), headers: { "Content-Type": "application/json" },
	}));
}

async function seeded(name: string) {
	const stub = binding.getByName(name);
	const result = await call(stub, "/commit", { id: "seed", revision: 0, statements: [statement("CREATE TABLE items (key TEXT PRIMARY KEY, value TEXT)"), statement("INSERT INTO items VALUES (?, ?)", "a", "original")] });
	expect(result.status).toBe(200);
	return stub;
}

describe("Durable Object catalog transactions", () => {
	it("reads tentative writes but rolls them back, then commits exactly once", async () => {
		const stub = await seeded("atomic");
		const writes = [statement("UPDATE items SET value = ? WHERE key = ?", "new", "a")];
		const result = await call(stub, "/query", { revision: 1, pending: writes, statement: statement("SELECT value FROM items WHERE key = ?", "a") });
		expect(await result.json()).toMatchObject({ rows: [["new"]] });
		await runInDurableObject(stub, (_instance, state) => {
			expect(state.storage.sql.exec("SELECT value FROM items").one()).toEqual({ value: "original" });
		});
		const commit = { id: "update", revision: 1, statements: writes };
		expect(await (await call(stub, "/commit", commit)).json()).toEqual({ revision: 2 });
		expect(await (await call(stub, "/commit", commit)).json()).toEqual({ revision: 2 });
		expect((await call(stub, "/commit", { ...commit, statements: [] })).status).toBe(409);
	});

	it("rolls back the entire batch on constraint failure", async () => {
		const stub = await seeded("rollback");
		const result = await call(stub, "/commit", { id: "fail", revision: 1, statements: [statement("UPDATE items SET value = 'changed'"), statement("INSERT INTO items VALUES ('a', 'duplicate')")] });
		expect(result.status).toBe(503);
		await runInDurableObject(stub, (_instance, state) => {
			expect(state.storage.sql.exec("SELECT value FROM items").one()).toEqual({ value: "original" });
			expect(state.storage.sql.exec("SELECT revision FROM _bastion_revision").one()).toEqual({ revision: 1 });
		});
	});

	it("rejects stale readers and writers and keeps forge identities separate", async () => {
		const first = await seeded("first");
		const second = await seeded("second");
		expect((await call(first, "/commit", { id: "next", revision: 1, statements: [statement("UPDATE items SET value = 'first'")] })).status).toBe(200);
		expect((await call(first, "/query", { revision: 1, statement: statement("SELECT * FROM items") })).status).toBe(409);
		expect((await call(first, "/commit", { id: "stale", revision: 1, statements: [] })).status).toBe(409);
		expect(await (await call(second, "/query", { revision: 1, statement: statement("SELECT value FROM items") })).json()).toMatchObject({ rows: [["original"]] });
	});

	it("persists root configuration atomically and denies internal SQL access", async () => {
		const stub = await seeded("root");
		const root = { repository: '{"repository_id":"test"}', phase: "ready" };
		expect((await call(stub, "/commit", { id: "root", revision: 1, statements: [], root })).status).toBe(200);
		expect(await (await call(stub, "/status", {})).json()).toMatchObject({ revision: 2, root });
		expect((await call(stub, "/query", { revision: 2, statement: statement("SELECT * FROM _bastion_receipts") })).status).toBe(503);
	});

	it("stages immutable frames without publishing and commits them exactly once", async () => {
		const stub = await seeded("staged");
		const writes = [statement("UPDATE items SET value = ? WHERE key = ?", "staged", "a")];
		const frame = { id: "staged-update", revision: 1, sequence: 0, previous: "", chunk: btoa(writes.map(s => JSON.stringify(s) + "\n").join("")) };
		const staged = await (await call(stub, "/stage", frame)).json();
		expect(await (await call(stub, "/stage", frame)).json()).toEqual(staged);
		expect((await call(stub, "/stage", { ...frame, chunk: btoa("changed") })).status).toBe(409);
		expect((await call(stub, "/stage", { ...frame, sequence: 2 })).status).toBe(409);
		expect(await (await call(stub, "/query", { revision: 1, statement: statement("SELECT value FROM items") })).json()).toMatchObject({ rows: [["original"]] });
		expect(await (await call(stub, "/query", { revision: 1, staged, statement: statement("SELECT value FROM items") })).json()).toMatchObject({ rows: [["staged"]] });
		const commit = { id: frame.id, revision: 1, staged, statements: [], root: { repository: "{}", phase: "ready" } };
		expect(await (await call(stub, "/commit", commit)).json()).toEqual({ revision: 2 });
		expect(await (await call(stub, "/commit", commit)).json()).toEqual({ revision: 2 });
		await runInDurableObject(stub, (_instance, state) => {
			expect(state.storage.sql.exec("SELECT value FROM items").one()).toEqual({ value: "staged" });
			expect(state.storage.sql.exec("SELECT COUNT(*) AS count FROM _bastion_frames").one()).toEqual({ count: 0 });
		});
	});

	it("rejects incomplete streams and rolls back failed staged publication", async () => {
		const stub = await seeded("staged-failure");
		const writes = [statement("UPDATE items SET value = 'changed'"), statement("INSERT INTO items VALUES ('a', 'duplicate')")];
		let staged = await (await call(stub, "/stage", { id: "constraint", revision: 1, sequence: 0, previous: "", chunk: btoa(writes.map(s => JSON.stringify(s) + "\n").join("")) })).json();
		expect((await call(stub, "/commit", { id: "constraint", revision: 1, staged, statements: [] })).status).toBe(503);
		staged = await (await call(stub, "/stage", { id: "incomplete", revision: 1, sequence: 0, previous: "", chunk: btoa(JSON.stringify(writes[0])) })).json();
		expect((await call(stub, "/commit", { id: "incomplete", revision: 1, staged, statements: [] })).status).toBe(409);
		expect((await call(stub, "/discard", { id: "constraint" })).status).toBe(200);
		expect((await call(stub, "/discard", { id: "constraint" })).status).toBe(200);
		await runInDurableObject(stub, (_instance, state) => {
			expect(state.storage.sql.exec("SELECT value FROM items").one()).toEqual({ value: "original" });
			expect(state.storage.sql.exec("SELECT revision FROM _bastion_revision").one()).toEqual({ revision: 1 });
		});
	});

	it("expires abandoned frames without altering catalog data or revision", async () => {
		const stub = await seeded("staged-expiry");
		await call(stub, "/stage", { id: "abandoned", revision: 1, sequence: 0, previous: "", chunk: btoa(JSON.stringify(statement("DELETE FROM items")) + "\n") });
		await runInDurableObject(stub, (_instance, state) => {
			state.storage.sql.exec("UPDATE _bastion_staged SET touched = ?", Date.now() - 25 * 60 * 60 * 1000);
		});
		expect(await runDurableObjectAlarm(stub)).toBe(true);
		await runInDurableObject(stub, (_instance, state) => {
			expect(state.storage.sql.exec("SELECT COUNT(*) AS count FROM _bastion_frames").one()).toEqual({ count: 0 });
			expect(state.storage.sql.exec("SELECT value FROM items").one()).toEqual({ value: "original" });
			expect(state.storage.sql.exec("SELECT revision FROM _bastion_revision").one()).toEqual({ revision: 1 });
		});
	});

	it("exports bounded keyset pages and rejects stale continuation requests", async () => {
		const stub = binding.getByName("export");
		const writes = [statement("CREATE TABLE teams (key TEXT PRIMARY KEY, doc TEXT)")];
		for (let i = 0; i < 300; i++) writes.push(statement("INSERT INTO teams VALUES (?, ?)", i.toString().padStart(3, "0"), "value"));
		await call(stub, "/commit", { id: "export-seed", revision: 0, statements: writes });
		const query = statement("SELECT key FROM teams ORDER BY key");
		const queryPage = await (await call(stub, "/query", { revision: 1, statement: query })).json() as { rows: string[][]; more: boolean };
		expect(queryPage.rows.length).toBe(256);
		expect(queryPage.more).toBe(true);
		const queryRest = await (await call(stub, "/query", { revision: 1, statement: query, offset: 256 })).json() as { rows: string[][]; more: boolean };
		expect(queryRest.rows.length).toBe(44);
		expect(queryRest.more).toBe(false);
		const first = await (await call(stub, "/export", { revision: 1, table: "teams" })).json() as { rows: string[][]; after: string; done: boolean };
		expect(first.rows.length).toBe(256);
		expect(first.after).toBe("255");
		const second = await (await call(stub, "/export", { revision: 1, table: "teams", after: first.after })).json() as { rows: string[][]; after: string };
		expect(second.rows.length).toBe(44);
		expect(await (await call(stub, "/export", { revision: 1, table: "teams", after: second.after })).json()).toMatchObject({ rows: [], done: true });
		await call(stub, "/commit", { id: "export-change", revision: 1, statements: [statement("DELETE FROM teams WHERE key = '299'")] });
		expect((await call(stub, "/export", { revision: 1, table: "teams", after: first.after })).status).toBe(409);
		expect((await call(stub, "/export", { revision: 2, table: "_bastion_frames" })).status).toBe(503);
	});
});
