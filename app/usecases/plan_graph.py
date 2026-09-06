"""Topology graph from a plan (SPEC v1.4): the same vocabulary as `topology.py`, filled from
`tofu show -json <planfile>` instead of the cloud, so the off state draws what ON deploys.

Two sources inside one plan document:
- `planned_values.root_module.resources[].values` — the attributes known before apply (CIDRs,
  tags, instance types, ports). Anything unknown until apply (ids, IPs, allocation ids) is
  simply absent there and stays `null` on the node — never invented.
- `configuration.root_module.resources[].expressions[*].references` — the structure: which
  subnet sits in which VPC, which instance in which subnet, which route table a subnet is
  associated with and where its default route points, which group a rule belongs to.

Nothing provider-specific escapes the `PlanSchema` table and a node's `detail`: the builder
only knows kinds (vpc, subnet, instance, nat, igw, eip) and linking roles (route table,
route, association, security group, ingress rule). Node ids are resource addresses
(`aws_instance.pse`), labels come from the name tag else the resource name.

`SourceIndex` scans the use case's `terraform_dir` for `resource "<type>" "<name>"` blocks so
every node can carry `source: {path, line}` — planned nodes by address, deployed nodes by
matching their name tag (and, for gateways, their VPC) to a block that is in state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.usecases.manifest import Manifest
from app.usecases.topology import DEFAULT_ROUTE, Graph, assemble, rule_label

STRUCTURE_KINDS = ("vpc", "subnet", "instance", "nat", "igw", "eip")
LINK_KINDS = ("route_table", "route", "association", "security_group", "ingress_rule")


# ---------------------------------------------------------------------- the provider table
@dataclass(frozen=True)
class TypeSpec:
    """How one resource type maps onto the graph.

    kind:    a node kind or a linking kind (see STRUCTURE_KINDS / LINK_KINDS)
    refs:    role -> expression key whose `references` point at another resource
    fields:  node field -> attribute key carrying a value known at plan time
    blocks:  role -> name of a nested block (inline routes / inline ingress rules)
    rule:    attribute keys of a rule-shaped thing (dest/targets for routes; proto/from/to and
             cidr(s)/groups for security rules)
    only_if: attribute equalities the resource must satisfy to count (e.g. type == ingress)
    default: a route table that applies to subnets with no explicit association
    """

    kind: str
    refs: dict[str, str] = field(default_factory=dict)
    fields: dict[str, str] = field(default_factory=dict)
    blocks: dict[str, str] = field(default_factory=dict)
    rule: dict[str, Any] = field(default_factory=dict)
    only_if: dict[str, Any] = field(default_factory=dict)
    default: bool = False


@dataclass(frozen=True)
class PlanSchema:
    provider: str
    types: dict[str, TypeSpec]
    tags_attr: str = "tags"
    name_tag: str = "Name"
    region_key: str = "region"
    any_protocol: tuple[str, ...] = ("-1", "all")

    def spec(self, rtype: str) -> TypeSpec | None:
        return self.types.get(rtype)

    def kinds(self) -> dict[str, str]:
        return {t: s.kind for t, s in self.types.items()}


AWS = PlanSchema(
    provider="aws",
    types={
        "aws_vpc": TypeSpec("vpc", fields={"cidr": "cidr_block"}),
        "aws_subnet": TypeSpec("subnet", refs={"vpc": "vpc_id"}, fields={"cidr": "cidr_block", "az": "availability_zone"}),
        "aws_instance": TypeSpec(
            "instance",
            refs={"subnet": "subnet_id", "groups": "vpc_security_group_ids"},
            fields={"type": "instance_type", "private_ip": "private_ip", "public_ip": "public_ip", "az": "availability_zone"},
        ),
        "aws_nat_gateway": TypeSpec("nat", refs={"subnet": "subnet_id", "eip": "allocation_id"}, fields={"public_ip": "public_ip", "private_ip": "private_ip"}),
        "aws_internet_gateway": TypeSpec("igw", refs={"vpc": "vpc_id"}),
        "aws_eip": TypeSpec("eip", refs={"instance": "instance"}, fields={"ip": "public_ip"}),
        "aws_route_table": TypeSpec(
            "route_table", refs={"vpc": "vpc_id"}, blocks={"routes": "route"},
            rule={"dest": "cidr_block", "targets": ("gateway_id", "nat_gateway_id")},
        ),
        "aws_default_route_table": TypeSpec(
            "route_table", refs={"vpc": "default_route_table_id"}, blocks={"routes": "route"},
            rule={"dest": "cidr_block", "targets": ("gateway_id", "nat_gateway_id")}, default=True,
        ),
        "aws_route": TypeSpec(
            "route", refs={"route_table": "route_table_id", "gateway": "gateway_id", "nat": "nat_gateway_id"},
            rule={"dest": "destination_cidr_block", "targets": ("gateway_id", "nat_gateway_id")},
        ),
        "aws_route_table_association": TypeSpec("association", refs={"subnet": "subnet_id", "route_table": "route_table_id"}),
        "aws_security_group": TypeSpec(
            "security_group", refs={"vpc": "vpc_id"}, fields={"name": "name"}, blocks={"ingress": "ingress"},
            rule={"proto": "protocol", "from": "from_port", "to": "to_port", "cidrs": "cidr_blocks", "groups": "security_groups"},
        ),
        "aws_vpc_security_group_ingress_rule": TypeSpec(
            "ingress_rule", refs={"group": "security_group_id", "source_group": "referenced_security_group_id", "source": "cidr_ipv4"},
            rule={"proto": "ip_protocol", "from": "from_port", "to": "to_port", "cidr": "cidr_ipv4"},
        ),
        "aws_security_group_rule": TypeSpec(
            "ingress_rule", refs={"group": "security_group_id", "source_group": "source_security_group_id"},
            rule={"proto": "protocol", "from": "from_port", "to": "to_port", "cidrs": "cidr_blocks"}, only_if={"type": "ingress"},
        ),
    },
)
SCHEMAS: dict[str, PlanSchema] = {"aws": AWS}


# ---------------------------------------------------------------------- the plan document
def strip_index(address: str) -> str:
    """`aws_subnet.x[0]` / `module.m["a"].aws_vpc.v` -> the configuration address without keys."""
    return re.sub(r"\[[^\]]*\]", "", address)


class _Plan:
    """Random access over a `tofu show -json` plan: planned values by address, configuration
    expressions by address, and reference resolution (config address -> planned addresses)."""

    def __init__(self, show: dict[str, Any]) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.types: dict[str, str] = {}
        self.names: dict[str, str] = {}
        self.order: list[str] = []
        self.config: dict[str, dict[str, Any]] = {}
        self.module_of: dict[str, str] = {}
        self.instances: dict[str, list[str]] = {}
        self._walk_values((show.get("planned_values") or {}).get("root_module") or {})
        self._walk_config((show.get("configuration") or {}).get("root_module") or {}, "")
        for addr in self.order:
            self.instances.setdefault(strip_index(addr), []).append(addr)
        self.provider_config: dict[str, Any] = (show.get("configuration") or {}).get("provider_config") or {}

    def _walk_values(self, module: dict[str, Any]) -> None:
        for res in module.get("resources") or []:
            if not isinstance(res, dict) or res.get("mode", "managed") != "managed" or not res.get("address"):
                continue
            addr = res["address"]
            self.values[addr] = res.get("values") if isinstance(res.get("values"), dict) else {}
            self.types[addr] = res.get("type") or ""
            self.names[addr] = res.get("name") or ""
            self.order.append(addr)
        for child in module.get("child_modules") or []:
            if isinstance(child, dict):
                self._walk_values(child)

    def _walk_config(self, module: dict[str, Any], prefix: str) -> None:
        for res in module.get("resources") or []:
            if not isinstance(res, dict) or res.get("mode", "managed") != "managed" or not res.get("address"):
                continue
            addr = f"{prefix}{res['address']}"
            self.config[addr] = res.get("expressions") if isinstance(res.get("expressions"), dict) else {}
            self.module_of[addr] = prefix
        for name, call in (module.get("module_calls") or {}).items():
            if isinstance(call, dict) and isinstance(call.get("module"), dict):
                self._walk_config(call["module"], f"{prefix}module.{name}.")

    # -- values
    def known(self, addr: str, key: str) -> Any:
        """A planned value, else the configuration constant; None when unknown until apply."""
        value = self.values.get(addr, {}).get(key)
        if value is not None:
            return value
        expr = self.config.get(strip_index(addr), {}).get(key)
        return expr.get("constant_value") if isinstance(expr, dict) else None

    def tags(self, addr: str, tags_attr: str) -> dict[str, Any]:
        tags = self.known(addr, tags_attr)
        return tags if isinstance(tags, dict) else {}

    def blocks(self, addr: str, block: str) -> list[dict[str, Any]]:
        """Nested block items as expression maps (constants + references), config first because
        a block holding one unknown value is dropped from planned values as a whole."""
        items = self.config.get(strip_index(addr), {}).get(block)
        if isinstance(items, list) and items:
            return [i for i in items if isinstance(i, dict)]
        values = self.values.get(addr, {}).get(block)
        if isinstance(values, list):
            return [{k: {"constant_value": v} for k, v in item.items()} for item in values if isinstance(item, dict)]
        return []

    # -- references
    def refs(self, addr: str, key: str) -> list[str]:
        """Planned addresses referenced by expression `key` on `addr` (expanded through count/for_each)."""
        return self.resolve(self.config.get(strip_index(addr), {}).get(key), self.module_of.get(strip_index(addr), ""))

    def resolve(self, expr: Any, module: str = "") -> list[str]:
        if not isinstance(expr, dict):
            return []
        out: list[str] = []
        for ref in expr.get("references") or []:
            if not isinstance(ref, str) or ref.startswith(("var.", "local.", "data.", "each.", "count.", "path.")):
                continue
            # "aws_vpc.lab.id" -> try "aws_vpc.lab.id", then "aws_vpc.lab"; module-relative first.
            parts = strip_index(ref).split(".")
            found = next(
                (full for n in range(len(parts), 1, -1) for full in (f"{module}{'.'.join(parts[:n])}", ".".join(parts[:n])) if full in self.instances),
                None,
            )
            for planned in self.instances.get(found or "", []):
                if planned not in out:
                    out.append(planned)
        return out

    @staticmethod
    def const(item: dict[str, Any], key: str) -> Any:
        expr = item.get(key)
        return expr.get("constant_value") if isinstance(expr, dict) else None


# ---------------------------------------------------------------------- the builder
DETAIL_TEXT_LIMIT = 512


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    """Planned values for `detail`, minus long text blobs (user data scripts, policies)."""
    return {k: (v if not (isinstance(v, str) and len(v) > DETAIL_TEXT_LIMIT) else f"<{len(v)} chars>") for k, v in values.items()}


class _PlanGraph:
    def __init__(self, manifest: Manifest, schema: PlanSchema, plan: _Plan, sources: SourceIndex | None) -> None:
        self.manifest, self.schema, self.plan, self.sources = manifest, schema, plan, sources
        self.graph = Graph(manifest)
        self.region = self._region()
        self.kind_of: dict[str, str] = {}
        for addr in plan.order:
            spec = schema.spec(plan.types.get(addr, ""))
            if spec is not None and self._passes(addr, spec):
                self.kind_of[addr] = spec.kind

    def _passes(self, addr: str, spec: TypeSpec) -> bool:
        return all(self.plan.known(addr, k) == v for k, v in spec.only_if.items())

    def _spec(self, addr: str) -> TypeSpec:
        return self.schema.types[self.plan.types[addr]]

    def of_kind(self, kind: str) -> list[str]:
        return [a for a in self.plan.order if self.kind_of.get(a) == kind]

    def label(self, addr: str) -> str:
        return str(self.plan.tags(addr, self.schema.tags_attr).get(self.schema.name_tag) or self.plan.names.get(addr) or addr)

    def first_ref(self, addr: str, role: str, kind: str | None = None) -> str | None:
        key = self._spec(addr).refs.get(role)
        if not key:
            return None
        for target in self.plan.refs(addr, key):
            if kind is None or self.kind_of.get(target) == kind:
                return target
        return None

    def node(self, addr: str, kind: str, **extra: Any) -> dict[str, Any]:
        spec = self._spec(addr)
        node: dict[str, Any] = {"id": addr, "kind": kind, "label": self.label(addr), "parent": None}
        for fld, attr in spec.fields.items():
            node[fld] = self.plan.known(addr, attr)
        node.update(extra)
        node.setdefault("region", self.region)
        node["tagged"] = True
        node["address"] = addr
        node["detail"] = {"address": addr, "type": self.plan.types[addr], "name": self.plan.names[addr], **_compact(self.plan.values.get(addr, {}))}
        if self.sources is not None:
            src = self.sources.source(addr)
            if src:
                node["source"] = src
        return node

    # -- region
    def _region(self) -> str | None:
        for cfg in self.plan.provider_config.values():
            if isinstance(cfg, dict) and cfg.get("name") == self.schema.provider:
                region = _Plan.const(cfg.get("expressions") or {}, self.schema.region_key)
                if isinstance(region, str):
                    return region
        return None

    # -- routing
    def default_route_target(self, rt_addr: str) -> str | None:
        """Where a route table's 0.0.0.0/0 goes: an inline route, else a standalone route resource."""
        spec = self._spec(rt_addr)
        for item in self.plan.blocks(rt_addr, spec.blocks.get("routes", "")) if spec.blocks.get("routes") else []:
            if _Plan.const(item, spec.rule.get("dest", "")) == DEFAULT_ROUTE:
                for key in spec.rule.get("targets", ()):
                    for target in self.plan.resolve(item.get(key), self.plan.module_of.get(strip_index(rt_addr), "")):
                        if self.kind_of.get(target) in ("igw", "nat"):
                            return target
        for route in self.of_kind("route"):
            rspec = self._spec(route)
            if self.first_ref(route, "route_table") != rt_addr or self.plan.known(route, rspec.rule.get("dest", "")) != DEFAULT_ROUTE:
                continue
            for key in rspec.rule.get("targets", ()):
                for target in self.plan.refs(route, key):
                    if self.kind_of.get(target) in ("igw", "nat"):
                        return target
        return None

    def build(self) -> Graph:
        g = self.graph
        region = self.region
        vpcs = self.of_kind("vpc")
        if not (vpcs or self.of_kind("instance") or self.of_kind("nat")):
            return g
        if region:
            g.regions.append(region)

        # Structure: parent links from configuration references.
        subnet_vpc = {s: self.first_ref(s, "vpc", "vpc") for s in self.of_kind("subnet")}
        igw_vpc = {i: self.first_ref(i, "vpc", "vpc") for i in self.of_kind("igw")}
        rt_vpc = {r: self.first_ref(r, "vpc", "vpc") for r in self.of_kind("route_table")}
        subnet_rt: dict[str, str] = {}
        for assoc in self.of_kind("association"):
            sn, rt = self.first_ref(assoc, "subnet", "subnet"), self.first_ref(assoc, "route_table", "route_table")
            if sn and rt:
                subnet_rt.setdefault(sn, rt)
        default_rt = {rt_vpc[r]: r for r in rt_vpc if self._spec(r).default and rt_vpc[r]}
        rt_target = {r: self.default_route_target(r) for r in rt_vpc}

        for vid in sorted(vpcs, key=lambda v: (str(self.plan.known(v, self._spec(v).fields.get("cidr", "")) or ""), v)):
            g.add(self.node(vid, "vpc", default=False))
            for igw, parent in igw_vpc.items():
                if parent == vid:
                    g.add(self.node(igw, "igw", label="IGW", parent=vid))

        nats = self.of_kind("nat")
        nat_subnet = {n: self.first_ref(n, "subnet", "subnet") for n in nats}
        nat_vpc = {n: subnet_vpc.get(nat_subnet[n] or "") for n in nats}

        subnets = self.of_kind("subnet")
        for sid in sorted(subnets, key=lambda s: (str(self.plan.known(s, self._spec(s).fields.get("cidr", "")) or ""), s)):
            vpc = subnet_vpc.get(sid)
            rt = subnet_rt.get(sid) or default_rt.get(vpc or "")
            target = rt_target.get(rt) if rt else None
            exposure = "isolated" if not target else ("public" if self.kind_of.get(target) == "igw" else "private")
            g.add(self.node(sid, "subnet", parent=vpc, exposure=exposure, default_route=target, route_table=rt))
            if target:
                g.add_edge({"kind": "route", "from": sid, "to": target, "label": DEFAULT_ROUTE})

        for nid in sorted(nats):
            vpc = nat_vpc.get(nid)
            g.add(self.node(nid, "nat", label=self.label(nid), parent=vpc, subnet=nat_subnet.get(nid), state=None))
            for igw, parent in igw_vpc.items():
                if vpc and parent == vpc:
                    g.add_edge({"kind": "uplink", "from": nid, "to": igw})
        for igw in igw_vpc:
            if igw in g.nodes:
                g.add_edge({"kind": "uplink", "from": igw, "to": "internet"})

        roles = self.manifest.topology.roles
        instances = self.of_kind("instance")
        inst_groups: dict[str, list[str]] = {}
        for iid in sorted(instances, key=lambda i: (self.label(i), i)):
            subnet = self.first_ref(iid, "subnet", "subnet")
            parent = subnet if subnet in g.nodes else None
            node = self.node(iid, "instance", parent=parent, role=roles.get(self.label(iid)), state=None)
            if parent is None:
                g.unknown.append({"kind": "instance", "id": iid, "label": node["label"], "region": region,
                                  "reason": "Planned instance references no subnet the plan declares"})
                continue
            if node.get("az") is None and subnet:
                node["az"] = g.nodes[subnet].get("az")
            g.add(node)
            key = self._spec(iid).refs.get("groups")
            inst_groups[iid] = [a for a in (self.plan.refs(iid, key) if key else []) if self.kind_of.get(a) == "security_group"]

        # Elastic IPs: attached to the instance that names them, or to the NAT that consumes them.
        eip_nat = {}
        for nid in nats:
            eip = self.first_ref(nid, "eip", "eip")
            if eip:
                eip_nat[eip] = nid
        for eid in sorted(self.of_kind("eip"), key=lambda e: (self.label(e), e)):
            inst = self.first_ref(eid, "instance", "instance")
            attached_to = inst if inst in g.nodes else eip_nat.get(eid) if eip_nat.get(eid) in g.nodes else None
            g.add(self.node(eid, "eip", parent=None, attached_to=attached_to, attached=attached_to is not None))

        # Security rules -> allow edges onto the instances holding the group.
        members: dict[str, list[str]] = {}
        for iid, groups in inst_groups.items():
            for sg in groups:
                members.setdefault(sg, []).append(iid)
        for sg in self.of_kind("security_group"):
            spec = self._spec(sg)
            for item in self.plan.blocks(sg, spec.blocks.get("ingress", "")) if spec.blocks.get("ingress") else []:
                rule = self._rule(item, spec.rule, sg)
                self._allow(sg, rule, members)
        for rule_addr in self.of_kind("ingress_rule"):
            spec = self._spec(rule_addr)
            sg = self.first_ref(rule_addr, "group", "security_group")
            if not sg:
                continue
            item = {k: {"constant_value": self.plan.known(rule_addr, k)} for k in spec.rule.values() if isinstance(k, str)}
            rule = self._rule(item, spec.rule, rule_addr)
            src_group = self.first_ref(rule_addr, "source_group", "security_group")
            if src_group:
                rule["sources"] = [src_group]
            elif not rule["sources"]:
                key = spec.refs.get("source")
                rule["sources"] = [a for a in (self.plan.refs(rule_addr, key) if key else []) if a in g.nodes]
            self._allow(sg, rule, members, rule_addr)
        return g

    def _rule(self, item: dict[str, Any], keys: dict[str, Any], owner: str) -> dict[str, Any]:
        module = self.plan.module_of.get(strip_index(owner), "")
        proto = _Plan.const(item, keys.get("proto", ""))
        proto = "all" if proto is None or str(proto) in self.schema.any_protocol else str(proto)
        frm, to = _Plan.const(item, keys.get("from", "")), _Plan.const(item, keys.get("to", ""))
        if proto == "all":
            frm = to = None
        sources: list[Any] = []
        cidr = _Plan.const(item, keys.get("cidr", "")) if keys.get("cidr") else None
        if isinstance(cidr, str):
            sources.append(cidr)
        cidrs = _Plan.const(item, keys.get("cidrs", "")) if keys.get("cidrs") else None
        if isinstance(cidrs, list):
            sources.extend(c for c in cidrs if isinstance(c, str))
        if keys.get("groups"):
            sources.extend(self.plan.resolve(item.get(keys["groups"]), module))
        if keys.get("cidr") and not isinstance(cidr, str):
            # Unknown until apply (e.g. "${aws_eip.nat.public_ip}/32"): point at the resource it names.
            sources.extend(a for a in self.plan.resolve(item.get(keys["cidr"]), module) if a in self.graph.nodes)
        return {"proto": proto, "from": frm, "to": to, "sources": sources}

    def _allow(self, sg: str, rule: dict[str, Any], members: dict[str, list[str]], rule_addr: str | None = None) -> None:
        targets = members.get(sg) or []
        if not targets:
            return
        group = {"id": sg, "name": self.plan.known(sg, self._spec(sg).fields.get("name", "")) or self.label(sg)}
        label = rule_label(rule)
        for source in rule["sources"]:
            for inst in targets:
                edge: dict[str, Any] = {"kind": "allow", "from": source, "to": inst, "label": label, "group": group}
                if source in members:
                    edge["source_nodes"] = list(members[source])
                elif source in self.graph.nodes:
                    edge["source_nodes"] = [source]
                if rule_addr:
                    edge["rule"] = rule_addr
                self.graph.add_edge(edge)


