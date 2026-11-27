"""PR commits as narrative (issue #97, the owner's feedback: "This is in the
git commit diffs / patches ... Change logs, docs, ADRs, code comments. All
contain the narrative").

gather adds to each walked pr_history row, per commit, the code comments
added / removed / reworded (with their declaration), the documentation
prose changed (generated README sections skipped), the interface delta
between the commit and its parent, and per review comment the later commits
that changed the lines it is on. link carries them into the slice
(`evidence`, thread `response` / `verdict_after`, outcome.requests) and the
story check refuses a quoted code comment the slice does not hold.

The gather tests drive REAL git against a throwaway local `file://`
repository (no network, no global / system git config)."""
import copy
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import gather  # noqa: E402
import link  # noqa: E402
import validate  # noqa: E402
from test_story import GIT_TEST_ENV, URL, bundle, git, pr, review, train_of  # noqa: E402
from test_story_substance import (A, B, C, commit, conversation, enriched,  # noqa: E402
                                  history_row, rc)


def hunk(lines, old_start=1, new_start=1, func=""):
    """A parse_narrative_patch hunk from [(sign, text)] (numbers derived)."""
    out, o, n = [], old_start, new_start
    for sign, text in lines:
        if sign == " ":
            out.append((" ", text, o, n))
            o, n = o + 1, n + 1
        elif sign == "-":
            out.append(("-", text, o, None))
            o += 1
        else:
            out.append(("+", text, None, n))
            n += 1
    return {"old_start": old_start, "new_start": new_start, "func": func,
            "lines": out}


# --- 1. code comments ----------------------------------------------------------

class TestComments(unittest.TestCase):

    def test_bicep_add_drop_reword_with_their_declaration(self):
        h = hunk([(" ", "@description('Name.')"),
                  ("-", "// Requires a subnet when WAF alignment is on."),
                  ("+", "// Private networking is out of scope."),
                  (" ", "param name string"),
                  (" ", "resource sa 'Microsoft.Storage/storageAccounts@2024-01-01' = {"),
                  ("+", "  // keep public access for the Consumption plan"),
                  (" ", "  name: name"),
                  ("-", "  // TODO: firewall"),
                  (" ", "}")])
        rows = gather.narrative_comments("bicep", [h])
        self.assertEqual(rows[0], {"change": "reword",
                                   "before": "// Requires a subnet when WAF alignment is on.",
                                   "after": "// Private networking is out of scope.",
                                   "decl": "param name", "line": 2})
        self.assertEqual(rows[1], {"change": "add",
                                   "text": "// keep public access for the Consumption plan",
                                   "decl": "resource sa", "line": 5})
        self.assertEqual((rows[2]["change"], rows[2]["text"], rows[2]["decl"]),
                         ("drop", "// TODO: firewall", "resource sa"))

    def test_banners_and_type_lines_are_not_comments(self):
        h = hunk([("+", "// ========== //"), ("+", "// Parameters //"),
                  ("+", "// ========== //"),
                  ("+", "type m = {"), ("+", "  *: string"), ("+", "}")])
        self.assertEqual(gather.narrative_comments("bicep", [h]), [])
        block = hunk([("+", "/* Why:"), ("+", " * the plan needs it"), ("+", " */")])
        self.assertEqual(gather.narrative_comments("bicep", [block])[0]["text"],
                         "/* Why:\n* the plan needs it\n*/")

    def test_multi_line_comment_is_one_comment_and_capped(self):
        long = "// " + "x" * 600
        rows = gather.narrative_comments("hcl", [hunk([("+", "# one"), ("+", "# two"),
                                                       (" ", 'variable "a" {'),
                                                       ("+", long)])])
        self.assertEqual(rows[0]["text"], "# one\n# two")
        self.assertEqual(rows[0]["decl"], "variable a")
        self.assertEqual(len(rows[1]["text"]), gather.PR_NARRATIVE_COMMENT_CHARS)
        self.assertEqual(rows[1]["truncated_from"], len(long))

    def test_yaml_and_powershell(self):
        y = hunk([(" ", "jobs:"), (" ", "  build:"), ("+", "    # 900KB leaves headroom"),
                  (" ", "    runs-on: x")])
        self.assertEqual(gather.narrative_comments("yaml", [y])[0]["decl"], "build")
        ps = hunk([(" ", "function Set-Thing {"), ("+", "    # trim the summary"),
                   (" ", "}")])
        self.assertEqual(gather.narrative_comments("powershell", [ps])[0]["decl"],
                         "function Set-Thing")

    def test_unchanged_text_moved_is_not_a_reword(self):
        h = hunk([("-", "// same"), ("-", "x = 1"), ("+", "// same"), ("+", "x = 2")])
        self.assertEqual(gather.narrative_comments("bicep", [h]), [])


