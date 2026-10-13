import { Container, ContainerProxy, type OutboundHandler } from "@cloudflare/containers";
import { routeCatalog } from "./catalog-routing";
export { ContainerProxy };
export { ForgeCatalog } from "./catalog";

interface Env extends Cloudflare.Env {
	BASTION_ADMIN_SECRET: string;
	BASTION_BOOTSTRAP_ADMIN_EMAIL: string;
	CLERK_PUBLISHABLE_KEY?: string;
	CLERK_SECRET_KEY?: string;
	CLERK_AUTHORIZED_PARTIES?: string;
	CLERK_JWT_KEY?: string;
}

type StoredArtifacts = { remote: string; token: string; expiresAt: number };

const PRIVATE_CONFIG = "bastion:private-artifacts";
const PORT = 8080;

function constantTimeEqual(left: string, right: string): boolean {
	const a = new TextEncoder().encode(left);
	const b = new TextEncoder().encode(right);
	let difference = a.length ^ b.length;
	const length = Math.max(a.length, b.length);
	for (let index = 0; index < length; index++) {
		difference |= (a[index] ?? 0) ^ (b[index] ?? 0);
	}
	return difference === 0;
}

function json(body: unknown, status = 200): Response {
	return new Response(JSON.stringify(body), {
		status,
		headers: {
			"Content-Type": "application/json; charset=utf-8",
			"Cache-Control": "no-store",
			"X-Content-Type-Options": "nosniff",
		},
	});
}

export class ForgeContainer extends Container<Env> {
	static {
		this.outboundByHost = {
			"catalog.bastion.internal": (request, env, ctx) =>
				routeCatalog(request, env.CATALOG, ctx.containerId),
		} satisfies Record<string, OutboundHandler<Env>>;
	}
	defaultPort = PORT;
	requiredPorts = [PORT];
	sleepAfter = "10m";
	pingEndpoint = "localhost/healthz";
	entrypoint = [
		"/app/deployment/container-entrypoint.sh",
		"gunicorn",
		"--workers",
		"1",
		"--threads",
		"1",
		"--timeout",
		"660",
		"--bind",
		"0.0.0.0:8080",
		"cloudflare_runtime:build_app()",
	];
	private operationTail: Promise<void> = Promise.resolve();

	private serialize<T>(operation: () => Promise<T>): Promise<T> {
		const result = this.operationTail.then(operation, operation);
		this.operationTail = result.then(() => undefined, () => undefined);
		return result;
	}

	async configureArtifacts(value: StoredArtifacts): Promise<void> {
		return this.serialize(async () => {
			if (!value.remote.startsWith("https://") || !value.token || !Number.isFinite(value.expiresAt)) {
				throw new Error("invalid private Artifacts configuration");
			}
			const current = await this.ctx.storage.get<StoredArtifacts>(PRIVATE_CONFIG);
			if (current?.remote !== value.remote || current?.token !== value.token) {
				if (this.ctx.container?.running) await this.stop();
				await this.ctx.storage.put(PRIVATE_CONFIG, value);
			}
		});
	}

	async isProvisioned(): Promise<boolean> {
		return (await this.ctx.storage.get<StoredArtifacts>(PRIVATE_CONFIG)) !== undefined;
	}

	async refreshTokenBeforeStart(): Promise<void> {
		const current = await this.ctx.storage.get<StoredArtifacts>(PRIVATE_CONFIG);
		if (!current) throw new Error("private Artifacts configuration is missing");
		if (current.expiresAt > Date.now() + 5 * 60 * 1000) return;
		using repo = await this.env.ARTIFACTS.get(this.env.ARTIFACTS_REPO);
		const info = await repo.info();
		const token = await repo.createToken("write", 31_536_000);
		const updated = {
			remote: info.remote,
			token: token.plaintext,
			expiresAt: Date.parse(token.expiresAt),
		};
		if (!Number.isFinite(updated.expiresAt)) throw new Error("Artifacts token expiry is invalid");
		if (this.ctx.container?.running) await this.stop();
		await this.ctx.storage.put(PRIVATE_CONFIG, updated);
	}

	override async fetch(request: Request): Promise<Response> {
		return this.serialize(() => this.forward(request));
	}

