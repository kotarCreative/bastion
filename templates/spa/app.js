/* Bastion browser application.
 *
 * The access token lives in one module-local variable (page memory only). It
 * is never written to a cookie, URL, DOM node, or telemetry, and is cleared
 * from the input immediately after use. Untrusted server data is rendered
 * through textContent and attribute setters only.
 */
"use strict";

(function () {
  var credential = null;
  var session = null;
  var clerk = null;
  var clerkHadSession = false;
  var clerkExchangePending = false;
  var clerkUserButtonMounted = false;
  var historyRendered = false;
  var treeState = { stack: [], entries: [], cursor: null, loading: false };
  var fullTree = true;
  var treeRun = 0;
  var invitationRun = 0;

  var ELEMENTS = {
    homepage: document.getElementById("homepage"),
    homeNav: document.getElementById("home-nav"),
    sessionNav: document.getElementById("session-nav"),
    identity: document.getElementById("identity"),
    clerkUserButton: document.getElementById("clerk-user-button"),
    manageAccount: document.getElementById("manage-account"),
    logout: document.getElementById("logout"),
    login: document.getElementById("login"),
    loginForm: document.getElementById("login-form"),
    tokenInput: document.getElementById("token"),
    loginError: document.getElementById("login-error"),
    clerkAuth: document.getElementById("clerk-auth"),
    clerkSignIn: document.getElementById("clerk-sign-in"),
    clerkViewForm: document.getElementById("clerk-view-form"),
    clerkView: document.getElementById("clerk-view"),
    sessionNote: document.getElementById("session-note"),
    workspace: document.getElementById("workspace"),
    repoOwner: document.getElementById("repo-owner"),
    repoName: document.getElementById("repo-name"),
    branchName: document.getElementById("branch-name"),
    tipLabel: document.getElementById("tip-label"),
    treeList: document.getElementById("tree-list"),
    treeMore: document.getElementById("tree-more"),
    breadcrumb: document.getElementById("repo-breadcrumb"),
    entryCount: document.getElementById("entry-count"),
    fileViewer: document.getElementById("file-viewer"),
    readmePanel: document.getElementById("readme-panel"),
    readmeName: document.getElementById("readme-name"),
    readmeContent: document.getElementById("readme-content"),
    cloneDialog: document.getElementById("clone-dialog"),
    cloneUrl: document.getElementById("clone-url"),
  };

  function visibleText(value) {
    return String(value == null ? "" : value);
  }

  function clearChildren(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = visibleText(text);
    return node;
  }

  function textButton(className, text) {
    var node = element("button", className, text);
    node.type = "button";
    return node;
  }

  function request(path, options) {
    options = options || {};
    var headers = Object.assign({}, options.headers || {});
    var requestCredential = credential && !options.skipCredential ? credential : null;
    if (credential && !options.skipCredential) {
      headers.Authorization = "Bearer " + credential;
    }
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    }).then(function (response) {
      if (requestCredential && requestCredential !== credential) throw new Error("stale-session");
      if (response.status === 401 && !options.optionalAuth) {
        clearBastionSession();
        throw new Error("auth-required");
      }
      return response.json().then(function (body) {
        if (requestCredential && requestCredential !== credential) throw new Error("stale-session");
        return { status: response.status, body: body };
      });
    });
  }

  function formatBytes(value) {
    if (value == null) return "";
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(value < 10240 ? 1 : 0) + " KB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
  }

  function shortDate(timestamp) {
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "numeric",
    }).format(new Date(timestamp * 1000));
  }

  function initials(value) {
    var words = visibleText(value).trim().split(/\s+/).filter(Boolean);
    return (words[0] ? words[0][0] : "B").toUpperCase();
  }

  function encodePath(text) {
    var raw = new TextEncoder().encode(text);
    var binary = "";
    raw.forEach(function (byte) { binary += String.fromCharCode(byte); });
    return btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  }

  function showPanel(name) {
    document.querySelectorAll(".workspace-panel").forEach(function (panel) {
      panel.hidden = panel.id !== "panel-" + name;
    });
    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.setAttribute("aria-selected", tab.dataset.panel === name ? "true" : "false");
    });
    if (name === "history" && !historyRendered) renderHistory();
  }

  function clearBastionSession() {
    invitationRun += 1;
    credential = null;
    session = null;
    historyRendered = false;
    treeRun += 1;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.login.hidden = false;
    ELEMENTS.homepage.hidden = false;
    ELEMENTS.homeNav.hidden = false;
    ELEMENTS.sessionNav.hidden = true;
    ELEMENTS.workspace.hidden = true;
    ELEMENTS.identity.hidden = true;
    ELEMENTS.identity.textContent = "";
    ELEMENTS.manageAccount.hidden = true;
    ELEMENTS.logout.hidden = true;
    ELEMENTS.clerkUserButton.hidden = true;
    document.getElementById("tab-permissions").hidden = true;
    ["panel-history", "panel-decision", "panel-permissions", "panel-profile"].forEach(function (id) {
      clearChildren(document.getElementById(id));
    });
    clearChildren(ELEMENTS.treeList);
    clearChildren(ELEMENTS.breadcrumb);
    clearChildren(ELEMENTS.fileViewer);
    clearChildren(ELEMENTS.readmeContent);
    ELEMENTS.fileViewer.hidden = true;
    ELEMENTS.readmePanel.hidden = true;
  }

  function prefixList(prefixes, emptyText) {
    var list = element("ul", "path-list");
    if (!prefixes || prefixes.length === 0) {
      list.appendChild(element("li", null, emptyText || "None"));
      return list;
    }
    prefixes.forEach(function (prefix) {
      list.appendChild(element("li", null, prefix.prefix_display || "/"));
    });
    return list;
  }

  function definitionList(rows) {
    var definition = element("dl", "definition-list");
    rows.forEach(function (row) {
      definition.appendChild(element("dt", null, row[0]));
      definition.appendChild(element("dd", null, row[1]));
    });
    return definition;
  }

  function currentDirectory() {
    return treeState.stack.length ? treeState.stack[treeState.stack.length - 1] : { path_b64: "", path_display: "" };
  }

  function renderBreadcrumb() {
    clearChildren(ELEMENTS.breadcrumb);
    var root = textButton("breadcrumb-button", session.view_id);
    root.addEventListener("click", function () {
      treeState.stack = [];
      loadDirectory(true);
    });
    ELEMENTS.breadcrumb.appendChild(root);
    treeState.stack.forEach(function (entry, index) {
      ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-separator", "/"));
      var name = entry.path_display.split("/").pop();
      if (index === treeState.stack.length - 1) {
        ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-current", name));
      } else {
        var crumb = textButton("breadcrumb-button", name);
        crumb.addEventListener("click", function () {
          treeState.stack = treeState.stack.slice(0, index + 1);
          loadDirectory(true);
        });
        ELEMENTS.breadcrumb.appendChild(crumb);
      }
    });
  }

  function renderTreeRows() {
    clearChildren(ELEMENTS.treeList);
    if (treeState.entries.length === 0) {
      ELEMENTS.treeList.appendChild(element("div", "tree-empty", "This directory has no visible files."));
      return;
    }
    treeState.entries.forEach(function (entry) {
      var row = element("div", "tree-row");
      var iconSlot = element("span", "entry-icon");
      iconSlot.appendChild(element("span", entry.kind === "tree" ? "file-glyph folder-glyph" : "file-glyph"));
      var name = entry.path_display.split("/").pop();
      var nameButton = textButton("entry-name", name + (entry.kind === "tree" ? "/" : ""));
      nameButton.addEventListener("click", function () {
        if (entry.kind === "tree") {
          treeState.stack.push(entry);
          loadDirectory(true);
        } else {
          openFile(entry);
        }
      });
      row.appendChild(iconSlot);
      row.appendChild(nameButton);
      row.appendChild(element("span", "entry-kind", entry.kind === "tree" ? "Directory" : "File"));
      row.appendChild(element("span", "entry-size", formatBytes(entry.size)));
      ELEMENTS.treeList.appendChild(row);
    });
  }

  function readmeEntry(entries) {
    var names = ["readme.md", "readme.markdown", "readme.txt", "readme"];
    for (var index = 0; index < names.length; index += 1) {
      var match = entries.find(function (entry) {
        return entry.kind === "blob" && entry.path_display.split("/").pop().toLowerCase() === names[index];
      });
      if (match) return match;
    }
    return null;
  }

  function loadDirectory(reset) {
    if (fullTree) { loadFullTree(); return; }
    if (treeState.loading) return;
    if (reset) {
      treeState.entries = [];
      treeState.cursor = null;
      ELEMENTS.fileViewer.hidden = true;
      ELEMENTS.readmePanel.hidden = true;
      clearChildren(ELEMENTS.treeList);
      ELEMENTS.treeList.appendChild(element("div", "tree-loading", "Loading visible files…"));
    }
    treeState.loading = true;
    var run = ++treeRun;
    var token = credential;
    var directory = currentDirectory();
    var query = "limit=50";
    if (directory.path_b64) query += "&path_b64=" + encodeURIComponent(directory.path_b64);
    if (treeState.cursor) query += "&cursor=" + encodeURIComponent(treeState.cursor);
    request("/api/v1/tree?" + query).then(function (result) {
      if (run !== treeRun || token !== credential) return;
      if (result.status !== 200) throw new Error("tree-unavailable");
      treeState.entries = treeState.entries.concat(result.body.entries);
      treeState.cursor = result.body.next_cursor;
      ELEMENTS.tipLabel.textContent = result.body.projected_tip.slice(0, 8);
      ELEMENTS.entryCount.textContent = treeState.entries.length + (treeState.entries.length === 1 ? " item" : " items");
      ELEMENTS.treeMore.hidden = !treeState.cursor;
      renderBreadcrumb();
      renderTreeRows();
      var readme = readmeEntry(treeState.entries);
      if (readme) loadReadme(readme);
    }).catch(function (error) {
      if (run !== treeRun || token !== credential) return;
      if (error && error.message === "auth-required") return;
      clearChildren(ELEMENTS.treeList);
      ELEMENTS.treeList.appendChild(element("div", "tree-empty", "This repository view is temporarily unavailable."));
      ELEMENTS.treeMore.hidden = true;
    }).then(function () {
      if (run === treeRun && token === credential) treeState.loading = false;
    });
  }

  function loadFullTree() {
    var run = ++treeRun;
    var token = credential;
    var tip = null;
    var count = 0;
    clearChildren(ELEMENTS.treeList);
    clearChildren(ELEMENTS.breadcrumb);
    ELEMENTS.breadcrumb.appendChild(element("span", "breadcrumb-current", session.view_id + " / Full file structure"));
    ELEMENTS.treeMore.hidden = true;
    ELEMENTS.fileViewer.hidden = true;
    ELEMENTS.readmePanel.hidden = true;
    ELEMENTS.entryCount.textContent = "Loading…";
    var status = element("div", "tree-loading", "Loading your visible file structure…");
    ELEMENTS.treeList.appendChild(status);
    var container = element("div", "full-tree");
    ELEMENTS.treeList.appendChild(container);

    function active() { return run === treeRun && token === credential; }
    function directory(path, parent, cursor) {
      if (!active()) return Promise.resolve();
      var query = "limit=100";
      if (path) query += "&path_b64=" + encodeURIComponent(path);
      if (cursor) query += "&cursor=" + encodeURIComponent(cursor);
      return request("/api/v1/tree?" + query).then(function (result) {
        if (!active()) return;
        if (result.status !== 200) throw new Error("tree-unavailable");
        if (tip && tip !== result.body.projected_tip) throw new Error("tree-changed");
        tip = result.body.projected_tip;
        ELEMENTS.tipLabel.textContent = tip.slice(0, 8);
        var chain = Promise.resolve();
        result.body.entries.forEach(function (entry) {
          chain = chain.then(function () {
            if (!active()) return;
            var row = element("div", "tree-row");
            var icon = element("span", "entry-icon");
            icon.appendChild(element("span", entry.kind === "tree" ? "file-glyph folder-glyph" : "file-glyph"));
            var name = entry.path_display.split("/").pop();
            var button = textButton("entry-name", name + (entry.kind === "tree" ? "/" : ""));
            row.appendChild(icon);
            row.appendChild(button);
            row.appendChild(element("span", "entry-kind", entry.kind === "tree" ? "Directory" : "File"));
            row.appendChild(element("span", "entry-size", formatBytes(entry.size)));
            parent.appendChild(row);
            count += 1;
            ELEMENTS.entryCount.textContent = count + " visible items";
            if (entry.kind === "tree") {
              var children = element("div", "tree-children");
              parent.appendChild(children);
              button.setAttribute("aria-expanded", "true");
              button.addEventListener("click", function () {
                children.hidden = !children.hidden;
                button.setAttribute("aria-expanded", String(!children.hidden));
              });
              return directory(entry.path_b64, children, null);
            }
            button.addEventListener("click", function () { openFile(entry); });
            if (!path && readmeEntry([entry])) loadReadme(entry);
          });
        });
        return chain.then(function () {
          if (result.body.next_cursor) return directory(path, parent, result.body.next_cursor);
        });
      });
    }
    directory("", container, null).then(function () {
      if (!active()) return;
      status.hidden = count > 0;
      if (!count) status.textContent = "This view has no visible files.";
    }).catch(function (error) {
      if (!active()) return;
      clearChildren(container);
      ELEMENTS.entryCount.textContent = "";
      status.textContent = error.message === "tree-changed" ? "The repository changed. Refresh the view to reload the structure." : "The file structure is unavailable. Refresh to try again.";
    });
  }

  function blobRequest(entry) {
    return request("/api/v1/blob?path_b64=" + encodeURIComponent(entry.path_b64));
  }

  function openFile(entry) {
    clearChildren(ELEMENTS.fileViewer);
    ELEMENTS.fileViewer.hidden = false;
    var header = element("header");
    header.appendChild(element("span", "document-icon"));
    header.appendChild(element("strong", null, entry.path_display));
    header.appendChild(element("span", "file-meta", formatBytes(entry.size)));
    var close = textButton("icon-button", "×");
    close.setAttribute("aria-label", "Close file preview");
    close.addEventListener("click", function () { ELEMENTS.fileViewer.hidden = true; });
    header.appendChild(close);
    ELEMENTS.fileViewer.appendChild(header);
    ELEMENTS.fileViewer.appendChild(element("div", "tree-loading", "Loading file…"));
    blobRequest(entry).then(function (result) {
      while (ELEMENTS.fileViewer.children.length > 1) ELEMENTS.fileViewer.removeChild(ELEMENTS.fileViewer.lastChild);
      if (result.status !== 200) {
        ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "This file cannot be previewed."));
      } else if (result.body.binary) {
        ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "Binary files are available when you clone this repository, but are not shown in the browser."));
      } else {
        ELEMENTS.fileViewer.appendChild(element("pre", "code-preview", result.body.text));
      }
      ELEMENTS.fileViewer.scrollIntoView({ behavior: "smooth", block: "start" });
    }).catch(function () {
      while (ELEMENTS.fileViewer.children.length > 1) ELEMENTS.fileViewer.removeChild(ELEMENTS.fileViewer.lastChild);
      ELEMENTS.fileViewer.appendChild(element("div", "binary-message", "This file cannot be previewed."));
    });
  }

  function appendMarkdown(container, text) {
    var lines = visibleText(text).replace(/\r\n/g, "\n").split("\n");
    var index = 0;
    while (index < lines.length) {
      var line = lines[index];
      if (line.indexOf("```") === 0) {
        var code = [];
        index += 1;
        while (index < lines.length && lines[index].indexOf("```") !== 0) {
          code.push(lines[index]);
          index += 1;
        }
        container.appendChild(element("pre", null, code.join("\n")));
      } else if (/^###\s+/.test(line)) {
        container.appendChild(element("h3", null, line.replace(/^###\s+/, "")));
      } else if (/^##\s+/.test(line)) {
        container.appendChild(element("h2", null, line.replace(/^##\s+/, "")));
      } else if (/^#\s+/.test(line)) {
        container.appendChild(element("h1", null, line.replace(/^#\s+/, "")));
      } else if (/^[-*]\s+/.test(line)) {
        var list = element("ul");
        while (index < lines.length && /^[-*]\s+/.test(lines[index])) {
          list.appendChild(element("li", null, lines[index].replace(/^[-*]\s+/, "")));
          index += 1;
        }
        container.appendChild(list);
        continue;
      } else if (line.trim()) {
        var paragraph = [line];
        while (index + 1 < lines.length && lines[index + 1].trim() && !/^(#{1,3}|[-*]\s|```)/.test(lines[index + 1])) {
          index += 1;
          paragraph.push(lines[index]);
        }
        container.appendChild(element("p", null, paragraph.join("\n")));
      }
      index += 1;
    }
  }

  function loadReadme(entry) {
    ELEMENTS.readmeName.textContent = entry.path_display.split("/").pop();
    clearChildren(ELEMENTS.readmeContent);
    ELEMENTS.readmeContent.appendChild(element("p", "hint", "Loading documentation…"));
    ELEMENTS.readmePanel.hidden = false;
    blobRequest(entry).then(function (result) {
      if (result.status !== 200 || result.body.binary) {
        ELEMENTS.readmePanel.hidden = true;
        return;
      }
      clearChildren(ELEMENTS.readmeContent);
      appendMarkdown(ELEMENTS.readmeContent, result.body.text);
    }).catch(function () { ELEMENTS.readmePanel.hidden = true; });
  }

  function renderHistory() {
    historyRendered = true;
    var panel = document.getElementById("panel-history");
    clearChildren(panel);
    var shell = element("div", "history-shell");
    var heading = element("div", "panel-heading");
    heading.appendChild(element("h2", null, "Visible history"));
    heading.appendChild(element("p", null, "Commits are drawn from your projected repository and contain only history visible to this view."));
    var list = element("div");
    var more = textButton("load-more", "Load more commits");
    var cursor = null;
    shell.appendChild(heading);
    shell.appendChild(list);
    shell.appendChild(more);
    panel.appendChild(shell);

    function load() {
      more.disabled = true;
      var query = "limit=25" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      request("/api/v1/history?" + query).then(function (result) {
        if (result.status !== 200) throw new Error("history-unavailable");
        result.body.commits.forEach(function (commit) {
          var row = element("div", "commit");
          row.appendChild(element("span", "commit-avatar", initials(commit.author_name_display)));
          var copy = element("div");
          copy.appendChild(element("p", "commit-message", commit.message_display.split("\n")[0] || "Untitled commit"));
          copy.appendChild(element("span", "commit-meta", commit.author_name_display + " committed on " + shortDate(commit.committed_at)));
          row.appendChild(copy);
          row.appendChild(element("code", null, commit.oid.slice(0, 7)));
          list.appendChild(row);
        });
        cursor = result.body.next_cursor;
        more.hidden = !cursor;
        more.disabled = false;
        if (!list.firstChild) list.appendChild(element("div", "tree-empty", "No visible commits."));
      }).catch(function () {
        more.hidden = true;
        list.appendChild(element("div", "tree-empty", "History is temporarily unavailable."));
      });
    }
    more.addEventListener("click", load);
    load();
  }

  function renderDecision() {
    var panel = document.getElementById("panel-decision");
    clearChildren(panel);
    var card = element("div", "inspector-card");
    card.appendChild(element("h2", null, "Access inspector"));
    card.appendChild(element("p", "hint", "Evaluate the active policy for a repository-relative path. This explains permission decisions without exposing hidden content."));
    var form = element("form");
    form.setAttribute("autocomplete", "off");
    var pathLabel = element("label", null, "Path");
    pathLabel.setAttribute("for", "decision-path");
    var path = element("input");
    path.id = "decision-path";
    path.placeholder = "src/service/handler.py";
    var capabilityLabel = element("label", null, "Capability");
    capabilityLabel.setAttribute("for", "decision-capability");
    var capability = element("select");
    capability.id = "decision-capability";
    ["read", "create", "modify", "delete", "mode", "symlink", "branch", "merge", "admin", "tag"].forEach(function (name) {
      var option = element("option", null, name);
      option.value = name;
      capability.appendChild(option);
    });
    var submit = element("button", null, "Evaluate access");
    submit.type = "submit";
    var outcome = element("div");
    form.appendChild(pathLabel);
    form.appendChild(path);
    form.appendChild(capabilityLabel);
    form.appendChild(capability);
    form.appendChild(submit);
    card.appendChild(form);
    card.appendChild(outcome);
    panel.appendChild(card);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clearChildren(outcome);
      var normalized = path.value.replace(/^\/+/, "");
      request("/api/v1/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: capability.value, path_b64: encodePath(normalized) }),
      }).then(function (result) {
        if (result.status !== 200) throw new Error("decision-failed");
        outcome.appendChild(element("p", result.body.allowed ? "verdict-allow" : "verdict-deny", result.body.allowed ? "Allowed" : "Denied"));
        outcome.appendChild(definitionList([
          ["Longest matching grant", result.body.longest_allow ? result.body.longest_allow.prefix_display || "/" : "No match"],
          ["Longest matching restriction", result.body.longest_deny ? result.body.longest_deny.prefix_display || "/" : "No match"],
        ]));
      }).catch(function () {
        outcome.appendChild(element("p", "error", "The decision could not be evaluated."));
      });
    });
  }

  function renderPermissions(initialBody) {
    var invitationVersion = ++invitationRun;
    var panel = document.getElementById("panel-permissions");
    clearChildren(panel);
    var card = element("div", "permission-card");
    card.appendChild(element("h2", null, "Repository permissions"));
    card.appendChild(element("p", "hint", "Make a file or directory private for a collaborator, or restore inherited visibility. Changes publish a new projection epoch."));
    var content = element("div");
    card.appendChild(content);
    panel.appendChild(card);

    function populate(body) {
      if (invitationVersion !== invitationRun) return;
      renderInvitation(card, body.account_picker_enabled, invitationVersion);
      if (!body.targets.length) {
        content.appendChild(element("p", "hint", "This repository has no other collaborators."));
        return;
      }
      var form = element("form");
      var targetLabel = element("label", null, "Collaborator view");
      var target = element("select");
      targetLabel.setAttribute("for", "permission-target");
      target.id = "permission-target";
      body.targets.forEach(function (item) {
        var option = element("option", null, item.principal_id + " / " + item.view_id + (item.can_modify ? " · Contributor" : " · Read-only"));
        option.value = item.view_id;
        target.appendChild(option);
      });
      var pathLabel = element("label", null, "File or directory path");
      var path = element("input");
      pathLabel.setAttribute("for", "permission-path");
      path.id = "permission-path";
      path.placeholder = "docs/private or secrets.txt";
      path.required = true;
      var actionLabel = element("label", null, "Visibility");
      var action = element("select");
      actionLabel.setAttribute("for", "permission-action");
      action.id = "permission-action";
      [["private", "Private for this collaborator"], ["visible", "Restore inherited visibility"]].forEach(function (item) {
        var option = element("option", null, item[1]);
        option.value = item[0];
        action.appendChild(option);
      });
      var submit = element("button", null, "Update permissions");
      submit.type = "submit";
      var outcome = element("p");
      var current = element("div");

      function showCurrent() {
        clearChildren(current);
        var selected = body.targets.find(function (item) { return item.view_id === target.value; });
        current.appendChild(element("h3", null, "Granted read access"));
        current.appendChild(prefixList(selected ? selected.readable_paths : [], "No readable paths"));
        current.appendChild(element("h3", null, "Explicitly private paths"));
        current.appendChild(prefixList(selected ? selected.private_paths : [], "No UI-managed private paths"));
      }
      target.addEventListener("change", showCurrent);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var normalized = path.value.replace(/^\/+|\/+$/g, "");
        if (!normalized) {
          outcome.className = "error";
          outcome.textContent = "Enter a file or directory path.";
          return;
        }
        outcome.className = "hint";
        outcome.textContent = "Publishing permission change…";
        request("/api/v1/permissions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ view_id: target.value, path_b64: encodePath(normalized), private: action.value === "private" }),
        }).then(function (result) {
          if (result.status !== 200) throw new Error("permission-failed");
          outcome.className = "ok";
          outcome.textContent = "Permissions updated. A new projection is active.";
          renderPermissions();
        }).catch(function () {
          outcome.className = "error";
          outcome.textContent = "The permission change could not be applied.";
        });
      });
      [targetLabel, target, pathLabel, path, actionLabel, action, submit, outcome].forEach(function (node) { form.appendChild(node); });
      content.appendChild(form);
      content.appendChild(current);
      showCurrent();
    }

    if (initialBody) populate(initialBody);
    else request("/api/v1/permissions").then(function (result) {
      if (result.status !== 200) throw new Error("permissions-unavailable");
      populate(result.body);
    }).catch(function () { content.appendChild(element("p", "error", "Permissions are unavailable.")); });
  }

  function renderInvitation(card, pickerEnabled, version) {
    var section = element("section", "invitation-section");
    section.appendChild(element("h2", null, "Invite a collaborator"));
    section.appendChild(element("p", "hint", "Select an existing Clerk account or invite someone by email, then set their permissions. Share this site's URL with them; no email is sent."));
    var form = element("form");
    var id = element("input");
    id.id = "invite-id";
    id.required = true;
    id.pattern = "[A-Za-z0-9][A-Za-z0-9._-]{0,63}";
    id.maxLength = 64;
    id.placeholder = "demo-collaborator";
    var email = element("input");
    email.id = "invite-email";
    email.type = "email";
    email.required = true;
    var selectedAccount = null;
    var source = element("select");
    source.id = "invite-source";
    if (pickerEnabled) {
      var existingOption = element("option", null, "Select an existing Clerk account");
      existingOption.value = "account";
      source.appendChild(existingOption);
    }
    var emailOption = element("option", null, "Invite by email");
    emailOption.value = "email";
    source.appendChild(emailOption);
    var sourceLabel = element("label", null, "Invite method");
    sourceLabel.setAttribute("for", source.id);
    form.appendChild(sourceLabel);
    form.appendChild(source);
    var picker = element("div", "account-picker");
    var searchLabel = element("label", null, "Find a Clerk account");
    searchLabel.setAttribute("for", "invite-search");
    var search = element("input");
    search.id = "invite-search";
    search.type = "search";
    search.maxLength = 100;
    search.placeholder = "Search by name or email";
    search.setAttribute("autocomplete", "off");
    var searchStatus = element("p", "hint", "Enter at least 2 characters to search.");
    searchStatus.setAttribute("role", "status");
    var results = element("div", "account-results");
    var more = textButton("load-more", "Load more accounts");
    more.hidden = true;
    var selection = element("p", "account-selection");
    selection.setAttribute("role", "status");
    [searchLabel, search, searchStatus, results, more, selection].forEach(function (node) { picker.appendChild(node); });
    form.appendChild(picker);
    var searchVersion = 0;
    var timer = null;
    var nextOffset = null;
    var token = credential;

    function active(run) {
      return version === invitationRun && token === credential && run === searchVersion;
    }
    function searchAccounts(offset, run) {
      if (!active(run)) return;
      var query = search.value.trim();
      searchStatus.textContent = "Searching Clerk accounts…";
      more.disabled = true;
      request("/api/v1/accounts?query=" + encodeURIComponent(query) + "&offset=" + offset).then(function (result) {
        if (!active(run)) return;
        if (result.status !== 200) throw new Error("search-unavailable");
        result.body.accounts.forEach(function (account) {
          var button = textButton("account-option", account.name + " · " + account.email + (account.already_invited ? " · Already invited" : ""));
          button.disabled = account.already_invited;
          button.addEventListener("click", function () {
            selectedAccount = account;
            email.value = account.email;
            selection.textContent = "Selected: " + account.name + " · " + account.email;
            if (!id.value) id.value = account.email.split("@")[0].replace(/[^A-Za-z0-9._-]/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 64);
          });
          results.appendChild(button);
        });
        nextOffset = result.body.next_offset;
        more.hidden = nextOffset === null;
        searchStatus.textContent = results.children.length ? "Select an account below. Only active accounts with verified email are shown." : "No eligible accounts on this page. Try another search or invite by email.";
      }).catch(function () {
        if (!active(run)) return;
        searchStatus.textContent = "Account search is unavailable. Try again or choose Invite by email.";
        more.hidden = true;
      }).then(function () { if (active(run)) more.disabled = false; });
    }
    search.addEventListener("input", function () {
      var run = ++searchVersion;
      if (timer) window.clearTimeout(timer);
      clearChildren(results);
      more.hidden = true;
      if (search.value.trim().length < 2) {
        searchStatus.textContent = "Enter at least 2 characters to search.";
        return;
      }
      timer = window.setTimeout(function () { searchAccounts(0, run); }, 300);
    });
    more.addEventListener("click", function () {
      if (nextOffset !== null) searchAccounts(nextOffset, ++searchVersion);
    });
    function changeSource() {
      searchVersion += 1;
      if (timer) window.clearTimeout(timer);
      selectedAccount = null;
      selection.textContent = "";
      email.value = "";
      picker.hidden = source.value !== "account";
      email.readOnly = source.value === "account";
      email.required = source.value !== "account";
      clearChildren(results);
      more.hidden = true;
      search.value = "";
      searchStatus.textContent = "Enter at least 2 characters to search.";
    }
    source.addEventListener("change", changeSource);
    changeSource();
    var access = element("select");
    access.id = "invite-access";
    [["read-only", "Read-only — browse files and history"], ["contributor", "Contributor — read, create, edit, and delete allowed files"]].forEach(function (item) {
      var option = element("option", null, item[1]);
      option.value = item[0];
      access.appendChild(option);
    });
    var paths = element("textarea");
    paths.id = "invite-paths";
    paths.required = true;
    paths.rows = 4;
    paths.placeholder = "docs\nsrc/public\nREADME.md";
    [["Collaborator identifier", id], ["Verified email address", email], ["Permission level", access], ["Allowed files or folders (one per line; / for the entire repo)", paths]].forEach(function (item) {
      var label = element("label", null, item[0]);
      label.setAttribute("for", item[1].id);
      form.appendChild(label);
      form.appendChild(item[1]);
    });
    form.appendChild(element("p", "hint", "All other paths are hidden. Contributors cannot manage permissions or invite people. Git credentials are issued separately."));
    var submit = element("button", null, "Invite with these permissions");
    submit.type = "submit";
    var outcome = element("p");
    outcome.setAttribute("role", "status");
    form.appendChild(submit);
    form.appendChild(outcome);
    section.appendChild(form);
    card.appendChild(section);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (source.value === "account" && !selectedAccount) {
        outcome.className = "error";
        outcome.textContent = "Search for and select a Clerk account first.";
        return;
      }
      var selectedPaths = paths.value.split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean);
      if (!selectedPaths.length) return;
      submit.disabled = true;
      outcome.className = "hint";
      outcome.textContent = "Creating the permission-scoped invitation…";
      var payload = { principal_id: id.value.trim(), access: access.value, readable_paths: selectedPaths };
      if (source.value === "account") payload.clerk_user_id = selectedAccount.clerk_user_id;
      else payload.email = email.value.trim();
      request("/api/v1/invitations", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function (result) {
        if (result.status !== 201) {
          outcome.className = "error";
          outcome.textContent = result.status === 409 ? "That identifier, email, or Clerk account is already invited. Choose a unique collaborator." : result.body.error === "account-unavailable" ? "That account is no longer available or has no verified email. Search again." : "Invitation failed. Check the account, identifier, email, and existing repository paths.";
          return;
        }
        renderPermissions();
        var notice = element("p", "ok", "Invited " + result.body.view_id + " with " + result.body.access + " access. Share this site's URL so they can sign in.");
        notice.setAttribute("role", "status");
        document.getElementById("panel-permissions").prepend(notice);
      }).catch(function () {
        outcome.className = "error";
        outcome.textContent = "The invitation could not be created.";
      }).then(function () { submit.disabled = false; });
    });
  }

  function renderProfile() {
    var panel = document.getElementById("panel-profile");
    clearChildren(panel);
    var hero = element("div", "profile-hero");
    hero.appendChild(element("div", "profile-avatar", initials(session.principal.id)));
    var heading = element("div");
    heading.appendChild(element("h2", null, session.principal.id));
    heading.appendChild(element("p", null, "Bastion " + session.principal.kind + " · active on " + session.view_id));
    hero.appendChild(heading);
    var account = textButton(null, clerk ? "Manage account" : "Account managed by token");
    account.disabled = !clerk;
    account.addEventListener("click", function () { if (clerk) clerk.openUserProfile(); });
    hero.appendChild(account);
    panel.appendChild(hero);

    var grid = element("div", "profile-grid");
    var identityCard = element("article", "profile-card");
    identityCard.appendChild(element("h3", null, "Identity"));
    identityCard.appendChild(definitionList([
      ["Principal", session.principal.id],
      ["Account type", session.principal.kind],
      ["Teams", session.team_ids.join(", ") || "None"],
      ["Roles", session.role_ids.join(", ") || "None"],
    ]));
    var viewCard = element("article", "profile-card");
    viewCard.appendChild(element("h3", null, "Active repository view"));
    viewCard.appendChild(definitionList([
      ["View", session.view_id],
      ["Branch", ELEMENTS.branchName.textContent],
      ["Generation", session.descriptor.generation],
      ["Projection epoch", session.descriptor.projection_epoch],
    ]));
    var accessCard = element("article", "profile-card profile-card-wide");
    accessCard.appendChild(element("h3", null, "Read access"));
    var columns = element("div", "access-columns");
    var grants = element("div");
    grants.appendChild(element("h4", null, "Visible paths"));
    grants.appendChild(prefixList(session.descriptor.capabilities.read, "No visible paths"));
    var restrictions = element("div");
    restrictions.appendChild(element("h4", null, "Explicit restrictions"));
    restrictions.appendChild(prefixList(session.descriptor.restrictions.read, "No explicit restrictions"));
    columns.appendChild(grants);
    columns.appendChild(restrictions);
    accessCard.appendChild(columns);
    grid.appendChild(identityCard);
    grid.appendChild(viewCard);
    grid.appendChild(accessCard);
    panel.appendChild(grid);
  }

  function configureClone() {
    ELEMENTS.cloneUrl.textContent = window.location.protocol + "//" + encodeURIComponent(session.view_id) + "@" + window.location.host + "/" + encodeURIComponent(session.view_id) + ".git";
  }

  function openSession(token, envelope) {
    credential = token;
    session = envelope;
    historyRendered = false;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    ELEMENTS.loginError.hidden = true;
    ELEMENTS.login.hidden = true;
    ELEMENTS.homepage.hidden = true;
    ELEMENTS.homeNav.hidden = true;
    ELEMENTS.sessionNav.hidden = false;
    ELEMENTS.workspace.hidden = false;
    ELEMENTS.identity.hidden = false;
    ELEMENTS.identity.textContent = session.principal.id + " / " + session.view_id;
    ELEMENTS.manageAccount.hidden = !clerk;
    ELEMENTS.logout.hidden = false;
    ELEMENTS.repoOwner.textContent = session.principal.id;
    ELEMENTS.repoName.textContent = session.view_id;
    var refParts = session.descriptor.projected_ref.split("/");
    ELEMENTS.branchName.textContent = refParts[refParts.length - 1] || "main";
    document.getElementById("tab-permissions").hidden = true;
    configureClone();
    renderDecision();
    renderProfile();
    request("/api/v1/permissions").then(function (result) {
      if (result.status === 200) {
        document.getElementById("tab-permissions").hidden = false;
        renderPermissions(result.body);
      }
    });
    showPanel("code");
    loadDirectory(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function loadScript(src, attributes) {
    return new Promise(function (resolve, reject) {
      var script = document.createElement("script");
      script.src = src;
      script.defer = true;
      script.crossOrigin = "anonymous";
      Object.keys(attributes || {}).forEach(function (name) { script.setAttribute(name, attributes[name]); });
      script.addEventListener("load", resolve);
      script.addEventListener("error", function () { reject(new Error("authentication service failed to load")); });
      document.head.appendChild(script);
    });
  }

  function showClerkError(message) {
    ELEMENTS.loginError.hidden = false;
    ELEMENTS.loginError.textContent = message;
  }

  function mountClerkUserButton() {
    if (!clerk) return;
    if (!clerkUserButtonMounted) {
      clerk.mountUserButton(ELEMENTS.clerkUserButton, { afterSignOutUrl: "/" });
      clerkUserButtonMounted = true;
    }
    ELEMENTS.clerkUserButton.hidden = false;
  }

  function exchangeClerkSession(view) {
    if (!clerk || !clerk.session || clerkExchangePending) return;
    clerkExchangePending = true;
    ELEMENTS.loginError.hidden = true;
    clerk.session.getToken().then(function (token) {
      var payload = {};
      if (view) payload.view_id = view;
      return request("/api/v1/auth/clerk", {
        method: "POST",
        headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        optionalAuth: true,
        skipCredential: true,
      });
    }).then(function (result) {
      if (result.status === 409 && Array.isArray(result.body.view_ids)) {
        clearChildren(ELEMENTS.clerkView);
        result.body.view_ids.forEach(function (viewId) {
          var option = element("option", null, viewId);
          option.value = visibleText(viewId);
          ELEMENTS.clerkView.appendChild(option);
        });
        ELEMENTS.clerkViewForm.hidden = false;
        ELEMENTS.clerkSignIn.hidden = true;
        return;
      }
      if (result.status !== 200) {
        showClerkError(result.status === 403 ? "Your account has not been invited to this repository." : "Your sign-in could not be verified.");
        return;
      }
      mountClerkUserButton();
      openSession(result.body.access_token, result.body.session);
    }).catch(function () {
      showClerkError("Sign-in failed. Please try again.");
    }).then(function () { clerkExchangePending = false; });
  }

  function configureClerk(config) {
    if (!config.enabled) return Promise.resolve();
    ELEMENTS.clerkAuth.hidden = false;
    ELEMENTS.sessionNote.textContent = "Authentication, recovery, MFA, and account security are managed by Clerk.";
    return loadScript(config.frontend_api_origin + "/npm/@clerk/ui@1/dist/ui.browser.js")
      .then(function () {
        return loadScript(config.frontend_api_origin + "/npm/@clerk/clerk-js@6/dist/clerk.browser.js", { "data-clerk-publishable-key": config.publishable_key });
      })
      .then(function () {
        clerk = window.Clerk;
        return clerk.load({ ui: { ClerkUI: window.__internal_ClerkUICtor } });
      })
      .then(function () {
        clerkHadSession = Boolean(clerk.session);
        clerk.addListener(function (state) {
          var hasSession = Boolean(state.session);
          if (!clerkHadSession && hasSession && !session) {
            mountClerkUserButton();
            exchangeClerkSession();
          }
          if (clerkHadSession && !hasSession && credential) {
            request("/api/v1/logout", { method: "POST", optionalAuth: true }).then(clearBastionSession, clearBastionSession);
          }
          clerkHadSession = hasSession;
        });
        if (clerk.isSignedIn) {
          mountClerkUserButton();
          exchangeClerkSession();
        } else {
          clerk.mountSignIn(ELEMENTS.clerkSignIn);
        }
      });
  }

  function authenticate(token) {
    request("/api/v1/session", { headers: { Authorization: "Bearer " + token } }).then(function (result) {
      ELEMENTS.tokenInput.value = "";
      if (result.status !== 200) {
        showClerkError(result.status === 403 ? "This token cannot open the browser interface." : "Sign-in failed.");
        return;
      }
      openSession(token, result.body);
    }).catch(function (error) {
      ELEMENTS.tokenInput.value = "";
      if (!error || error.message !== "auth-required") showClerkError("Sign-in failed.");
    });
  }

  ELEMENTS.loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = ELEMENTS.tokenInput.value.trim();
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.loginError.hidden = true;
    if (token) authenticate(token);
  });
  ELEMENTS.clerkViewForm.addEventListener("submit", function (event) {
    event.preventDefault();
    exchangeClerkSession(ELEMENTS.clerkView.value);
  });
  ELEMENTS.manageAccount.addEventListener("click", function () { if (clerk) clerk.openUserProfile(); });
  ELEMENTS.logout.addEventListener("click", function () {
    request("/api/v1/logout", { method: "POST", optionalAuth: true }).then(function () {
      clearBastionSession();
      return clerk ? clerk.signOut() : null;
    }, function () {
      clearBastionSession();
      return clerk ? clerk.signOut() : null;
    });
  });
  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () { showPanel(tab.dataset.panel); });
  });
  document.getElementById("repository-nav").addEventListener("click", function () { showPanel("code"); });
  document.getElementById("profile-nav").addEventListener("click", function () { showPanel("profile"); });
  ELEMENTS.treeMore.addEventListener("click", function () { loadDirectory(false); });
  document.getElementById("tree-mode").addEventListener("click", function (event) {
    fullTree = !fullTree;
    treeRun += 1;
    treeState = { stack: [], entries: [], cursor: null, loading: false };
    event.currentTarget.textContent = fullTree ? "Browse one folder" : "Show full file structure";
    event.currentTarget.setAttribute("aria-pressed", String(fullTree));
    loadDirectory(true);
  });
  document.getElementById("refresh-repo").addEventListener("click", function () {
    request("/api/v1/session").then(function (result) {
      if (result.status === 200) openSession(credential, result.body);
    });
  });
  document.getElementById("clone-action").addEventListener("click", function () { ELEMENTS.cloneDialog.showModal(); });
  document.getElementById("clone-close").addEventListener("click", function () { ELEMENTS.cloneDialog.close(); });
  ELEMENTS.cloneDialog.addEventListener("click", function (event) {
    if (event.target === ELEMENTS.cloneDialog) ELEMENTS.cloneDialog.close();
  });
  document.getElementById("copy-clone").addEventListener("click", function (event) {
    var button = event.currentTarget;
    if (!navigator.clipboard) {
      button.textContent = "Copy unavailable";
      return;
    }
    navigator.clipboard.writeText(ELEMENTS.cloneUrl.textContent).then(function () {
      button.textContent = "Copied";
      window.setTimeout(function () { button.textContent = "Copy"; }, 1600);
    }).catch(function () { button.textContent = "Copy failed"; });
  });

  clearBastionSession();
  request("/api/v1/auth/config", { optionalAuth: true }).then(function (result) {
    if (result.status !== 200) throw new Error("authentication configuration unavailable");
    return configureClerk(result.body);
  }).catch(function () { showClerkError("Authentication is temporarily unavailable."); });
})();
