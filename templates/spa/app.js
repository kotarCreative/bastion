/* Bastion permission-demo application.
 *
 * Credential handling: the access token lives in one module-local variable
 * (page memory).  It is never written to any browser storage API, cookie,
 * URL, the DOM, or telemetry, and it is cleared from the input immediately
 * after use.  Reloading the page discards it.  All untrusted server data is
 * rendered through DOM text APIs (textContent), never HTML interpolation,
 * and displayed values are never turned into URLs without component-safe
 * encoding.
 */
"use strict";

(function () {
  var credential = null; // page memory only
  var session = null;

  var ELEMENTS = {
    identity: document.getElementById("identity"),
    logout: document.getElementById("logout"),
    login: document.getElementById("login"),
    loginForm: document.getElementById("login-form"),
    tokenInput: document.getElementById("token"),
    loginError: document.getElementById("login-error"),
    workspace: document.getElementById("workspace"),
  };

  function visibleText(value) {
    // Lossy presentation text; control characters were already escaped by
    // the server, so textContent renders them inertly.
    return String(value == null ? "" : value);
  }

  function clearChildren(node) {
    while (node.firstChild) {
      node.removeChild(node.firstChild);
    }
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = visibleText(text);
    }
    return node;
  }

  function request(path, options) {
    options = options || {};
    var headers = options.headers || {};
    if (credential) {
      headers["Authorization"] = "Bearer " + credential;
    }
    return fetch(path, {
      method: options.method || "GET",
      headers: headers,
      body: options.body,
      cache: "no-store",
      credentials: "omit",
      referrerPolicy: "no-referrer",
    }).then(function (response) {
      if (response.status === 401 && !options.optionalAuth) {
        signOut();
        throw new Error("auth-required");
      }
      return response.json().then(function (body) {
        return { status: response.status, body: body };
      });
    });
  }

  function showPanel(name) {
    var panels = ["summary", "decision", "tree", "history", "clone"];
    panels.forEach(function (panel) {
      document.getElementById("panel-" + panel).hidden = panel !== name;
      var tab = document.getElementById("tab-" + panel);
      tab.setAttribute("aria-selected", panel === name ? "true" : "false");
    });
  }

  function signOut() {
    credential = null;
    session = null;
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.login.hidden = false;
    ELEMENTS.workspace.hidden = true;
    ELEMENTS.identity.hidden = true;
    ELEMENTS.identity.textContent = "";
    ELEMENTS.logout.hidden = true;
    clearChildren(document.getElementById("panel-summary"));
    clearChildren(document.getElementById("panel-decision"));
    clearChildren(document.getElementById("panel-tree"));
    clearChildren(document.getElementById("panel-history"));
    clearChildren(document.getElementById("panel-clone"));
  }

  function prefixList(prefixes) {
    var list = element("ul");
    prefixes.forEach(function (prefix) {
      var item = element("li");
      item.textContent = visibleText(prefix.prefix_display);
      list.appendChild(item);
    });
    return list;
  }

  function capabilityTable(map, title) {
    var section = element("section");
    section.appendChild(element("h2", null, title));
    var table = element("table");
    var head = element("tr");
    head.appendChild(element("th", null, "capability"));
    head.appendChild(element("th", null, "prefixes"));
    table.appendChild(head);
    Object.keys(map).forEach(function (capability) {
      var row = element("tr");
      row.appendChild(element("td", null, capability));
      var cell = element("td");
      if (map[capability].length === 0) {
        cell.textContent = "(none)";
      } else {
        cell.appendChild(prefixList(map[capability]));
      }
      row.appendChild(cell);
      table.appendChild(row);
    });
    section.appendChild(table);
    return section;
  }

  function renderSummary() {
    var panel = document.getElementById("panel-summary");
    clearChildren(panel);
    var heading = element("h2", null, "View " + session.view_id);
    panel.appendChild(heading);
    var definition = element("dl");
    function row(term, value) {
      definition.appendChild(element("dt", null, term));
      definition.appendChild(element("dd", null, value));
    }
    row("principal", session.principal.id + " (" + session.principal.kind + ")");
    row("descriptor", session.descriptor.id);
    row("descriptor generation", session.descriptor.generation);
    row("projection epoch", session.descriptor.projection_epoch);
    row("projected ref", session.descriptor.projected_ref);
    row("teams", session.team_ids.join(", ") || "(none)");
    row("bindings", session.binding_ids.join(", ") || "(none)");
    row("roles", session.role_ids.join(", ") || "(none)");
    panel.appendChild(definition);
    panel.appendChild(element("p", "hint",
      "Downloads already fetched by a client stay readable until the client "
      + "discards them; a policy change starts a new projection epoch for "
      + "future operations."));
    panel.appendChild(
      capabilityTable(session.descriptor.capabilities, "Allowed prefixes")
    );
    panel.appendChild(
      capabilityTable(session.descriptor.restrictions, "Restricted prefixes")
    );
  }

  function renderDecision() {
    var panel = document.getElementById("panel-decision");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Decision inspector"));
    var form = element("form");
    form.setAttribute("autocomplete", "off");
    var label = element("label", null, "Path (repository-relative)");
    var input = element("input");
    input.type = "text";
    input.id = "decision-path";
    label.setAttribute("for", "decision-path");
    var capabilityLabel = element("label", null, "Capability");
    var select = element("select");
    select.id = "decision-capability";
    capabilityLabel.setAttribute("for", "decision-capability");
    ["read", "create", "modify", "delete", "mode", "symlink", "branch", "merge", "admin", "tag"]
      .forEach(function (capability) {
        var option = element("option", null, capability);
        option.value = capability;
        select.appendChild(option);
      });
    var submit = element("button", null, "Evaluate");
    submit.type = "submit";
    var outcome = element("div");
    form.appendChild(label);
    form.appendChild(input);
    form.appendChild(capabilityLabel);
    form.appendChild(select);
    form.appendChild(submit);
    panel.appendChild(form);
    panel.appendChild(outcome);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      clearChildren(outcome);
      var path = input.value.replace(/^\/+/, "");
      var raw = new TextEncoder().encode(path);
      var binary = "";
      raw.forEach(function (byte) {
        binary += String.fromCharCode(byte);
      });
      var encoded = btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
      request("/api/v1/decisions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ capability: select.value, path_b64: encoded }),
      }).then(function (result) {
        if (result.status !== 200) {
          outcome.appendChild(element("p", "error", "Decision evaluation failed."));
          return;
        }
        var verdict = element("p", result.body.allowed ? "verdict-allow" : "verdict-deny",
          (result.body.allowed ? "allowed" : "denied"));
        outcome.appendChild(verdict);
        var definition = element("dl");
        function matchRow(term, match) {
          definition.appendChild(element("dt", null, term));
          definition.appendChild(element("dd", null,
            match ? match.prefix_display : "(no match)"));
        }
        matchRow("longest allow", result.body.longest_allow);
        matchRow("longest deny", result.body.longest_deny);
        outcome.appendChild(definition);
      }).catch(function () {
        clearChildren(outcome);
        outcome.appendChild(element("p", "error", "Decision evaluation failed."));
      });
    });
  }

  function renderTree() {
    var panel = document.getElementById("panel-tree");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Projected tree"));
    var breadcrumb = element("p", "breadcrumb");
    var list = element("ul", "tree-list");
    var moreButton = element("button", null, "Load more");
    var state = { prefix: "", cursor: null };

    function encodeBytes(text) {
      var raw = new TextEncoder().encode(text);
      var binary = "";
      raw.forEach(function (byte) {
        binary += String.fromCharCode(byte);
      });
      return btoa(binary).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
    }

    function open(entry) {
      if (entry.kind !== "tree") {
        return;
      }
      state.prefix = entry.path_b64;
      state.cursor = null;
      breadcrumb.textContent = entry.path_display;
      clearChildren(list);
      load();
    }

    function load() {
      var query = "limit=50";
      if (state.prefix) {
        query += "&path_b64=" + encodeURIComponent(state.prefix);
      }
      if (state.cursor) {
        query += "&cursor=" + encodeURIComponent(state.cursor);
      }
      request("/api/v1/tree?" + query).then(function (result) {
        if (result.status !== 200) {
          list.appendChild(element("li", "error", "Tree is unavailable."));
          return;
        }
        result.body.entries.forEach(function (entry) {
          var item = element("li");
          item.textContent = entry.path_display + (entry.kind === "tree" ? "/" : "");
          if (entry.kind === "tree") {
            item.setAttribute("role", "button");
            item.tabIndex = 0;
            item.addEventListener("click", function () { open(entry); });
            item.addEventListener("keydown", function (event) {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                open(entry);
              }
            });
          }
          list.appendChild(item);
        });
        state.cursor = result.body.next_cursor;
        moreButton.hidden = !result.body.next_cursor;
      }).catch(function () {
        list.appendChild(element("li", "error", "Tree is unavailable."));
      });
    }

    moreButton.addEventListener("click", load);
    panel.appendChild(breadcrumb);
    panel.appendChild(list);
    panel.appendChild(moreButton);
    load();
  }

  function renderHistory() {
    var panel = document.getElementById("panel-history");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Visible history"));
    var list = element("div");
    var moreButton = element("button", null, "Load more");
    var cursor = null;
    function load() {
      var query = "limit=50" + (cursor ? "&cursor=" + encodeURIComponent(cursor) : "");
      request("/api/v1/history?" + query).then(function (result) {
        if (result.status !== 200) {
          list.appendChild(element("p", "error", "History is unavailable."));
          return;
        }
        result.body.commits.forEach(function (commit) {
          var entry = element("div", "commit");
          entry.appendChild(element("code", null, commit.oid));
          entry.appendChild(element("div", null, commit.message_display));
          entry.appendChild(element("div", "breadcrumb",
            commit.author_name_display + " <" + commit.author_email_display + "> "
            + new Date(commit.committed_at * 1000).toISOString()));
          list.appendChild(entry);
        });
        cursor = result.body.next_cursor;
        moreButton.hidden = !result.body.next_cursor;
      }).catch(function () {
        list.appendChild(element("p", "error", "History is unavailable."));
      });
    }
    moreButton.addEventListener("click", load);
    panel.appendChild(list);
    panel.appendChild(moreButton);
    load();
  }

  function renderClone() {
    var panel = document.getElementById("panel-clone");
    clearChildren(panel);
    panel.appendChild(element("h2", null, "Clone and credentials"));
    panel.appendChild(element("p", "hint",
      "The remote URL never contains your token. Configure Git once per host "
      + "so credentials are asked interactively and stored by a non-plaintext "
      + "helper."));
    var commands = [
      "git config credential.useHttpPath true",
      "git clone https://<host>/" + session.view_id + ".git",
    ];
    commands.forEach(function (line) {
      panel.appendChild(element("pre", null, line));
    });
    panel.appendChild(element("p", "hint",
      "When Git prompts for a password, paste your Bastion token. Two views on "
      + "the same host use different credentials; useHttpPath keeps them apart."));
  }

  function authenticate(token) {
    request("/api/v1/session", { headers: { Authorization: "Bearer " + token } })
      .then(function (result) {
        ELEMENTS.tokenInput.value = "";
        if (result.status !== 200) {
          ELEMENTS.loginError.hidden = false;
          ELEMENTS.loginError.textContent =
            result.status === 403 ? "This token cannot open the demo interface." : "Sign-in failed.";
          return;
        }
        credential = token;
        session = result.body;
        ELEMENTS.loginError.hidden = true;
        ELEMENTS.login.hidden = true;
        ELEMENTS.workspace.hidden = false;
        ELEMENTS.identity.hidden = false;
        ELEMENTS.identity.textContent = session.principal.id + " / " + session.view_id;
        ELEMENTS.logout.hidden = false;
        renderSummary();
        renderDecision();
        renderTree();
        renderHistory();
        renderClone();
        showPanel("summary");
      })
      .catch(function (error) {
        ELEMENTS.tokenInput.value = "";
        if (error && error.message === "auth-required") {
          return;
        }
        ELEMENTS.loginError.hidden = false;
        ELEMENTS.loginError.textContent = "Sign-in failed.";
      });
  }

  ELEMENTS.loginForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var token = ELEMENTS.tokenInput.value.trim();
    ELEMENTS.tokenInput.value = "";
    ELEMENTS.loginError.hidden = true;
    if (!token) {
      return;
    }
    authenticate(token);
  });

  ELEMENTS.logout.addEventListener("click", signOut);

  document.querySelectorAll(".tab").forEach(function (tab) {
    tab.addEventListener("click", function () {
      showPanel(tab.id.replace("tab-", ""));
    });
  });

  // Reloading the page starts from a fresh module: the credential is gone.
  signOut();
})();
