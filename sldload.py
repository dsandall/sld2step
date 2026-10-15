"""Load decoded Parasolid node lists out of a SolidWorks .SLDPRT.

Uses sldprt2xt for container/carve/decode.  Native parts keep their bodies in
Contents/Config-N-Partition; imported (vendor) parts keep an empty partition and
the bodies live as separate transmits in Config-N-FeatureBodies/LocalBodies.

Configurations: a part file stores geometry for the configuration active when
it was saved (sometimes one or two more, e.g. a sheet-metal flat pattern).  The
id -> name map lives in ``swXmlContents/Features`` (``Features__ZLB``, zlib, in
older files): each ``swModel`` carries ``swConfigurationId`` (the N of
``Config-N-*``) and ``swConfigurationName``; ``swConfigurationList`` flags the
most recent (active) one.  ``docProps/Config-N-Properties.xml`` carries the
custom properties but no name.
"""
from __future__ import annotations
import hashlib
import re
import zlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from sldprt2xt.container import open_container
from sldprt2xt.carve import carve
from sldprt2xt.parasolid import read_binary_transmit, BINARY_MAGIC, TransmitError, extract_transmits
from sldprt2xt.builtin import base_schema
from sldprt2xt.xt.binary import parse_nodes

NS = "{http://www.solidworks.com/sw2003/schema}"
_CFG_RE = re.compile(r"Config-(\d+)-")


@dataclass
class PartConfigs:
    names: dict = field(default_factory=dict)   # config id (str) -> name
    active: str | None = None                   # id of the most recently active configuration

    def id_of(self, name: str) -> str | None:
        return next((i for i, n in self.names.items() if n == name), None)


def config_of_stream(stream: str) -> str | None:
    """Config id a transmit came from ('Contents/Config-2276-Partition' -> '2276')."""
    m = _CFG_RE.search(stream)
    return m.group(1) if m else None


def read_configs(c) -> PartConfigs:
    """Configuration id -> name map of an opened part container."""
    blobs = {b.name: b.data for b in c.blobs}
    xml = blobs.get("swXmlContents/Features")
    if xml is None and "swXmlContents/Features__ZLB" in blobs:
        d = blobs["swXmlContents/Features__ZLB"]
        i = d.find(b"x\x01")
        try:
            xml = zlib.decompress(d[i:]) if i >= 0 else None
        except zlib.error:
            xml = None
    pc = PartConfigs()
    if not xml:
        return pc
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return pc
    for m in root.iter(NS + "swModel"):
        if m.get("swConfigurationId") is not None:
            pc.names.setdefault(m.get("swConfigurationId"), m.get("swConfigurationName", ""))
    for cf in root.iter(NS + "swConfiguration"):
        if cf.get("swMostRecentConfiguration") == "YES":
            pc.active = cf.get("swID")
            if pc.active is not None and pc.active not in pc.names:
                pc.names[pc.active] = cf.get("swName", "")
    return pc


def part_configs(path) -> PartConfigs:
    return read_configs(open_container(Path(path)))


def _decode_transmit(t):
    header = {}
    nodes, layouts = parse_nodes(t.data, t.body_offset, base_schema(), current=None, header=header)
    return nodes


def select_config(out, configs: PartConfigs, config: str | None):
    """When bodies of several configurations are stored (sheet metal keeps its
    flat pattern), keep only the requested one, else the active one."""
    ids = {config_of_stream(s) for s, _, _ in out}
    if len(ids) < 2:
        return out
    want = configs.id_of(config) if config else None
    for pick in (want, configs.active):
        if pick in ids:
            return [x for x in out if config_of_stream(x[0]) == pick]
    return out


def load_transmits(path, config: str | None = None):
    """Return [(source_name, transmit, nodes)] for every body-carrying transmit.

    ``config`` (a configuration name) selects among several stored
    configurations; ``stored_config_ids`` tells which one was returned.
    """
    configs, out = load_all(path)
    return select_config(out, configs, config)


def load_all(path):
    """(PartConfigs, every body-carrying transmit of every stored configuration)."""
    c = open_container(Path(path))
    configs = read_configs(c)
    out = []
    for stream, ts in extract_transmits(c).items():
        if "GhostPartition" in stream:
            continue
        for t in ts:
            if t.guise == "partition":
                nodes = _decode_transmit(t)
                if any(n.name == "BODY" for n in nodes):
                    out.append((stream, t, nodes))
    if out:
        return configs, out
    seen = set()
    for b in c.blobs:
        if not b.name.endswith("FeatureBodies/LocalBodies"):
            continue
        for r in carve(b.data, scan_bare=False):
            if not r.data.startswith(BINARY_MAGIC):
                continue
            h = hashlib.sha1(r.data).hexdigest()
            if h in seen:
                continue
            seen.add(h)
            try:
                t = read_binary_transmit(r.data)
            except TransmitError:
                continue
            nodes = _decode_transmit(t)
            if any(n.name == "BODY" for n in nodes):
                out.append((f"{b.name}@{r.offset}", t, nodes))
    return configs, out


def stored_config_ids(transmits) -> list[str]:
    """Config ids whose stored bodies ``load_transmits`` returned."""
    return sorted({i for s, _, _ in transmits if (i := config_of_stream(s)) is not None})
