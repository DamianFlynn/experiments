"""The reader's module view (`module_changes`, link.build_module_changes).

Derived at view time from stored records only (commits, code / symbol
events, symbol moves, PRs, releases, code graph) plus the gathered
`module_notes` fact. The synthetic window below mirrors real October 2026
shapes of Azure/bicep-registry-modules: a README / CI sweep PR that also
deprecates a child module (with a declared breaking changelog item),
moves another module's child and renames a third one's; plus a new module, a
removed one, an orphaned one, a Terraform module repo with releases and a
landing-zone deployment.
"""
import copy
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import link  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

LAB = "avm/res/dev-test-lab/lab"
HUB = "avm/res/devices/iot-hub"
SQL = "avm/res/db-for-my-sql/flexible-server"
KV = "avm/res/key-vault/vault"
ST = "avm/res/storage/storage-account"
P1 = "avm/ptn/app/iaas"
P2 = "avm/ptn/dev-ops/runners"
NEW = "avm/res/code-signing/account"
OLD = "avm/res/old/thing"
ORPH = "avm/res/network/ip-group"

SWEEP_FILES = [
    f"{P1}/README.md", f"{P2}/README.md", f"{KV}/README.md",
    f"{ST}/policy/README.md",
    f"{SQL}/main.bicep", f"{SQL}/CHANGELOG.md",
    f"{SQL}/advanced-threat-protection-setting/main.bicep",
    f"{LAB}/main.bicep", f"{LAB}/main.json", f"{LAB}/CHANGELOG.md",
    f"{LAB}/version.json", f"{LAB}/secret/DEPRECATED.md", f"{LAB}/secret/README.md",
    f"{HUB}/main.bicep", f"{HUB}/event-hub-endpoint/consumergroup/main.bicep",
    "utilities/pipelines/sharedScripts/Set-ModuleReadMe.ps1",
]


def sym(commit, path, subkind, name, change, before=None, after=None,
        date="2026-10-03"):
    return {"commit": commit, "date": date, "path": path, "lang": "bicep",
            "subkind": subkind, "name": name, "change": change,
            "before": before, "after": after, "author": "a"}


def code(commit, change, path, old=None, date="2026-10-03"):
    e = {"commit": commit, "date": date, "change": change, "path": path,
         "author": "a"}
    if old:
        e["old_path"] = old
    return e


def pr(number, title, merged=True):
    return {"number": number, "title": title, "author": "dev{}".format(number),
            "url": "https://github.com/o/r/pull/{}".format(number),
            "merged": merged, "merged_at": "2026-10-03T14:00:16Z" if merged else None,
            "state": "closed", "base": "main", "created_at": "2026-10-01T00:00:00Z"}


