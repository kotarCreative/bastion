// This entrypoint is for local tests only. It has no public production route.
export { ForgeCatalog } from "../src/catalog";

export default {
	fetch(request: Request, env: { CATALOG: DurableObjectNamespace }): Promise<Response> {
		const name = request.headers.get("X-Test-Catalog") ?? "test";
		return env.CATALOG.getByName(name).fetch(request);
	},
};
