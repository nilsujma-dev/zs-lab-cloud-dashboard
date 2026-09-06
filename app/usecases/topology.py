"""Topology graph for a use case (SPEC v1.2): structure from the cloud, meaning from the manifest.

`build_graph` is pure: it takes the provider's cached inventory (the v1.1 shape), the manifest
and the last status-probe output, and returns provider-neutral nodes and edges. Nothing here
calls a cloud, and nothing in it recognises a provider's id formats: gateways and NATs are
known by the inventory's own cross-references, so only a node's `detail` is provider-shaped.

`Graph` (nodes, edges, the declared-flow resolver, enrolment) and `assemble` (the response
envelope) are shared with `plan_graph.py`, which fills the same vocabulary from a `tofu plan`
instead of the cloud (SPEC v1.4): one drawing, two registers.

Node kinds:   internet · vpc · subnet · instance · nat · igw · eip
Edge kinds:   route (subnet → gateway, its default route) · uplink (nat → igw, igw → internet)
              · allow (security-rule source → instance) · flow / blocked (declared in the manifest)
"""

from __future__ import annotations

from typing import Any

from app.usecases.manifest import INTERNET, Link, Manifest

DEFAULT_ROUTE = "0.0.0.0/0"
NODE_KINDS = ("internet", "vpc", "subnet", "instance", "nat", "igw", "eip")
EDGE_KINDS = ("route", "uplink", "allow", "flow", "blocked")
AUTHENTICATED = "ZPN_STATUS_AUTHENTICATED"


def _tagged(resource: dict[str, Any], tags: dict[str, str]) -> bool:
    """A resource carries the use case's tags when every manifest tag is present with that value."""
    have = resource.get("tags") or {}
    return bool(tags) and all(have.get(k) == v for k, v in tags.items())


def _default_route_target(subnet: dict[str, Any], route_tables: dict[str, dict[str, Any]]) -> str | None:
    """The default route's target for a subnet: the inventory's `default_route`, else derived from
    its route table (older inventories), else None."""
    if "default_route" in subnet:
        return subnet.get("default_route")
    rt = route_tables.get(subnet.get("route_table") or "")
    for route in (rt or {}).get("routes", []) or []:
        if route.get("dest") == DEFAULT_ROUTE:
            return route.get("target")
    return None


def _exposure(target: str | None, subnet: dict[str, Any], igws: set[str], nats: set[str]) -> str:
    """public: default route to an internet gateway; private: to a NAT (or any other egress
    device); isolated: no default route at all. The inventory's own `public` flag is honoured."""
    if not target:
        return "isolated"
    if target in igws or subnet.get("public") is True:
        return "public"
    return "private"


def rule_label(rule: dict[str, Any]) -> str:
    """`{proto, from, to}` -> `tcp/443`, `udp/1024-2048`, `icmp`, `all`."""
    proto = str(rule.get("proto") or "all")
    frm, to = rule.get("from"), rule.get("to")
    if proto == "all":
        return "all"
    if frm is None or to is None or (frm, to) == (-1, -1):
        return proto
    return f"{proto}/{frm}" if frm == to else f"{proto}/{frm}-{to}"


