// User guide page: renders docs/user-guide.md (GET /guide.md) with a contents
// list and a search box that keeps only the matching sections.
(function () {
  "use strict";

  function escapeHtml(text) {
    return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function slug(text, used) {
    var base = text.toLowerCase().replace(/<[^>]+>/g, "").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || "section";
    var name = base;
    for (var i = 2; used[name]; i++) name = base + "-" + i;
    used[name] = true;
    return name;
  }

  // Bold, italic, inline code and links inside one line
  function inline(text) {
    var codes = [];
    text = text.replace(/`([^`]+)`/g, function (_, code) {
      codes.push("<code>" + escapeHtml(code) + "</code>");
      return "\u0000" + (codes.length - 1) + "\u0000";
    });
    text = escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_, label, href) {
        return '<a href="' + href.replace(/"/g, "%22") + '">' + label + "</a>";
      });
    return text.replace(/\u0000(\d+)\u0000/g, function (_, i) {
      return codes[Number(i)];
    });
  }

  function cells(row) {
    return row.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split(/(?<!\\)\|/).map(function (c) {
      return c.trim().replace(/\\\|/g, "|");
    });
  }

  // A small Markdown renderer: headings, paragraphs, lists, tables, code
  // blocks and quotes (what the guide uses)
  function render(md) {
    var lines = md.replace(/\r\n/g, "\n").split("\n");
    var out = [];
    var used = {};
    var toc = [];
    var i = 0;
    function isBlockStart(line) {
      return /^(#{1,6}\s|```|\s*[-*]\s|\s*\d+[.)]\s|>|\|)/.test(line);
    }
    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) {
        i++;
        continue;
      }
      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        var level = heading[1].length;
        var html = inline(heading[2]);
        var id = slug(heading[2], used);
        out.push("<h" + level + ' id="' + id + '">' + html + "</h" + level + ">");
        if (level === 2 || level === 3) toc.push({ level: level, id: id, html: html });
        i++;
        continue;
      }
      if (/^```/.test(line)) {
        var code = [];
        i++;
        while (i < lines.length && !/^```/.test(lines[i])) code.push(lines[i++]);
        i++;
        out.push("<pre><code>" + escapeHtml(code.join("\n")) + "</code></pre>");
        continue;
      }
      if (/^\|/.test(line) && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var head = cells(line);
        i += 2;
        var rows = [];
        while (i < lines.length && /^\|/.test(lines[i])) rows.push(cells(lines[i++]));
        out.push(
          "<table><thead><tr>" + head.map(function (c) { return "<th>" + inline(c) + "</th>"; }).join("") + "</tr></thead><tbody>" +
            rows.map(function (r) { return "<tr>" + r.map(function (c) { return "<td>" + inline(c) + "</td>"; }).join("") + "</tr>"; }).join("") +
            "</tbody></table>"
        );
        continue;
      }
      if (/^>/.test(line)) {
        var quote = [];
        while (i < lines.length && /^>/.test(lines[i])) quote.push(lines[i++].replace(/^>\s?/, ""));
        out.push("<blockquote>" + inline(quote.join(" ")) + "</blockquote>");
        continue;
      }
      var list = /^(\s*)([-*]|\d+[.)])\s+/.exec(line);
      if (list) {
        var ordered = /\d/.test(list[2]);
        var items = [];
        while (i < lines.length && lines[i].trim()) {
          var item = /^(\s*)([-*]|\d+[.)])\s+(.*)$/.exec(lines[i]);
          if (item) items.push({ depth: item[1].length >= 2 ? 1 : 0, text: item[3] });
          else if (items.length) items[items.length - 1].text += " " + lines[i].trim();
          i++;
        }
        var htmlList = "";
        var depth = 0;
        var tag = ordered ? "ol" : "ul";
        htmlList += "<" + tag + ">";
        items.forEach(function (it, n) {
          if (it.depth > depth) htmlList += "<ul>";
          else if (it.depth < depth) htmlList += "</li></ul></li>";
          else if (n) htmlList += "</li>";
          depth = it.depth;
          htmlList += "<li>" + inline(it.text);
        });
        htmlList += depth ? "</li></ul></li>" : "</li>";
        htmlList += "</" + tag + ">";
        out.push(htmlList);
        continue;
      }
      var para = [];
      while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i]))) para.push(lines[i++].trim());
      out.push("<p>" + inline(para.join(" ")) + "</p>");
    }
    return { html: out.join("\n"), toc: toc };
  }

  // Sections: each h2/h3 with the blocks under it, so a search keeps whole sections
  function sections(doc) {
    var list = [];
    var current = null;
    Array.prototype.forEach.call(doc.children, function (node) {
      if (node.tagName === "H2" || node.tagName === "H3") {
        current = { head: node, nodes: [node], parent: node.tagName === "H3" ? lastH2 : null };
        if (node.tagName === "H2") lastH2 = current;
        list.push(current);
      } else if (current) current.nodes.push(node);
    });
    return list;
  }
  var lastH2 = null;

  function unmark(doc) {
    Array.prototype.forEach.call(doc.querySelectorAll("mark"), function (m) {
      m.replaceWith(document.createTextNode(m.textContent));
    });
    doc.normalize();
  }

  function markText(node, words) {
    var walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    var texts = [];
    while (walker.nextNode()) texts.push(walker.currentNode);
    var pattern = new RegExp("(" + words.map(function (w) { return w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }).join("|") + ")", "gi");
    texts.forEach(function (t) {
      if (!pattern.test(t.nodeValue)) return;
      pattern.lastIndex = 0;
      var span = document.createElement("span");
      span.innerHTML = escapeHtml(t.nodeValue).replace(pattern, "<mark>$1</mark>");
      t.replaceWith.apply(t, Array.prototype.slice.call(span.childNodes));
    });
  }

  function search(doc, list, tocLinks, query, count) {
    unmark(doc);
    var words = query.toLowerCase().split(/\s+/).filter(Boolean);
    var shown = 0;
    var keepH2 = {};
    list.forEach(function (s) {
      var text = s.nodes.map(function (n) { return n.textContent; }).join(" ").toLowerCase();
      s.match = !words.length || words.every(function (w) { return text.indexOf(w) >= 0; });
      if (s.match && s.parent) keepH2[s.parent.head.id] = true;
    });
    list.forEach(function (s) {
      // An h2 heading stays visible above its matching h3 sections
      var visible = s.match || keepH2[s.head.id];
      s.nodes.forEach(function (n, k) {
        n.classList.toggle("hidden", !(s.match || (k === 0 && visible)));
      });
      if (tocLinks[s.head.id]) tocLinks[s.head.id].classList.toggle("hidden", !visible);
      if (s.match && words.length) {
        shown++;
        s.nodes.forEach(function (n) { markText(n, words); });
      }
    });
    count.textContent = words.length ? shown + " section" + (shown === 1 ? "" : "s") + " match" : "";
  }

  function start() {
    var doc = document.getElementById("help-doc");
    var tocBox = document.getElementById("help-toc");
    var box = document.getElementById("help-search");
    var count = document.getElementById("help-count");
    fetch("/branding").then(function (r) { return r.ok ? r.json() : null; }).then(function (brand) {
      if (!brand || !brand.name) return;
      document.title = brand.name + " · User guide";
      Array.prototype.forEach.call(document.querySelectorAll(".brand-name"), function (n) { n.textContent = brand.name; });
    }).catch(function () {});
    fetch("/guide.md")
      .then(function (r) {
        if (!r.ok) throw new Error("The guide could not be loaded (" + r.status + ")");
        return r.text();
      })
      .then(function (md) {
        var result = render(md);
        doc.innerHTML = result.html;
        var tocLinks = {};
        tocBox.innerHTML = result.toc.map(function (t) {
          return '<a class="l' + t.level + '" href="#' + t.id + '">' + t.html + "</a>";
        }).join("");
        Array.prototype.forEach.call(tocBox.querySelectorAll("a"), function (a) {
          tocLinks[a.getAttribute("href").slice(1)] = a;
        });
        var list = sections(doc);
        var timer = null;
        box.addEventListener("input", function () {
          clearTimeout(timer);
          timer = setTimeout(function () {
            search(doc, list, tocLinks, box.value, count);
            try {
              history.replaceState(null, "", box.value ? "?q=" + encodeURIComponent(box.value) : location.pathname);
            } catch (e) {}
          }, 200);
        });
        var q = new URLSearchParams(location.search).get("q");
        if (q) {
          box.value = q;
          search(doc, list, tocLinks, q, count);
        }
        if (location.hash) {
          var target = document.getElementById(location.hash.slice(1));
          if (target) target.scrollIntoView();
        }
      })
      .catch(function (error) {
        doc.textContent = error.message;
      });
  }

  start();
})();