# --- 2. documentation prose ------------------------------------------------------

GENERATED_README = """# Function App `[App/FunctionApp]`

Generated description.

## Navigation

- [Parameters](#Parameters)

## Resource Types

| Resource Type | API Version |

## Parameters

### Parameter: `name`

## Notes

Hand-written note.
"""


class TestDocs(unittest.TestCase):

    def test_doc_kinds_and_adr_dirs(self):
        k = gather.doc_kind
        self.assertEqual(k("m/README.md"), "readme")
        self.assertEqual(k("m/CHANGELOG.md"), "changelog")
        self.assertEqual(k("docs/adr/0001-scope.md"), "adr")
        self.assertEqual(k("decisions/x.md"), "adr")
        self.assertEqual(k("docs/guide.md"), "docs")
        self.assertEqual(k("CONTRIBUTING.md"), "markdown")
        self.assertIsNone(k("main.bicep"))
        dirs = gather._adr_dirs({"ACTIVITY_ADR_DIRS": "records, design/rfc"})
        self.assertEqual(dirs, ("records", "design/rfc"))
        self.assertEqual(k("design/rfc/1.md", dirs), "adr")
        self.assertEqual(gather._adr_dirs({}), gather.PR_ADR_DIRS)

    def test_generated_readme_sections_are_skipped(self):
        gen, sec = gather.markdown_generated_lines(GENERATED_README)
        lines = GENERATED_README.split("\n")
        note = lines.index("Hand-written note.") + 1
        self.assertNotIn(note, gen)
        self.assertEqual(sec[note], "Notes")
        self.assertIn(lines.index("Generated description.") + 1, gen)
        self.assertIn(lines.index("### Parameter: `name`") + 1, gen)
        tf = "Intro.\n<!-- BEGIN_TF_DOCS -->\n| a |\n<!-- END_TF_DOCS -->\nOutro.\n"
        self.assertEqual(gather.markdown_generated_lines(tf)[0], {2, 3, 4})

    def test_narrative_docs_keeps_hand_written_lines(self):
        new = GENERATED_README
        old = new.replace("Hand-written note.", "Old note.").replace(
            "Generated description.", "Older description.")
        lines = new.split("\n")
        note = lines.index("Hand-written note.") + 1
        desc = lines.index("Generated description.") + 1
        h1 = hunk([("-", "Older description."), ("+", "Generated description.")],
                  old_start=desc, new_start=desc)
        h2 = hunk([("-", "Old note."), ("+", "Hand-written note.")],
                  old_start=note, new_start=note, func="## Notes")
        got = gather.narrative_docs("readme", [h1, h2], old, new)
        self.assertEqual(got, [{"section": "Notes", "added": ["Hand-written note."],
                                "removed": ["Old note."], "generated_skipped": 2}])

    def test_without_full_text_sections_come_from_the_hunk(self):
        h = hunk([(" ", "## Decision"), ("+", "We keep the Consumption plan."),
                  ("+", ""), ("+", "```")], func="# ADR 1")
        self.assertEqual(gather.narrative_docs("adr", [h]),
                         [{"section": "Decision",
                           "added": ["We keep the Consumption plan."], "removed": []}])