def _parse_components(output: Any) -> list[dict[str, Any]]:
    """Enrolment components from a status probe's output, tolerant of two shapes:
    the PSE lab's `{"components": [{id, label, authenticated, control_channel, private_ip}, …]}`
    and a mapping of `{name: {status|authenticated|enrolled, …}}`."""
    if not isinstance(output, dict):
        return []
    raw = output.get("components")
    comps: list[dict[str, Any]] = []
    if isinstance(raw, list):
        comps = [c for c in raw if isinstance(c, dict)]
    elif isinstance(raw, dict):
        comps = [{"id": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    else:
        for key, value in output.items():
            if isinstance(value, dict) and any(f in value for f in ("authenticated", "status", "control_channel", "enrolled")):
                comps.append({"id": key, **value})
    return comps


def _authenticated(component: dict[str, Any]) -> bool:
    if isinstance(component.get("authenticated"), bool):
        return component["authenticated"]
    if isinstance(component.get("enrolled"), bool):
        return component["enrolled"]
    status = component.get("control_channel") or component.get("status")
    return str(status).upper() == AUTHENTICATED if status else False


class Graph:
    def __init__(self, manifest: Manifest) -> None:
        self.manifest = manifest
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, Any]] = []
        self.unknown: list[dict[str, Any]] = []
        self.regions: list[str] = []
        self._edge_keys: set[tuple[Any, ...]] = set()

    # ------------------------------------------------------------------ nodes
    def add(self, node: dict[str, Any]) -> dict[str, Any]:
        existing = self.nodes.get(node["id"])
        if existing is not None:
            if node.get("tagged") and not existing.get("tagged"):
                existing["tagged"] = True
            return existing
        self.nodes[node["id"]] = node
        return node

    def add_edge(self, edge: dict[str, Any]) -> None:
        key = (edge["kind"], edge["from"], edge["to"], edge.get("label"), tuple(edge.get("via") or ()))
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append(edge)

    def by_kind(self, kind: str) -> list[dict[str, Any]]:
        return [n for n in self.nodes.values() if n["kind"] == kind]

    # ------------------------------------------------------------------ one region
    def add_region(self, region: dict[str, Any]) -> None:
        tags = self.manifest.tags
        region_name = region.get("region")
        vpcs = {v["id"]: v for v in region.get("vpcs", []) or [] if v.get("id")}
        igw_ids = {v["igw"] for v in vpcs.values() if v.get("igw")}
        subnets: dict[str, dict[str, Any]] = {}
        subnet_vpc: dict[str, str] = {}
        route_tables: dict[str, dict[str, Any]] = {}
        for vpc in vpcs.values():
            for rt in vpc.get("route_tables", []) or []:
                if rt.get("id"):
                    route_tables[rt["id"]] = rt
            for sn in vpc.get("subnets", []) or []:
                if sn.get("id"):
                    subnets[sn["id"]] = sn
                    subnet_vpc[sn["id"]] = vpc["id"]
        instances = [i for i in region.get("instances", []) or [] if i.get("id") and i.get("state") != "terminated"]
        nats = {n["id"]: n for n in region.get("nat_gateways", []) or [] if n.get("id")}

        tagged_vpcs = {vid for vid, v in vpcs.items() if _tagged(v, tags)}
        tagged_instances = [i for i in instances if _tagged(i, tags)]
        tagged_nats = {nid for nid, n in nats.items() if _tagged(n, tags)}
        # Reachable-only: subnets of tagged VPCs, the VPC/subnet holding a tagged instance or NAT.
        want_vpcs = set(tagged_vpcs)
        want_subnets = {sid for sid, vid in subnet_vpc.items() if vid in tagged_vpcs}
        for inst in tagged_instances:
            if inst.get("vpc") in vpcs:
                want_vpcs.add(inst["vpc"])
            if inst.get("subnet") in subnets:
                want_subnets.add(inst["subnet"])
        for nid in tagged_nats:
            nat = nats[nid]
            if nat.get("vpc") in vpcs:
                want_vpcs.add(nat["vpc"])
            if nat.get("subnet") in subnets:
                want_subnets.add(nat["subnet"])
        # NATs are structure: every NAT inside a wanted VPC is drawn, tagged or not.
        want_nats = set(tagged_nats) | {nid for nid, n in nats.items() if n.get("vpc") in want_vpcs}
        for nid in want_nats:
            if nats[nid].get("subnet") in subnets:
                want_subnets.add(nats[nid]["subnet"])

        if not (want_vpcs or tagged_instances or want_nats):
            return
        if region_name and region_name not in self.regions:
            self.regions.append(region_name)

        for vid in sorted(want_vpcs, key=lambda v: (vpcs[v].get("cidr") or "", v)):
            vpc = vpcs[vid]
            self.add({
                "id": vid, "kind": "vpc", "label": vpc.get("name") or vid, "cidr": vpc.get("cidr"), "parent": None,
                "region": region_name, "tagged": vid in tagged_vpcs, "default": bool(vpc.get("default")),
                "detail": {k: v for k, v in vpc.items() if k not in ("subnets", "route_tables")},
            })
            if vpc.get("igw"):
                self.add({"id": vpc["igw"], "kind": "igw", "label": "IGW", "parent": vid, "region": region_name, "tagged": vid in tagged_vpcs,
                          "detail": {"id": vpc["igw"], "vpc": vid}})

        for sid in sorted(want_subnets, key=lambda s: (subnets[s].get("cidr") or "", s)):
            sn = subnets[sid]
            target = _default_route_target(sn, route_tables)
            self.add({
                "id": sid, "kind": "subnet", "label": sn.get("name") or sid, "cidr": sn.get("cidr"), "parent": subnet_vpc.get(sid),
                "az": sn.get("az"), "exposure": _exposure(target, sn, igw_ids, want_nats), "default_route": target, "region": region_name,
                "tagged": _tagged(sn, tags) or subnet_vpc.get(sid) in tagged_vpcs, "detail": dict(sn),
            })
            if target and (target in want_nats or target in igw_ids and target in self.nodes):
                self.add_edge({"kind": "route", "from": sid, "to": target, "label": DEFAULT_ROUTE})

        for nid in sorted(want_nats):
            nat = nats[nid]
            self.add({
                "id": nid, "kind": "nat", "label": nat.get("name") or "NAT", "parent": nat.get("vpc") if nat.get("vpc") in self.nodes else None,
                "subnet": nat.get("subnet") if nat.get("subnet") in self.nodes else None, "state": nat.get("state"),
                "public_ip": nat.get("public_ip"), "private_ip": nat.get("private_ip"), "region": region_name,
                "tagged": nid in tagged_nats, "detail": dict(nat),
            })
            igw = (vpcs.get(nat.get("vpc") or "") or {}).get("igw")
            if igw and igw in self.nodes:
                self.add_edge({"kind": "uplink", "from": nid, "to": igw})

        for igw in self.by_kind("igw"):
            if igw.get("region") == region_name:
                self.add_edge({"kind": "uplink", "from": igw["id"], "to": INTERNET})

        roles = self.manifest.topology.roles
        for inst in sorted(tagged_instances, key=lambda i: (i.get("name") or "", i["id"])):
            parent = inst.get("subnet") if inst.get("subnet") in self.nodes else (inst.get("vpc") if inst.get("vpc") in self.nodes else None)
            node = {
                "id": inst["id"], "kind": "instance", "label": inst.get("name") or inst["id"], "parent": parent,
                "role": roles.get(inst.get("name") or ""), "type": inst.get("type"), "state": inst.get("state"),
                "private_ip": inst.get("private_ip"), "public_ip": inst.get("public_ip"), "az": inst.get("az"),
                "region": region_name, "tagged": True, "detail": dict(inst),
            }
            if parent is None:
                self.unknown.append({"kind": "instance", "id": inst["id"], "label": node["label"], "region": region_name,
                                     "reason": "Tagged instance is not in any subnet or VPC the inventory describes"})
                continue
            self.add(node)

        # Elastic IPs: tagged, or bound to a node that is drawn.
        for eip in region.get("eips", []) or []:
            assoc = eip.get("association") or {}
            target = assoc.get("id") or eip.get("instance")
            attached_to = target if target in self.nodes else None
            if not (_tagged(eip, tags) or attached_to):
                continue
            eid = eip.get("allocation_id") or eip.get("ip")
            if not eid:
                continue
            self.add({
                "id": eid, "kind": "eip", "label": eip.get("ip") or eid, "parent": None, "attached_to": attached_to,
                "attached": bool(eip.get("attached")), "region": region_name, "tagged": _tagged(eip, tags), "detail": dict(eip),
            })

        # Security-rule ingress -> the instances the group is attached to.
        group_members: dict[str, list[str]] = {}
        for sg in region.get("security_groups", []) or []:
            group_members[sg.get("id") or ""] = [i for i in sg.get("attached_to", []) or [] if i in self.nodes]
        for sg in region.get("security_groups", []) or []:
            members = group_members.get(sg.get("id") or "", [])
            if not members:
                continue
            for rule in sg.get("ingress", []) or []:
                source = rule.get("source")
                if not source:
                    continue
                for inst_id in members:
                    edge: dict[str, Any] = {
                        "kind": "allow", "from": source, "to": inst_id, "label": rule_label(rule),
                        "group": {"id": sg.get("id"), "name": sg.get("name")},
                    }
                    source_nodes = group_members.get(source)
                    if source_nodes:
                        edge["source_nodes"] = list(source_nodes)
                    self.add_edge(edge)

    # ------------------------------------------------------------------ declared flows
    def _endpoints(self, name: str) -> list[str]:
        if name == INTERNET:
            return [INTERNET]
        return [n["id"] for n in self.by_kind("instance") if n["label"] == name]

    def _hop(self, hop: str, source_id: str) -> str | None:
        if hop == "internet":
            return INTERNET
        source = self.nodes.get(source_id) or {}
        vpc_id = source.get("parent")
        while vpc_id and self.nodes.get(vpc_id, {}).get("kind") != "vpc":
            vpc_id = self.nodes[vpc_id].get("parent")
        if hop == "nat":
            for nat in self.by_kind("nat"):
                if nat.get("parent") == vpc_id:
                    return nat["id"]
        if hop == "igw":
            for igw in self.by_kind("igw"):
                if igw.get("parent") == vpc_id:
                    return igw["id"]
        return None

    def add_links(self, links: tuple[Link, ...], kind: str) -> None:
        for link in links:
            sources, targets = self._endpoints(link.from_), self._endpoints(link.to)
            missing = [name for name, found in ((link.from_, sources), (link.to, targets)) if not found]
            if missing:
                self.unknown.append({
                    "kind": kind, "from": link.from_, "to": link.to, "label": link.label,
                    "reason": "No instance named " + " or ".join(repr(m) for m in missing) + " in the inventory",
                })
                continue
            for src in sources:
                via: list[str] = []
                via_missing: list[str] = []
                for hop in link.via:
                    resolved = self._hop(hop, src)
                    (via if resolved else via_missing).append(resolved or hop)
                for dst in targets:
                    if src == dst:
                        continue
                    edge: dict[str, Any] = {"kind": kind, "from": src, "to": dst, "via": via, "label": link.label, "declared": True}
                    if via_missing:
                        edge["via_missing"] = via_missing
                    self.add_edge(edge)

    # ------------------------------------------------------------------ enrolment
    def enrolment(self, status_output: Any) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        instances = self.by_kind("instance")
        by_ip = {n["private_ip"]: n["id"] for n in instances if n.get("private_ip")}
        claimed: set[str] = set()
        pending: list[dict[str, Any]] = []
        for comp in _parse_components(status_output):
            ip = comp.get("private_ip")
            inst_id = by_ip.get(ip) if ip else None
            if inst_id and inst_id not in claimed:
                claimed.add(inst_id)
                out[inst_id] = self._enrolment_entry(comp, "private_ip")
            else:
                pending.append(comp)
        for comp in pending:
            cid = str(comp.get("id") or comp.get("label") or "").strip()
            candidates = [
                n["id"] for n in instances
                if n["id"] not in claimed and n.get("role") and (cid == n["role"] or cid.startswith((n["role"] + "_", n["role"] + "-")))
            ]
            if len(candidates) == 1:
                claimed.add(candidates[0])
                out[candidates[0]] = self._enrolment_entry(comp, "role")
            else:
                why = "matches no instance by private IP or role" if not candidates else f"matches {len(candidates)} instances by role; ambiguous"
                self.unknown.append({"kind": "component", "id": cid or None, "label": comp.get("label") or cid or None,
                                     "authenticated": _authenticated(comp), "reason": f"Enrolment component {why}"})
        return out

    @staticmethod
    def _enrolment_entry(comp: dict[str, Any], matched_by: str) -> dict[str, Any]:
        return {
            "authenticated": _authenticated(comp),
            "label": comp.get("label") or comp.get("id"),
            "component": comp.get("id"),
            "status": comp.get("control_channel") or comp.get("status"),
            "version": comp.get("version"),
            "matched_by": matched_by,
        }


