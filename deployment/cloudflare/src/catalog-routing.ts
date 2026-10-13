/** Private Container outbound traffic only; public Worker routes never call this. */
export function routeCatalog<T extends Rpc.DurableObjectBranded | undefined>(request: Request, catalogs: DurableObjectNamespace<T>, hostIdentity: string): Promise<Response> {
	const url = new URL(request.url);
	const match = /^\/(host|repos\/[0-9a-f]{32})\/(status|query|commit|stage|discard|export)$/.exec(url.pathname);
	if (!match) return Promise.resolve(new Response("not found", { status: 404 }));
	url.pathname = "/" + match[2];
	return catalogs.getByName(hostIdentity + ":" + match[1]).fetch(new Request(url, request));
}