def build_plan_graph(manifest: Manifest, show: dict[str, Any] | None, sources: SourceIndex | None = None) -> dict[str, Any]:
    """Nodes and edges for `manifest` from a `tofu show -json` plan document.

    Same envelope as `topology.build_graph`; `enrolment` is always `{}` (nothing is running).
    Unknown-until-apply attributes are None. Never raises on odd plan data — it degrades to
    `unknown` entries, and a plan holding nothing drawable yields `nodes: []`.
    """
    schema = SCHEMAS.get(manifest.provider)
    if schema is None or not isinstance(show, dict):
        return assemble(Graph(manifest), None)
    builder = _PlanGraph(manifest, schema, _Plan(show), sources)
    return assemble(builder.build(), None)


# ---------------------------------------------------------------------- source lines
RESOURCE_RE = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{')
HEREDOC_RE = re.compile(r'<<-?\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s*$')
STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
REF_RE = re.compile(r'^\s*([a-z_]+)\s*=\s*\[?\s*([a-z][a-z0-9_]*\.[A-Za-z0-9_-]+)\.[a-z_]+\s*\]?\s*$')


@dataclass(frozen=True)
class SourceEntry:
    type: str
    name: str
    path: str
    line: int
    name_tag: str | None
    refs: dict[str, str]

    @property
    def address(self) -> str:
        return f"{self.type}.{self.name}"

    def to_api(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line}


class SourceIndex:
    """`resource "<type>" "<name>"` blocks in the use case's terraform dir: file (relative to the
    repo root, the form `GET /usecases/{id}/code?path=` accepts), line, the block's name tag and
    its simple `attr = type.name.attr` references. Root module only."""

    def __init__(self, entries: list[SourceEntry]) -> None:
        self.entries = entries
        self.by_address: dict[str, SourceEntry] = {}
        for e in entries:
            self.by_address.setdefault(e.address, e)

    @classmethod
    def scan(cls, checkout: Path, terraform_dir: str, *, name_tag: str = "Name") -> SourceIndex:
        entries: list[SourceEntry] = []
        root = checkout / terraform_dir
        if not root.is_dir():
            return cls(entries)
        name_re = re.compile(r"\b" + re.escape(name_tag) + r'\s*=\s*"([^"]*)"')
        for tf in sorted(root.glob("*.tf")):
            try:
                lines = tf.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            rel = (Path(terraform_dir) / tf.name).as_posix()
            for rtype, rname, start, body in cls._blocks(lines):
                found = name_re.search("\n".join(body))
                refs: dict[str, str] = {}
                for raw in body:
                    m = REF_RE.match(raw)
                    if m:
                        refs.setdefault(m.group(1), m.group(2))
                entries.append(SourceEntry(rtype, rname, rel, start, found.group(1) if found else None, refs))
        return cls(entries)

    @staticmethod
    def _blocks(lines: list[str]) -> list[tuple[str, str, int, list[str]]]:
        """(type, name, 1-based line, body lines) per resource block; heredocs are skipped so
        their braces do not count, and string contents are ignored for brace depth."""
        out: list[tuple[str, str, int, list[str]]] = []
        i = 0
        while i < len(lines):
            m = RESOURCE_RE.match(lines[i])
            if not m:
                i += 1
                continue
            start, depth, body, heredoc = i + 1, 0, [], None
            j = i
            while j < len(lines):
                line = lines[j]
                if heredoc is not None:
                    if line.strip() == heredoc:
                        heredoc = None
                    j += 1
                    continue
                body.append(line)
                hd = HEREDOC_RE.search(line)
                if hd:
                    heredoc = hd.group(1)
                stripped = STRING_RE.sub('""', line).split("#", 1)[0].split("//", 1)[0]
                depth += stripped.count("{") - stripped.count("}")
                j += 1
                if depth <= 0:
                    break
            out.append((m.group(1), m.group(2), start, body))
            i = j
        return out

    def source(self, address: str) -> dict[str, Any] | None:
        entry = self.by_address.get(strip_index(address))
        return entry.to_api() if entry else None

    def attach_live(self, nodes: list[dict[str, Any]], schema: PlanSchema, state_addrs: list[str] | None) -> None:
        """Give deployed nodes a `source` (and `address`) when exactly one resource block of their
        kind carries their name tag — restricted to blocks that are in state when the state list
        is known. Gateways have no name in the inventory: they match through their VPC."""
        kinds = schema.kinds()
        in_state = {strip_index(a) for a in state_addrs} if state_addrs else None
        candidates = [e for e in self.entries if kinds.get(e.type) and (in_state is None or e.address in in_state)]
        matched: dict[str, SourceEntry] = {}
        for node in nodes:
            kind = node.get("kind")
            if kind not in STRUCTURE_KINDS or kind == "igw":
                continue
            detail = node.get("detail") or {}
            name = (detail.get(schema.tags_attr) or {}).get(schema.name_tag) or detail.get("name") or node.get("label")
            hits = [e for e in candidates if kinds[e.type] == kind and e.name_tag == name]
            if name and len(hits) == 1:
                matched[node["id"]] = hits[0]
        for node in nodes:
            if node.get("kind") != "igw":
                continue
            vpc = matched.get(node.get("parent") or "")
            if vpc is None:
                continue
            hits = [e for e in candidates if kinds[e.type] == "igw" and e.refs.get(schema.types[e.type].refs.get("vpc", "")) == vpc.address]
            if len(hits) == 1:
                matched[node["id"]] = hits[0]
        for node in nodes:
            entry = matched.get(node["id"])
            if entry is not None:
                node["source"] = entry.to_api()
                node["address"] = entry.address
