/* Dependency-free behavioral checks for the browser application's demo flows.
 * Run with: node tests/test_spa.js
 */
"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

class Node {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this.dataset = {};
    this.hidden = false;
    this.value = "";
    this.textContent = "";
  }
  get firstChild() { return this.children[0]; }
  get lastChild() { return this.children[this.children.length - 1]; }
  appendChild(child) {
    this.children.push(child);
    if (this.tagName === "select" && this.children.length === 1) this.value = child.value;
    return child;
  }
  prepend(child) { this.children.unshift(child); }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  fire(name) { this.listeners[name]({ preventDefault() {}, currentTarget: this, target: this }); }
  scrollIntoView() {}
}

function descendants(node) {
  return [node].concat(node.children.flatMap(descendants));
}
function text(node) {
  return descendants(node).map(item => item.textContent).join(" ");
}
const nodes = new Map();
const shell = fs.readFileSync("templates/spa/index.html", "utf8");
for (const match of shell.matchAll(/id="([^"]+)"/g)) {
  const node = new Node("div");
  node.id = match[1];
  nodes.set(node.id, node);
}
const panels = [...nodes.values()].filter(node => node.id.startsWith("panel-"));
const tabs = [...nodes.values()].filter(node => node.id.startsWith("tab-"));
tabs.forEach(node => { node.dataset.panel = node.id.slice(4); });
const envelope = {
  principal: { id: "owner", kind: "user" }, view_id: "owner", team_ids: [], role_ids: [],
  descriptor: { projected_ref: "refs/heads/main", generation: 1, projection_epoch: 1,
    capabilities: { read: [{ prefix_display: "/" }] }, restrictions: { read: [] } },
};
const entry = (path, kind) => ({ path_display: path, path_b64: Buffer.from(path).toString("base64url"), kind, size: kind === "blob" ? 12 : null });
const calls = [];
const context = {
  document: {
    getElementById: id => nodes.get(id),
    createElement: tag => new Node(tag),
    querySelectorAll: selector => selector === ".tab" ? tabs : panels,
  },
  window: { location: { protocol: "https:", host: "demo.test" }, scrollTo() {} },
  TextEncoder, btoa: value => Buffer.from(value, "binary").toString("base64"), Intl, Date,
  fetch: async (path, options) => {
    calls.push({ path, options });
    let body;
    let status = 200;
    if (path === "/api/v1/auth/config") body = { enabled: false };
    else if (path === "/api/v1/session") body = envelope;
    else if (path === "/api/v1/permissions") body = { targets: [] };
    else if (path === "/api/v1/invitations") {
      const payload = JSON.parse(options.body);
      body = { view_id: payload.principal_id, access: payload.access };
      status = 201;
    } else if (path.startsWith("/api/v1/tree?")) {
      const params = new URLSearchParams(path.split("?")[1]);
      const dir = Buffer.from(params.get("path_b64") || "", "base64url").toString();
      const cursor = params.get("cursor");
      body = { projected_tip: "1234567890", entries: [], next_cursor: null };
      if (!dir && !cursor) { body.entries = [entry("src", "tree")]; body.next_cursor = "page2"; }
      else if (!dir) body.entries = [entry("LICENSE", "blob")];
      else if (dir === "src") body.entries = [entry("src/public", "tree")];
      else if (dir === "src/public") body.entries = [entry("src/public/app.js", "blob")];
    } else throw new Error("Unexpected request: " + path);
    return { status, json: async () => body };
  },
};
vm.runInNewContext(fs.readFileSync("templates/spa/app.js", "utf8"), context);
const settle = async () => { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); };

(async () => {
  await settle();
  nodes.get("token").value = "owner-token";
  nodes.get("login-form").fire("submit");
  await settle();
  const tree = nodes.get("tree-list");
  assert.match(text(tree), /app\.js/);
  assert.match(text(tree), /LICENSE/);
  assert.equal(nodes.get("entry-count").textContent, "4 visible items");
  assert.ok(calls.some(call => call.path.includes("cursor=page2")));
  const folder = descendants(tree).find(node => node.textContent === "src/");
  folder.fire("click");
  assert.equal(folder.attributes["aria-expanded"], "false");
  folder.fire("click");
  assert.equal(folder.attributes["aria-expanded"], "true");
  nodes.get("tree-mode").fire("click");
  await settle();
  assert.doesNotMatch(text(tree), /app\.js/);
  assert.equal(nodes.get("tree-more").hidden, false);

  const permissions = nodes.get("panel-permissions");
  const find = id => descendants(permissions).find(node => node.id === id);
  find("invite-id").value = "reader";
  find("invite-email").value = "reader@example.test";
  find("invite-access").value = "read-only";
  find("invite-paths").value = "src/public\nLICENSE";
  descendants(permissions).find(node => node.tagName === "form").fire("submit");
  await settle();
  const invitation = calls.find(call => call.path === "/api/v1/invitations");
  assert.deepEqual(JSON.parse(invitation.options.body), {
    principal_id: "reader", email: "reader@example.test", access: "read-only", readable_paths: ["src/public", "LICENSE"],
  });
  assert.match(text(permissions), /Invited reader with read-only access/);
  assert.equal(invitation.options.headers.Authorization, "Bearer owner-token");
  console.log("SPA checks passed: nested tree, pagination, collapse, folder mode, scoped invitation.");
})().catch(error => { console.error(error); process.exitCode = 1; });