# --- 3. the interface reader and delta -------------------------------------------

BICEP_BEFORE = """metadata description = '''
Deploys a Function App.
'''

@description('Optional. Subnet for VNet integration.')
param functionAppSubnetResourceId string = ''

@allowed([
  'Y1'
  'EP1'
])
param appServicePlanSkuName string = 'Y1'

@description('Optional. Workspace name.')
param logAnalyticsWorkspaceName string = ''

// param commented string
resource sa 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: 'x'
}

module site 'br/public:avm/res/web/site:0.23.0' = {
  name: 'site'
}

output name string = sa.name
"""

BICEP_AFTER = """metadata description = '''
Deploys a Function App. Private networking is out of scope.
'''

@allowed([
  'FC1'
  'Y1'
  'EP1'
])
param appServicePlanSkuName string = 'FC1'

@description('Optional. Workspace name.')
param workspaceName string = ''

resource sa 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: 'x'
}

module site 'br/public:avm/res/web/site:0.24.0' = {
  name: 'site'
}

@description('The name.')
output name string = sa.name
"""


class TestInterface(unittest.TestCase):

    def test_bicep_reader(self):
        r = gather.read_bicep_interface(BICEP_BEFORE)
        self.assertNotIn(("param", "commented"), r)
        self.assertEqual(r[("metadata", "description")]["value"], "Deploys a Function App.")
        self.assertEqual(r[("param", "appServicePlanSkuName")],
                         {"kind": "param", "name": "appServicePlanSkuName",
                          "type": "string", "default": "'Y1'",
                          "allowed": "[ 'Y1' 'EP1' ]",
                          "_entries": {"allowed": ["'Y1'", "'EP1'"]}})
        self.assertEqual(r[("resource", "sa")]["api_version"], "2023-01-01")
        self.assertEqual(r[("module", "site")]["ref"],
                         "br/public:avm/res/web/site:0.23.0")

    def test_bicep_delta(self):
        rows = gather.diff_interface("m/main.bicep",
                                     gather.read_bicep_interface(BICEP_BEFORE),
                                     gather.read_bicep_interface(BICEP_AFTER))
        got = {(r["kind"], r["name"], r["change"]): r for r in rows}
        self.assertEqual(got[("param", "workspaceName", "renamed")]["from"],
                         "logAnalyticsWorkspaceName")
        removed = got[("param", "functionAppSubnetResourceId", "removed")]
        self.assertEqual(removed["before"]["description"],
                         "Optional. Subnet for VNet integration.")
        self.assertEqual(got[("param", "appServicePlanSkuName", "default")]
                         ["after"], "'FC1'")
        allowed = got[("param", "appServicePlanSkuName", "allowed")]
        self.assertEqual(allowed["entries_added"], ["'FC1'"])
        self.assertNotIn("before", allowed)
        self.assertEqual(got[("resource", "sa", "api_version")]["after"], "2024-01-01")
        self.assertEqual(got[("module", "site", "ref")]["after"],
                         "br/public:avm/res/web/site:0.24.0")
        self.assertEqual(got[("output", "name", "description")]["after"], "The name.")
        self.assertIn(("metadata", "description", "value"), got)

    def test_terraform_reader(self):
        tf = '''variable "sku" {
  type        = string
  default     = "Y1"
  description = <<EOT
The plan.
EOT
}

# variable "commented" {}
output "id" {
  description = "The ID."
  value       = azurerm_x.this.id
}

module "net" {
  source  = "Azure/avm-res-network/azurerm"
  version = "0.2.0"
}

resource "azurerm_storage_account" "this" {
  name = "x"
}
'''
        r = gather.read_tf_interface(tf)
        self.assertEqual(r[("variable", "sku")],
                         {"kind": "variable", "name": "sku", "type": "string",
                          "default": "Y1", "description": "The plan."})
        self.assertEqual(r[("output", "id")]["description"], "The ID.")
        self.assertEqual(r[("module", "net")]["version"], "0.2.0")
        self.assertIn(("resource", "azurerm_storage_account.this"), r)
        self.assertNotIn(("variable", "commented"), r)

    def test_long_values_are_compared_whole_and_emitted_capped(self):
        b = {("param", "p"): {"kind": "param", "name": "p", "default": "x" * 300}}
        a = {("param", "p"): {"kind": "param", "name": "p", "default": "x" * 299 + "y"}}
        row = gather.diff_interface("m.bicep", b, a)[0]
        self.assertEqual(row["change"], "default")
        self.assertEqual(len(row["after"]), gather.PR_NARRATIVE_VALUE_CHARS + 1)
        self.assertTrue(row["after"].endswith("…"))