def window():
    commits = [
        {"sha": "s7454", "date": "2026-10-03", "author": "AlexanderSehr",
         "message": "fix: CI - Readme generation updates (#7454)", "files": SWEEP_FILES},
        {"sha": "s100", "date": "2026-10-10", "author": "newdev",
         "message": "feat: initial version (#100)",
         "files": [f"{NEW}/main.bicep", f"{NEW}/CHANGELOG.md", f"{NEW}/README.md"]},
        {"sha": "s101", "date": "2026-10-11", "author": "x",
         "message": "chore: remove old thing (#101)",
         "files": [f"{OLD}/main.bicep", f"{OLD}/README.md"]},
        {"sha": "s102", "date": "2026-10-12", "author": "y",
         "message": "chore: orphan (#102)", "files": [f"{ORPH}/ORPHANED.md"]},
        {"sha": "s103", "date": "2026-10-13", "author": "z",
         "message": "ci: pin tools (#103)", "files": [".github/workflows/a.yml"]},
        {"sha": "s104", "date": "2026-10-14", "author": "z",
         "message": "fix(test): names (#104)", "files": [f"{KV}/tests/e2e/max/main.test.bicep"]},
        {"sha": "s105", "date": "2026-10-15", "author": "z",
         "message": "release as 0.3.0 (#105)",
         "files": [f"{KV}/CHANGELOG.md", f"{KV}/version.json"]},
    ]
    for c in commits:
        c["pr"] = int(c["message"].rsplit("#", 1)[1].rstrip(")"))
    code_events = [
        code("s7454", "modify", f"{LAB}/main.bicep"),
        code("s7454", "add", f"{LAB}/secret/DEPRECATED.md"),
        code("s7454", "modify", f"{LAB}/secret/README.md"),
        code("s7454", "rename", f"{HUB}/event-hub-endpoint/consumergroup/main.bicep",
             old=f"{HUB}/consumergroup/main.bicep"),
        code("s7454", "rename", f"{SQL}/advanced-threat-protection-setting/main.bicep",
             old=f"{SQL}/advanced-threat-protection/main.bicep"),
        code("s7454", "modify", f"{P1}/README.md"),
        code("s100", "add", f"{NEW}/main.bicep", date="2026-10-10"),
        code("s100", "add", f"{NEW}/CHANGELOG.md", date="2026-10-10"),
        code("s101", "delete", f"{OLD}/main.bicep", date="2026-10-11"),
        code("s102", "add", f"{ORPH}/ORPHANED.md", date="2026-10-12"),
    ]
    symbol_events = [
        sym("s7454", f"{LAB}/main.bicep", "param", "secrets", "drop",
            before="param secrets secretType[]?"),
        sym("s7454", f"{LAB}/main.bicep", "module", "lab_secrets", "drop",
            before="module lab_secrets 'secret/main.bicep' = ["),
        sym("s7454", f"{LAB}/main.bicep", "comment", "Optional. Secrets", "drop",
            before="@description('Optional. Secrets')"),
        sym("s7454", f"{HUB}/main.bicep", "module", "consumerGroupsModule", "change",
            before="module consumerGroupsModule 'consumergroup/main.bicep' = [",
            after="module consumerGroupsModule "
                  "'event-hub-endpoint/consumergroup/main.bicep' = ["),
        sym("s7454", f"{SQL}/main.bicep", "module", "atp", "change",
            before="module atp 'advanced-threat-protection/main.bicep' = {",
            after="module atp 'advanced-threat-protection-setting/main.bicep' = {"),
        sym("s100", f"{NEW}/main.bicep", "param", "name", "add",
            after="param name string", date="2026-10-10"),
        sym("s100", f"{NEW}/main.bicep", "param", "location", "add",
            after="param location string = resourceGroup().location",
            date="2026-10-10"),
    ]
    notes = {"collected": True, "changelogs": [
        {"path": f"{LAB}/CHANGELOG.md", "module": LAB, "commit": "s7454",
         "date": "2026-10-03", "change": "modify", "entries": [
             {"version": "0.6.0", "heading": "## 0.6.0", "sections": [
                 {"heading": "Changes", "kind": "changed", "items": [],
                  "placeholder": "None"},
                 {"heading": "Breaking Changes", "kind": "breaking", "items": [
                     {"text": "Deprecated `avm/res/dev-test-lab/lab/secret` as no "
                              "longer supported by Resource Provider"}]}]}]},
        {"path": f"{NEW}/CHANGELOG.md", "module": NEW, "commit": "s100",
         "date": "2026-10-10", "change": "add", "new_file": True, "entries": [
             {"version": "0.1.0", "heading": "## 0.1.0", "sections": [
                 {"heading": "Changes", "kind": "changed",
                  "items": [{"text": "Initial version"}]}]}]},
        {"path": f"{KV}/CHANGELOG.md", "module": KV, "commit": "s105",
         "date": "2026-10-15", "change": "modify", "entries": [
             {"version": "0.3.0", "heading": "## 0.3.0", "sections": [
                 {"heading": "Changes", "kind": "changed",
                  "items": [{"text": "Released unpublished changes"}]}]}]},
    ], "versions": [
        {"path": f"{LAB}/version.json", "module": LAB, "commit": "s7454",
         "date": "2026-10-03", "change": "modify", "from": "0.5", "to": "0.6"}],
        "tags": {"collected": True, "source": "clone", "items": [
            {"tag": f"{NEW}/0.1.0", "module": NEW, "version": "0.1.0",
             "date": "2026-10-10T12:00:00Z", "date_source": "commit",
             "commit": "s100", "first": True},
            {"tag": f"{HUB}/0.4.0", "module": HUB, "version": "0.4.0",
             "date": "2026-10-20T12:00:00Z", "date_source": "commit",
             "commit": "zzz", "first": False}]}}
    all_paths = sorted({f for c in commits for f in c["files"]}
                       | {e["path"] for e in code_events}
                       | {e["old_path"] for e in code_events if e.get("old_path")})
    areas = {}
    import gather
    for p in all_paths:
        areas.setdefault(gather.classify_code_area(p, gather.DEFAULT_AREA_PATTERNS),
                         []).append(p)
    return {
        "meta": {"owner": "o", "repo": "r", "from": "2026-10-01", "to": "2026-10-31",
                 "period": {"from": "2026-10-01", "to": "2026-10-31"},
                 "base_branch": "main"},
        "prs": [pr(7454, "fix: CI - Readme generation updates to help "
                         "parallization & regex issues"),
                pr(100, "feat: initial version code-signing-account module"),
                pr(101, "chore: remove old thing"), pr(102, "chore: orphan"),
                pr(103, "ci: pin tools"), pr(104, "fix(test): names"),
                pr(105, "feat: release unpublished changes as 0.3.0"),
                pr(106, "chore(module-list): sync [skip ci]"),
                pr(107, "open work", merged=False)],
        "issues": [], "commits": commits, "code_events": code_events,
        "symbol_events": symbol_events, "milestones": [], "releases": [],
        "code_graph": {"areas": [{"id": a, "paths": sorted(ps)}
                                 for a, ps in sorted(areas.items())]},
        "module_notes": notes,
    }