def build_graph(manifest: Manifest, inventory: dict[str, Any] | None, status: Any) -> dict[str, Any]:
    """Nodes and edges for `manifest` from a cached inventory and the last status-probe output.

    Returns {usecase, provider, region, regions, nodes, edges, enrolment, unknown, counts, declared}.
    `nodes` is empty when the inventory holds nothing carrying the manifest's tags; the caller
    decides what `reason` to attach. Never raises on odd inventory data — it degrades to `unknown`.
    """
    graph = Graph(manifest)
    for region in (inventory or {}).get("regions", []) or []:
        if isinstance(region, dict):
            graph.add_region(region)
    return assemble(graph, status)


def assemble(graph: Graph, status: Any) -> dict[str, Any]:
    """Finish a graph either builder filled: put the internet node first, resolve the manifest's
    declared flows and blocked pairs against the nodes present, map enrolment, and wrap it in
    the response envelope. An empty graph stays empty (no internet node, no flows)."""
    manifest = graph.manifest
    if graph.nodes:
        graph.nodes = {INTERNET: {"id": INTERNET, "kind": "internet", "label": "Internet", "parent": None}, **graph.nodes}
        graph.add_links(manifest.topology.flows, "flow")
        graph.add_links(manifest.topology.blocked, "blocked")
    enrolment = graph.enrolment(status) if graph.nodes else {}
    nodes = list(graph.nodes.values())
    return {
        "usecase": manifest.id,
        "provider": manifest.provider,
        "region": graph.regions[0] if graph.regions else None,
        "regions": list(graph.regions),
        "nodes": nodes,
        "edges": graph.edges,
        "enrolment": enrolment,
        "unknown": graph.unknown,
        "counts": {kind: sum(1 for n in nodes if n["kind"] == kind) for kind in NODE_KINDS},
        "declared": manifest.topology.to_api(),
    }
