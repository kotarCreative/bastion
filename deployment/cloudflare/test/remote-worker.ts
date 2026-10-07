// Isolated live-test Worker. Never bind this entrypoint to a production catalog.
export { ForgeCatalog } from "../src/catalog";

export default {
	fetch(request: Request, env: { CATALOG: DurableObjectNamespace; BASTION_SMOKE_SECRET: string }): Response | Promise<Response> {
		const expected = new TextEncoder().encode(`Bearer ${env.BASTION_SMOKE_SECRET}`);
		const actual = new TextEncoder().encode(request.headers.get("Authorization") ?? "");
		let difference = expected.length ^ actual.length;
		for (let i = 0; i < Math.max(expected.length, actual.length); i++) difference |= (expected[i] ?? 0) ^ (actual[i] ?? 0);
		if (!env.BASTION_SMOKE_SECRET || difference || request.method !== "POST") return new Response(null, { status: 404 });
		return env.CATALOG.getByName("smoke-v1").fetch(request);
	},
};