def enriched(bundle=None, **kw):
    b = bundle or window()
    link.enrich(b, **kw)
    return b


def module(b, area):
    return next(m for m in b["module_changes"]["modules"] if m["area"] == area)


class TestPathRules(unittest.TestCase):

    def test_defaults(self):
        rules = link.path_class_rules()
        cases = {
            "avm/res/a/b/main.bicep": "source", "main.tf": "source",
            "bicep/infra/central-dns/main.bicepparam": "source",
            "avm/res/a/b/main.json": "generated",
            "avm/res/a/b/README.md": "docs", "docs/guide.md": "docs",
            "avm/res/a/b/CHANGELOG.md": "metadata",
            "avm/res/a/b/version.json": "metadata",
            "avm/res/a/b/secret/DEPRECATED.md": "metadata",
            "avm/res/a/b/tests/e2e/max/main.test.bicep": "tests",
            "examples/default/main.tf": "tests", "tests/README.md": "tests",
            ".github/workflows/x.yml": "ci", ".github/CODEOWNERS": "ci",
            "utilities/pipelines/a.ps1": "ci", ".required-providers.json": "ci",
            "Makefile": "ci", "avm/res/a/b/.terraform.lock.hcl": "metadata",
        }
        for path, cls in cases.items():
            self.assertEqual(link.path_class(path, rules), cls, path)

    def test_operator_rules_come_first(self):
        rules = link.path_class_rules(["source=examples/**", "docs=*.ipynb"])
        self.assertEqual(link.path_class("examples/a/main.tf", rules), "source")
        self.assertEqual(link.path_class("validation/x.ipynb", rules), "docs")
        for bad in ("nonsense=*.md", "docs", "docs=", "=x"):
            with self.assertRaises(ValueError):
                link.path_class_rules([bad])

    def test_cli_accepts_path_class_and_refuses_a_bad_one(self):
        parsed = link._parse_argv(["b.json", "--path-class", "docs=*.ipynb",
                                   "--priority-label", "p2"])
        self.assertEqual(parsed, ("b.json", None, None, ["p2"], ["docs=*.ipynb"]))
        self.assertIsNone(link._parse_argv(["b.json", "--path-class", "bogus=x"]))

    def test_module_kinds(self):
        self.assertEqual(link.module_kind("avm/res/a/b"), "resource")
        self.assertEqual(link.module_kind("avm/ptn/a/b"), "pattern")
        self.assertEqual(link.module_kind("avm/utl/a/b"), "utility")
        self.assertIsNone(link.module_kind("avm/res/a"))
        self.assertEqual(link.module_kind("main.tf"), "module")
        self.assertEqual(link.module_kind("modules/net"), "submodule")
        self.assertEqual(link.module_kind("bicep/infra/dns",
                                          ["bicep/infra/dns/main.bicep"]), "deployment")
        self.assertEqual(link.module_kind("infra", ["infra/net.tf"]), "module")
        self.assertIsNone(link.module_kind("docs", ["docs/a.md"]))
        self.assertIsNone(link.module_kind(".github", [".github/w.yml"]))