# --- 4. review requests and what followed ------------------------------------------

class TestResponses(unittest.TestCase):

    def files(self):
        anchored = hunk([(" ", "param a string"),
                         ("-", "param firewallRules array"),
                         (" ", "param c string")], old_start=9, new_start=9)
        near = hunk([("-", "x"), ("+", "y")], old_start=21, new_start=21)
        return {B: {"m/main.bicep": [anchored]}, C: {"m/main.bicep": [near]}}

    def test_anchor_line_and_file(self):
        commits = [commit(A, "2026-09-01T00:00:00Z", "first"),
                   commit(B, "2026-09-03T00:00:00Z", "drop firewall"),
                   commit(C, "2026-09-04T00:00:00Z", "tidy")]
        comments = [
            {"id": 1, "author": "rev", "created_at": "2026-09-02T00:00:00Z",
             "path": "m/main.bicep", "line": 10, "commit": A,
             "diff_hunk": "@@ -8,3 +8,3 @@\n param a string\n+param firewallRules array"},
            {"id": 2, "author": "rev", "created_at": "2026-09-02T00:00:00Z",
             "path": "m/main.bicep", "line": 22, "commit": A,
             "diff_hunk": "@@ -20 +20 @@\n+}"},
            {"id": 3, "author": "rev", "path": "m/main.bicep", "in_reply_to": 1},
            {"id": 4, "author": "rev", "created_at": "2026-09-05T00:00:00Z",
             "path": "m/main.bicep", "commit": C}]
        got = gather.review_responses(comments, commits, self.files())
        self.assertEqual(len(got), 2)
        first = got[0]
        self.assertEqual((first["comment_id"], first["commits"][0]["sha"],
                          first["commits"][0]["match"]), (1, B, "anchor"))
        self.assertEqual(first["commits"][0]["hunks"][0]["removed"],
                         ["param firewallRules array"])
        self.assertEqual(first["file_commits"], [C])
        second = got[1]
        self.assertEqual(second["commits"], [])      # its first later commit is B
        self.assertEqual(second["file_commits"], [B, C])

    def test_line_match_for_the_next_commit(self):
        commits = [commit(A, "2026-09-01T00:00:00Z", "a"),
                   commit(C, "2026-09-04T00:00:00Z", "c")]
        c = {"id": 2, "created_at": "2026-09-02T00:00:00Z", "path": "m/main.bicep",
             "line": 22, "commit": A, "diff_hunk": "@@ -20 +20 @@\n+}"}
        got = gather.review_responses([c], commits, {C: self.files()[C]})
        self.assertEqual(got[0]["commits"][0]["match"], "line")


