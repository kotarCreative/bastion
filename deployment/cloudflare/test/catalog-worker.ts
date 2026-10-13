// This entrypoint is for local tests only. It has no public production route.
export { ForgeCatalog } from "../src/catalog";
import { routeCatalog } from "../src/catalog-routing";

export default {
	fetch(request: Request, env: { CATALOG: DurableObjectNamespace }): Promise<Response> {
		const name = request.headers.get("X-Test-Catalog") ?? "test";
		if (/^\/(?:host|repos)\//.test(new URL(request.url).pathname)) return routeCatalog(request, env.CATALOG, name);
		return env.CATALOG.getByName(name).fetch(request);
	},
};