	private async forward(request: Request): Promise<Response> {
		const privateConfig = await this.ctx.storage.get<StoredArtifacts>(PRIVATE_CONFIG);
		if (!privateConfig) {
			return json({ error: "not-provisioned" }, 503);
		}
		await this.refreshTokenBeforeStart();
		const activeConfig = (await this.ctx.storage.get<StoredArtifacts>(PRIVATE_CONFIG))!;
		const clerkValues = [
			this.env.CLERK_PUBLISHABLE_KEY,
			this.env.CLERK_SECRET_KEY,
			this.env.CLERK_AUTHORIZED_PARTIES,
		];
		if (clerkValues.some(Boolean) && !clerkValues.every(Boolean)) {
			throw new Error("incomplete Clerk configuration");
		}
		const clerkEnv =
			this.env.CLERK_PUBLISHABLE_KEY && this.env.CLERK_SECRET_KEY && this.env.CLERK_AUTHORIZED_PARTIES
				? {
						CLERK_PUBLISHABLE_KEY: this.env.CLERK_PUBLISHABLE_KEY,
						CLERK_SECRET_KEY: this.env.CLERK_SECRET_KEY,
						CLERK_AUTHORIZED_PARTIES: this.env.CLERK_AUTHORIZED_PARTIES,
						...(this.env.CLERK_JWT_KEY ? { CLERK_JWT_KEY: this.env.CLERK_JWT_KEY } : {}),
					}
				: {};
		await this.startAndWaitForPorts({
			ports: [PORT],
			cancellationOptions: {
				instanceGetTimeoutMS: 300_000,
				portReadyTimeoutMS: 600_000,
				waitInterval: 1_000,
			},
			startOptions: {
				// The Artifacts write credential is delivered only to the server
				// process. It is never copied to an inbound or outbound response.
				envVars: {
					BASTION_ARTIFACTS_REMOTE: activeConfig.remote,
					BASTION_ARTIFACTS_TOKEN: activeConfig.token,
					BASTION_BOOTSTRAP_ADMIN_EMAIL: this.env.BASTION_BOOTSTRAP_ADMIN_EMAIL,
					BASTION_ROOT: "/var/lib/bastion/host",
					BASTION_DATABASE: "durable_objects",
					BASTION_CATALOG_ENDPOINT: "http://catalog.bastion.internal",
					...clerkEnv,
				},
			},
		});
		return this.containerFetch(request, PORT);
	}
}

async function provision(env: Env): Promise<Response> {
	const repoName = env.ARTIFACTS_REPO;
	if (!repoName || !/^[a-z0-9][a-z0-9._-]{0,62}$/.test(repoName)) {
		return json({ error: "invalid-artifacts-repo" }, 500);
	}
	try {
		let exists = false;
		let cursor: string | undefined;
		do {
			const page = await env.ARTIFACTS.list({ limit: 100, cursor });
			exists ||= page.repos.some((repo) => repo.name === repoName);
			cursor = page.cursor ?? undefined;
		} while (!exists && cursor);
		if (!exists) {
			await env.ARTIFACTS.create(repoName, {
				description: "Private Bastion repository and control-plane storage",
				readOnly: false,
				setDefaultBranch: "main",
			});
		}
		using repo = await env.ARTIFACTS.get(repoName);
		const info = await repo.info();
		const token = await repo.createToken("write", 31_536_000);
		const created: StoredArtifacts = {
			remote: info.remote,
			token: token.plaintext,
			expiresAt: Date.parse(token.expiresAt),
		};
		if (!Number.isFinite(created.expiresAt)) throw new Error("Artifacts token expiry is invalid");
		const forge = env.FORGE.getByName("forge");
		await forge.configureArtifacts(created);
		return json({ provisioned: true, repository: repoName });
	} catch {
		// Do not return details that might include remote request material.
		return json({ error: "provision-failed" }, 502);
	}
}

export default {
	async fetch(request: Request, env: Env): Promise<Response> {
		const url = new URL(request.url);
		if (url.pathname === "/_admin/provision") {
			if (request.method !== "POST") {
				return json({ error: "method-not-allowed" }, 405);
			}
			const expected = env.BASTION_ADMIN_SECRET;
			const provided = request.headers.get("Authorization")?.replace(/^Bearer\s+/i, "") ?? "";
			if (!expected || !constantTimeEqual(provided, expected)) {
				return json({ error: "not-found" }, 404);
			}
			return provision(env);
		}
		const forge = env.FORGE.getByName("forge");
		if (!(await forge.isProvisioned())) {
			return json({ error: "not-provisioned" }, 503);
		}
		return forge.fetch(request);
	},
};