class TestCollectWithGit(unittest.TestCase):
    """Real git: an origin with refs/pull/7/head whose commits change a
    comment, a parameter, the README's hand-written section and the line a
    review comment is on."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ao-narr-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = mock.patch.dict(os.environ, GIT_TEST_ENV)
        env.start()
        self.addCleanup(env.stop)
        src = self.src = os.path.join(self.tmp, "src")
        git("init", "-q", "-b", "main", src)
        git("config", "uploadpack.allowFilter", "true", cwd=src)
        git("config", "uploadpack.allowAnySHA1InWant", "true", cwd=src)
        self.write("m/main.bicep", "param a string\n")
        self.write("m/README.md", "# M\n\n## Notes\n\nOld note.\n")
        self.commit("2026-04-01", "base")
        git("checkout", "-q", "-b", "feature", cwd=src)
        self.write("m/main.bicep",
                   "param a string\n"
                   "// Requires a subnet.\n"
                   "@description('Optional. Firewall rules.')\n"
                   "param firewallRules array = []\n")
        self.f1 = self.commit("2026-05-03", "first attempt")
        self.write("m/main.bicep",
                   "param a string\n"
                   "// Private networking is out of scope.\n")
        self.write("m/README.md", "# M\n\n## Notes\n\nNew note.\n")
        self.f2 = self.commit("2026-05-05", "drop the firewall")
        git("checkout", "-q", "main", cwd=src)
        git("update-ref", "refs/pull/7/head", self.f2, cwd=src)
        self.clone = os.path.join(self.tmp, "clone")
        git("clone", "-q", "--filter=blob:none", "--shallow-since=2026-03-02",
            "--single-branch", "file://" + src, self.clone)

    def write(self, rel, text):
        path = os.path.join(self.src, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)

    def commit(self, date, msg):
        git("add", "-A", cwd=self.src)
        git("commit", "-q", "-m", msg, cwd=self.src, date=date)
        return git("rev-parse", "HEAD", cwd=self.src).strip()

    def test_commits_carry_evidence_and_the_comment_its_response(self):
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "why?"}],
                "review_comments": [{
                    "id": 11, "author": "bob", "created_at": "2026-05-04T00:00:00Z",
                    "path": "m/main.bicep", "line": 4, "commit": self.f1,
                    "body": "Is the firewall needed?",
                    "diff_hunk": "@@ -1 +1,4 @@\n param a string\n+// Requires a "
                                 "subnet.\n+@description('Optional. Firewall rules.')"
                                 "\n+param firewallRules array = []"}]}]
        row = gather.collect_pr_history(prs, self.clone, base_branch="main")["prs"][0]
        self.assertTrue(row["narrative_collected"])
        c1, c2 = row["commits"]
        self.assertEqual(c1["interface"][0]["change"], "added")
        self.assertEqual(c1["comments"][0]["text"], "// Requires a subnet.")
        iface = {(r["name"], r["change"]) for r in c2["interface"]}
        self.assertIn(("firewallRules", "removed"), iface)
        self.assertEqual(c2["comments"][0]["change"], "reword")
        self.assertEqual(c2["docs"], [{"path": "m/README.md", "kind": "readme",
                                       "sections": [{"section": "Notes",
                                                     "added": ["New note."],
                                                     "removed": ["Old note."]}]}])
        resp = row["responses"][0]
        self.assertEqual((resp["comment_id"], resp["commits"][0]["sha"],
                          resp["commits"][0]["match"]), (11, self.f2, "anchor"))
        self.assertIn("param firewallRules array = []",
                      resp["commits"][0]["hunks"][0]["removed"])

    def test_a_failed_walk_is_recorded(self):
        real = gather.run_git

        def run(args, **kw):
            if any(a.startswith("core.attributesFile=") for a in args):
                raise RuntimeError("boom\nnarrative walk refused")
            return real(args, **kw)
        prs = [{"number": 7, "comments_list": [{"author": "bob", "body": "?"}]}]
        row = gather.collect_pr_history(prs, self.clone, run=run,
                                        base_branch="main")["prs"][0]
        self.assertEqual(row["narrative_skipped"],
                         "narrative walk failed: narrative walk refused")
        self.assertNotIn("narrative_collected", row)


# --- 5. link: evidence, responses, requests, the story check ---------------------

def _bundle():
    hist = history_row(1, [
        commit(A, "2026-05-02T00:00:00Z", "first", ["m/main.bicep"]),
        dict(commit(B, "2026-05-06T00:00:00Z", "drop the firewall", ["m/main.bicep"]),
             interface=[{"path": "m/main.bicep", "kind": "param",
                         "name": "firewallRules", "change": "removed",
                         "before": {"type": "array"}}],
             comments=[{"path": "m/main.bicep", "change": "add",
                        "text": "// Private networking is out of scope.",
                        "decl": "param a", "line": 2}],
             docs=[{"path": "m/README.md", "kind": "readme",
                    "sections": [{"section": "Notes", "added": ["New note."],
                                  "removed": []}]}])],
        notes=[], narrative_collected=True,
        responses=[{"comment_id": 11, "path": "m/main.bicep", "line": 4,
                    "at": "2026-05-04T00:00:00Z",
                    "commits": [{"sha": B, "match": "anchor", "hunks": [
                        {"old_start": 3, "new_start": 3,
                         "removed": ["param firewallRules array = []"],
                         "added": []}]}]}])
    p = pr(1, review_comments=[rc("bob", "2026-05-04T00:00:00Z",
                                  "Is the firewall needed?", 11,
                                  path="m/main.bicep", line=4)],
           reviews=[review("bob", "2026-05-04T00:01:00Z", "changes_requested", 31,
                           "Please drop it."),
                    review("bob", "2026-05-08T00:00:00Z", "approved", 32, "LGTM")])
    return bundle([p], pr_history=hist)


class TestSlice(unittest.TestCase):

    def setUp(self):
        self.c = conversation(_bundle())

    def test_commit_items_carry_evidence(self):
        it = next(i for i in self.c["timeline"] if i.get("sha") == B
                  and i["kind"] == "commit")
        ev = it["evidence"]
        self.assertEqual(ev["interface"][0]["name"], "firewallRules")
        self.assertEqual(ev["comments"][0]["class"], "note")
        self.assertEqual(ev["docs"][0]["sections"][0]["added"], ["New note."])
        self.assertEqual(self.c["truncation"]["evidence"],
                         {"budget": link.STORY_EVIDENCE_BUDGET, "kept": 3,
                          "omitted": 0})

    def test_review_thread_response_and_verdict(self):
        t = next(t for t in self.c["threads"] if t["kind"] == "review_thread")
        self.assertEqual(t["response"]["basis"], "anchor")
        c = t["response"]["commits"][0]
        self.assertEqual((c["sha"], c["match"], c["url"]),
                         (B, "anchor", f"{URL}/pull/1/commits/{B}"))
        self.assertEqual(c["hunks"][0]["removed"], ["param firewallRules array = []"])
        self.assertEqual(t["verdict_after"]["state"], "approved")

    def test_change_request_response_by_time_and_outcome_requests(self):
        t = next(t for t in self.c["threads"] if t["kind"] == "change_request")
        self.assertEqual(t["response"]["basis"], "time")
        self.assertEqual([c["sha"] for c in t["response"]["commits"]], [B])
        self.assertEqual(self.c["timeline"][t["response"]["commits"][0]["index"]]
                         ["sha"], B)
        self.assertEqual(t["verdict_after"]["url"], f"{URL}/pull/1#pullrequestreview-32")
        req = {r["kind"]: r for r in self.c["outcome"]["requests"]}
        self.assertEqual(req["change_request"]["followed_by"][0]["subject"],
                         "drop the firewall")
        self.assertEqual(req["change_request"]["verdict_after"]["state"], "approved")

    def test_commits_after_the_instant_are_not_a_response(self):
        b = _bundle()
        b["pr_history"]["prs"][0]["commits"][1]["committed_at"] = "2026-06-02T00:00:00Z"
        c = conversation(b)
        t = next(t for t in c["threads"] if t["kind"] == "review_thread")
        self.assertNotIn("response", t)

    def test_evidence_budget(self):
        with mock.patch.object(link, "STORY_EVIDENCE_BUDGET", 150):
            c = conversation(_bundle())
        it = next(i for i in c["timeline"] if i.get("sha") == B
                  and i["kind"] == "commit")
        self.assertEqual(it["evidence_omitted"], 2)
        self.assertEqual(c["truncation"]["evidence"]["omitted"], 2)

    def test_an_older_history_row_is_a_gap(self):
        b = _bundle()
        row = b["pr_history"]["prs"][0]
        del row["narrative_collected"]
        c = conversation(b)
        self.assertTrue([g for g in c["gaps"] if "PR commits of #1 carry no "
                         "code-comment" in g])

    def test_quoted_code_comment_must_be_in_the_slice(self):
        b = enriched(_bundle())
        s = link.slice_train(b, train_of(b)["id"])
        ok = ("On 6 May 2026 a commit added `// Private networking is out of "
              "scope.`.\n\nWhere it stands: #1 is open.")
        self.assertTrue(link.check_story_evidence(s, ok)["ok"])
        bad = ok.replace("out of scope.", "in scope.")
        r = link.check_story_evidence(s, bad)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code_comments_not_in_slice"],
                         ["// Private networking is in scope."])


class TestDocumented(unittest.TestCase):

    def test_every_new_key_is_in_bundle_md(self):
        import re
        with open(os.path.join(os.path.dirname(__file__), "BUNDLE.md"),
                  encoding="utf-8") as fh:
            text = fh.read()
        c = conversation(_bundle())
        row = _bundle()["pr_history"]["prs"][0]
        it = next(i for i in c["timeline"] if i.get("evidence"))
        threads = [t for t in c["threads"] if t.get("response")]
        keys = (set(it["evidence"]) | {"evidence", "evidence_omitted"}
                | {k for r in it["evidence"]["comments"] for k in r}
                | {k for r in it["evidence"]["docs"] for k in r}
                | {k for r in it["evidence"]["docs"] for s in r["sections"] for k in s}
                | {k for t in threads for k in t["response"]}
                | {k for t in threads for x in t["response"]["commits"] for k in x}
                | {k for t in c["threads"] for k in t.get("verdict_after", {})}
                | {k for r in c["outcome"]["requests"] for k in r}
                | set(row["responses"][0]) | set(row["responses"][0]["commits"][0])
                | set(row["responses"][0]["commits"][0]["hunks"][0])
                | {"narrative_collected", "narrative_skipped", "narrative_omitted",
                   "responses_omitted", "comments_omitted", "interface_omitted",
                   "entries_added", "entries_removed", "entries_omitted",
                   "generated_skipped", "file_commits", "verdict_after", "requests",
                   "STORY_EVIDENCE_BUDGET",
                   "PR_NARRATIVE_CHARS", "PR_RESPONSE_CHARS", "ACTIVITY_ADR_DIRS"})
        missing = sorted(k for k in keys if not re.search(
            r"(?<![\w]){}(?![\w])".format(re.escape(k)), text))
        self.assertEqual(missing, [])


class TestShapes(unittest.TestCase):

    def test_pr_history_narrative_shape(self):
        good = copy.deepcopy(_bundle()["pr_history"])
        self.assertIsNone(validate._fact_shape_problem("pr_history", good))
        bad = copy.deepcopy(good)
        bad["prs"][0]["commits"][1]["comments"] = "x"
        self.assertIsNotNone(validate._fact_shape_problem("pr_history", bad))
        bad = copy.deepcopy(good)
        bad["prs"][0]["responses"][0]["commits"][0]["match"] = "guess"
        self.assertIsNotNone(validate._fact_shape_problem("pr_history", bad))


if __name__ == "__main__":
    unittest.main()