class TestBuildModuleChanges(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.b = enriched()
        cls.mc = cls.b["module_changes"]

    def test_not_built_without_module_notes(self):
        b = window()
        del b["module_notes"]
        link.enrich(b)
        self.assertNotIn("module_changes", b)
        b = window()
        b["module_notes"] = {"collected": False}
        link.enrich(b)
        self.assertNotIn("module_changes", b)

    def test_dev_test_lab_declared_breaking_deprecation_and_candidate(self):
        lab = module(self.b, LAB)
        self.assertEqual((lab["kind"], lab["status"]), ("resource", "changed"))
        declared = lab["breaking"]["declared"]
        self.assertEqual([(d["version"], d["text"][:40]) for d in declared],
                         [("0.6.0", "Deprecated `avm/res/dev-test-lab/lab/sec")])
        self.assertEqual(declared[0]["sources"], [{
            "commit": "s7454", "pr": 7454,
            "url": "https://github.com/o/r/pull/7454", "path": f"{LAB}/CHANGELOG.md"}])
        self.assertEqual([(c["rule"], c["subject"]) for c in lab["breaking"]["candidates"]],
                         [("parameter_removed", "secrets")])
        self.assertEqual(sorted((d["basis"], d["target"]) for d in lab["deprecations"]),
                         [("changelog", f"{LAB}/secret"), ("marker_file", f"{LAB}/secret")])
        child = lab["structure"]["children"][0]
        self.assertEqual((child["path"], child["change"], child["markers"]),
                         (f"{LAB}/secret", "deprecated", ["DEPRECATED.md"]))
        self.assertEqual(lab["versions"]["version_file"]["from"], "0.5")
        self.assertEqual(lab["versions"]["version_file"]["to"], "0.6")
        self.assertEqual(lab["versions"]["changelog"], ["0.6.0"])
        self.assertEqual(lab["interface"]["parameters"]["removed"][0]["declaration"],
                         "param secrets secretType[]?")
        self.assertEqual(lab["interface"]["dependencies"]["removed"][0]["target"],
                         "secret/main.bicep")
        self.assertFalse(lab["plumbing_only"])
        # the changelog's placeholder section stays visible as such
        self.assertEqual(lab["changelog"][0]["sections"][0]["placeholder"], "None")

    def test_child_moves_are_structure_changes(self):
        hub = module(self.b, HUB)
        (child,) = hub["structure"]["children"]
        self.assertEqual((child["path"], child["from"], child["change"]),
                         (f"{HUB}/event-hub-endpoint/consumergroup",
                          f"{HUB}/consumergroup", "moved"))
        dep = hub["interface"]["dependencies"]["changed"][0]
        self.assertEqual((dep["change"], dep["from"], dep["to"]),
                         ("target", "consumergroup/main.bicep",
                          "event-hub-endpoint/consumergroup/main.bicep"))
        self.assertEqual([c["rule"] for c in hub["breaking"]["candidates"]],
                         ["child_module_moved"])
        self.assertEqual(hub["versions"]["published"][0]["version"], "0.4.0")
        sql = module(self.b, SQL)
        self.assertEqual(sql["structure"]["children"][0]["from"],
                         f"{SQL}/advanced-threat-protection")

    def test_the_sweep_pr_is_plumbing_but_its_module_changes_are_kept(self):
        row = next(r for r in self.mc["prs"] if r["number"] == 7454)
        self.assertEqual(row["class"], "mixed")
        self.assertEqual(row["title_signal"], "ci")
        self.assertTrue(row["plumbing"])
        self.assertEqual(row["sweep"], {"areas": 7, "plumbing_areas": 4})
        self.assertEqual(row["substantive_areas"], [SQL, LAB, HUB])
        self.assertEqual(row["module_kinds"], ["resource"])
        # README-only modules of the sweep are plumbing (the key vault also
        # had a release in #105, so it is not)
        for area in (P1, P2, ST):
            self.assertTrue(module(self.b, area)["plumbing_only"], area)
        self.assertFalse(module(self.b, KV)["plumbing_only"])
        self.assertEqual(module(self.b, P1)["product_score"], 0)
        lab_pr = module(self.b, LAB)["prs"][0]
        self.assertEqual((lab_pr["number"], lab_pr["class"], lab_pr["plumbing"]),
                         (7454, "mixed", True))

    def test_pr_classes(self):
        cls = {r["number"]: (r["class"], r["plumbing"]) for r in self.mc["prs"]}
        self.assertEqual(cls, {
            7454: ("mixed", True), 100: ("module", False), 101: ("module", False),
            102: ("docs-only", True), 103: ("ci", True), 104: ("tests-only", True),
            105: ("release", False), 106: ("unknown", None)})
        self.assertNotIn(107, cls)                    # not merged
        plumbing = self.mc["summary"]["plumbing"]
        self.assertEqual(plumbing, {
            "merged_prs": 8, "classified": 7, "plumbing_prs": 4,
            "share": round(4 / 7, 3),
            "by_class": {"mixed": 1, "module": 2, "docs-only": 1, "ci": 1,
                         "tests-only": 1, "release": 1, "unknown": 1}})

    def test_new_removed_orphaned(self):
        new = module(self.b, NEW)
        self.assertEqual((new["status"], new["first_version"]), ("new", "0.1.0"))
        # each signal is cited: the entry file, the first changelog, the tag
        self.assertEqual({s["pr"] for s in new["introduced_by"]}, {100})
        self.assertEqual(sorted(s["path"] for s in new["introduced_by"]),
                         [f"{NEW}/CHANGELOG.md", f"{NEW}/main.bicep",
                          f"refs/tags/{NEW}/0.1.0"])
        # a new module has no consumers: no breaking candidates
        self.assertNotIn("breaking", new)
        params = {p["name"]: p["required"]
                  for p in new["interface"]["parameters"]["added"]}
        self.assertEqual(params, {"name": True, "location": False})
        old = module(self.b, OLD)
        self.assertEqual(old["status"], "removed")
        self.assertEqual([c["rule"] for c in old["breaking"]["candidates"]],
                         ["module_removed"])
        self.assertEqual(module(self.b, ORPH)["status"], "orphaned")
        s = self.mc["summary"]
        self.assertEqual(s["new_modules"], [{
            "area": NEW, "kind": "resource", "first_version": "0.1.0",
            "introduced_by": [100], "prs": [100]}])
        self.assertEqual(s["removed"], [OLD])
        self.assertEqual(s["orphaned"], [ORPH])
        self.assertEqual(s["declared_breaking"],
                         [{"area": LAB, "versions": ["0.6.0"], "items": 1}])
        self.assertEqual(s["deprecations"], [
            {"area": LAB, "target": f"{LAB}/secret", "basis": "changelog"},
            {"area": LAB, "target": f"{LAB}/secret", "basis": "marker_file"}])

    def test_ranking_is_by_product_change(self):
        top = self.mc["summary"]["most_substantive"]
        # declared breaking + candidate + child + interface + changelog item
        self.assertEqual(top[0], {"area": LAB, "status": "changed", "score": 11})
        self.assertIn(NEW, [t["area"] for t in top])
        self.assertTrue(all(t["score"] > 0 for t in top))
        self.assertNotIn(P1, [t["area"] for t in top])
        scores = [t["score"] for t in top]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_every_item_cites_its_sources(self):
        def walk(node, path):
            if isinstance(node, dict):
                if "rule" in node or "text" in node and "version" in node:
                    self.assertTrue(node.get("sources"), path)
                for k, v in node.items():
                    walk(v, path + [k])
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, path + [i])
        walk(self.mc["modules"], [])

    def test_deterministic_and_key_order_free(self):
        a = enriched()
        rev = json.loads(json.dumps(window()), object_pairs_hook=lambda kv: dict(
            reversed(kv)))
        b = enriched(rev)
        self.assertEqual(json.dumps(a["module_changes"], sort_keys=True),
                         json.dumps(b["module_changes"], sort_keys=True))

    def test_path_rules_are_recorded(self):
        b = enriched(path_rules=["source=**/README.md"])
        self.assertEqual(b["module_changes"]["path_rules"], ["source=**/README.md"])
        self.assertFalse(module(b, P1)["plumbing_only"])
        self.assertNotIn("path_rules", self.mc)

    def test_inputs_say_what_was_collected(self):
        self.assertEqual(self.mc["inputs"], {"tags_collected": True})
        b = window()
        b["module_notes"]["tags"] = {"collected": False,
                                     "reason": "the clone holds no tags"}
        b["module_notes"]["unread"] = [{"path": "x", "commit": "c", "date": "d",
                                        "reason": "r"}]
        mc = enriched(b)["module_changes"]
        self.assertEqual(mc["inputs"]["tags_reason"], "the clone holds no tags")
        self.assertEqual(len(mc["inputs"]["unread"]), 1)


class TestOtherRepoShapes(unittest.TestCase):

    def test_terraform_module_repo(self):
        b = {"meta": {"owner": "o", "repo": "terraform-x-y", "period": {
                "from": "2026-10-01", "to": "2026-10-31"}, "base_branch": "main"},
             "prs": [pr(5, "feat!: drop var")], "issues": [], "milestones": [],
             "commits": [{"sha": "t1", "author": "a", "date": "2026-10-03",
                          "message": "feat!: drop var (#5)",
                          "files": ["variables.tf", "outputs.tf", "main.tf",
                                    "examples/default/main.tf"]}],
             "code_events": [code("t1", "modify", "variables.tf")],
             "symbol_events": [
                 dict(sym("t1", "variables.tf", "variable", "legacy", "drop",
                          before='variable "legacy" {'), lang="terraform"),
                 dict(sym("t1", "variables.tf", "variable", "tags", "add",
                          after='variable "tags" {'), lang="terraform"),
                 dict(sym("t1", "outputs.tf", "output", "key", "drop",
                          before='output "key" {'), lang="terraform")],
             "releases": [{"tag_name": "v1.2.0", "name": "v1.2.0", "prerelease": False,
                           "published_at": "2026-10-04T00:00:00Z",
                           "url": "https://github.com/o/terraform-x-y/releases/v1.2.0"}],
             "code_graph": {"areas": [
                 {"id": "main.tf", "paths": ["main.tf", "outputs.tf", "variables.tf"]},
                 {"id": "examples/default", "paths": ["examples/default/main.tf"]}]},
             "module_notes": {"collected": True, "changelogs": [], "versions": [],
                              "tags": {"collected": True, "source": "clone",
                                       "items": [{"tag": "v1.2.0", "module": "",
                                                  "version": "v1.2.0",
                                                  "date": "2026-10-04T00:00:00Z",
                                                  "date_source": "commit",
                                                  "commit": "t1", "first": False}]}}}
        link.enrich(b)
        root = module(b, "main.tf")
        self.assertEqual(root["kind"], "module")
        self.assertEqual(root["versions"]["releases"][0]["tag"], "v1.2.0")
        self.assertEqual(root["versions"]["published"][0]["version"], "v1.2.0")
        added = root["interface"]["parameters"]["added"][0]
        self.assertEqual((added["name"], added["required"]), ("tags", None))
        self.assertEqual(sorted(c["rule"] for c in root["breaking"]["candidates"]),
                         ["output_removed", "parameter_removed"])
        self.assertEqual(b["module_changes"]["releases"][0]["tag"], "v1.2.0")
        self.assertEqual(b["module_changes"]["prs"][0]["class"], "module")

    def test_landing_zone_deployments(self):
        dns = "bicep/infra/central-dns"
        b = {"meta": {"owner": "o", "repo": "lz", "period": {
                "from": "2026-10-01", "to": "2026-10-31"}},
             "prs": [], "issues": [], "milestones": [], "releases": [],
             "commits": [{"sha": "l1", "author": "a", "date": "2026-10-28",
                          "files": [f"{dns}/main.bicep", f"{dns}/main.bicepparam",
                                    "validation/notebook.ipynb"]}],
             "code_events": [code("l1", "add", f"{dns}/main.bicep", date="2026-10-28")],
             "symbol_events": [
                 sym("l1", f"{dns}/main.bicep", "param", "virtualNetworkIds", "add",
                     after="param virtualNetworkIds array = []", date="2026-10-28"),
                 sym("l1", f"{dns}/main.bicep", "output", "privateDnsZoneIds", "add",
                     after="output privateDnsZoneIds array = [", date="2026-10-28")],
             "code_graph": {"areas": [
                 {"id": dns, "paths": [f"{dns}/main.bicep", f"{dns}/main.bicepparam"]},
                 {"id": "validation", "paths": ["validation/notebook.ipynb"]}]},
             "module_notes": {"collected": True, "changelogs": [], "versions": [],
                              "tags": {"collected": False,
                                       "reason": "the clone holds no tags"}}}
        link.enrich(b)
        (entry,) = b["module_changes"]["modules"]
        self.assertEqual((entry["area"], entry["kind"], entry["status"]),
                         (dns, "deployment", "new"))
        self.assertEqual(entry["introduced_by"][0]["url"],
                         "https://github.com/o/lz/commit/l1")
        self.assertEqual(entry["interface"]["outputs"]["added"][0]["name"],
                         "privateDnsZoneIds")
        self.assertEqual(b["module_changes"]["summary"]["plumbing"]["merged_prs"], 0)


class TestDocsContract(unittest.TestCase):
    """BUNDLE.md names every key module_changes emits."""

    def test_every_key_is_documented(self):
        with open(os.path.join(HERE, "BUNDLE.md"), encoding="utf-8") as fh:
            doc = fh.read()
        section = doc[doc.index("## Module changes"):doc.index("## Optional")]
        b = enriched()
        b2 = window()
        b2["module_notes"]["unread"] = [{"path": "x", "commit": "c", "date": "d",
                                         "reason": "r"}]
        mc = enriched(b2, path_rules=["docs=*.ipynb"])["module_changes"]
        keys = set(mc) | set(mc["summary"]) | set(mc["inputs"])
        for e in b["module_changes"]["modules"]:
            keys |= set(e)
            for group in ("structure", "versions", "breaking", "interface"):
                keys |= set(e.get(group) or {})
        for r in b["module_changes"]["prs"]:
            keys |= set(r)
        missing = sorted(k for k in keys if not re.search(
            r"(?<![\w.]){}(?![\w])".format(re.escape(k)), section))
        self.assertEqual(missing, [])


class TestGoldensUnchanged(unittest.TestCase):
    """Fixture bundles carry no module_notes: enrich adds no key."""

    def test_fixture_bundles(self):
        for name in ("bundle_sample.json", "bundle_p3c.json"):
            with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
                b = json.load(fh)
            link.enrich(copy.deepcopy(b))
            out = link.enrich(b)
            self.assertNotIn("module_changes", out, name)


if __name__ == "__main__":
    unittest.main()
