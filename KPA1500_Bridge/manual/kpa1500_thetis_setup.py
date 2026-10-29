#!/usr/bin/env python3
"""
kpa1500_thetis_setup.py  -  puts the KPA1500 containers into Thetis
=====================================================================
Concept, design and on-air testing: KW4EX.

What it does (Thetis must be CLOSED while it runs):
  1. Finds your Thetis settings database (the active one).
  2. Saves a backup copy of it first (in the "thetis_backups" folder next to this file).
  3. Makes sure Thetis has the two Multi Meter I/O entries the bridge uses:
        - a UDP "In" entry on 127.0.0.1:50100 (XML)      -> amp readings into Thetis
        - a UDP entry sending to 127.0.0.1:50101 (raw)   -> button presses to the bridge
     If you already made them, they are used as they are.
  4. Adds (or replaces) two floating windows ("containers"):
        KPA1500 Telemetry - meters for power, SWR, drive, LP-100A, temperature ...
        KPA1500 Controls  - buttons that light up when a function is on
     Both show "BRIDGE NOT RUNNING" if the bridge stops.

Run it with:   py kpa1500_thetis_setup.py         (questions, then it does the work)
Other options: --check      only report what it finds, change nothing
               --restore    put back the most recent backup
               --remove     take the KPA1500 containers out again
               --yes        answer yes to everything (no questions)
               --close-thetis  close a running Thetis without asking (normal close, saves settings)
               --no-lp100   leave out the LP-100A meters
               --settings F the bridge settings file to read the button order from
                            (default: kpa1500_settings.json next to this file; the
                            order is set in the bridge panel: Settings > Thetis Controls buttons)
               --db PATH    use this database.xml instead of finding it

Needs Python 3.8 or newer. Standard library only.
Thetis 2.10.3.12 or newer (containers with macro buttons, LED scripts).
"""
import argparse
import copy
import base64
import datetime
import gzip
import hashlib
import html
import json
import os
import re
import shutil
import struct
import time
import subprocess
import sys
import uuid

SETUP_VERSION = "2.0"
IN_PORT, OUT_PORT, OUT_LISTEN = 50100, 50101, 50102
CTRL_NAME, TELE_NAME = "KPA1500 Controls - KW4EX", "KPA1500 Telemetry - KW4EX"
KNOB_NAME = "KPA1500 Knob - KW4EX"

# ---- .NET BinaryFormatter (MS-NRBF) reader / writer --------------------------

import io
import copy


def r_lps(f):
    n = 0; shift = 0
    while True:
        b = f.read(1)[0]
        n |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            break
    return f.read(n).decode('utf-8')


def w_lps(s):
    b = s.encode('utf-8'); n = len(b); out = bytearray()
    while True:
        x = n & 0x7F; n >>= 7
        if n:
            out.append(x | 0x80)
        else:
            out.append(x); break
    return bytes(out) + b


PRIM = {1: '<?', 2: '<B', 6: '<d', 7: '<h', 8: '<i', 9: '<q', 10: '<b', 11: '<f',
        12: '<q', 13: '<q', 14: '<H', 15: '<I', 16: '<Q'}


def r_prim(f, t):
    if t in PRIM:
        fmt = PRIM[t]; return struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    if t == 3:  # char utf8
        b0 = f.read(1); first = b0[0]
        n = 1 if first < 0x80 else 2 if first < 0xE0 else 3 if first < 0xF0 else 4
        return (b0 + f.read(n - 1)).decode('utf-8')
    if t in (5, 18):
        return r_lps(f)
    raise ValueError('prim %d' % t)


def w_prim(t, v):
    if t in PRIM:
        return struct.pack(PRIM[t], v)
    if t == 3:
        return v.encode('utf-8')
    if t in (5, 18):
        return w_lps(v)
    raise ValueError('prim %d' % t)


class Rec:  # generic record
    pass


class Prim:
    """inline primitive member value (not a record)"""
    def __init__(self, t, v): self.t, self.v = t, v
    def dump(self): return w_prim(self.t, self.v)
    def __repr__(self): return 'Prim(%r)' % (self.v,)


class Header(Rec):
    def dump(self): return b'\x00' + struct.pack('<iiii', self.root, self.hid, self.major, self.minor)


class Library(Rec):
    def dump(self): return b'\x0c' + struct.pack('<i', self.lid) + w_lps(self.name)


class End(Rec):
    def dump(self): return b'\x0b'


class Null(Rec):
    def dump(self): return b'\x0a'


class NullMulti(Rec):
    def __init__(self, kind, n): self.kind, self.n = kind, n
    def dump(self):
        return (b'\x0d' + bytes([self.n])) if self.kind == 13 else (b'\x0e' + struct.pack('<i', self.n))


class Ref(Rec):
    def __init__(self, i): self.i = i
    def dump(self): return b'\x09' + struct.pack('<i', self.i)
    def __repr__(self): return 'Ref(%d)' % self.i


class Str(Rec):
    def __init__(self, oid, s): self.oid, self.s = oid, s
    def dump(self): return b'\x06' + struct.pack('<i', self.oid) + w_lps(self.s)
    def __repr__(self): return 'Str(%d,%r)' % (self.oid, self.s)


class PrimTyped(Rec):
    def __init__(self, t, v): self.t, self.v = t, v
    def dump(self): return b'\x08' + bytes([self.t]) + w_prim(self.t, self.v)


class ClassMeta:
    def __init__(self, name, members, btypes, addinfo, lib, kind):
        self.name, self.members, self.btypes, self.addinfo, self.lib, self.kind = name, members, btypes, addinfo, lib, kind


class Obj(Rec):
    """record 1,2,3,4,5"""
    def __init__(self, kind, oid, meta, values, metaid=None):
        self.kind, self.oid, self.meta, self.values, self.metaid = kind, oid, meta, values, metaid

    def dump(self):
        m = self.meta
        if self.kind == 1:
            out = b'\x01' + struct.pack('<ii', self.oid, self.metaid)
        else:
            out = bytes([self.kind]) + struct.pack('<i', self.oid) + w_lps(m.name) + struct.pack('<i', len(m.members))
            out += b''.join(w_lps(x) for x in m.members)
            if self.kind in (4, 5):
                out += bytes(m.btypes)
                for bt, ai in zip(m.btypes, m.addinfo):
                    if bt in (0, 7): out += bytes([ai])
                    elif bt == 3: out += w_lps(ai)
                    elif bt == 4: out += w_lps(ai[0]) + struct.pack('<i', ai[1])
            if self.kind in (3, 5):
                out += struct.pack('<i', m.lib)
        return out + b''.join(v.dump() for v in self.values)

    def get(self, name):
        return self.values[self.meta.members.index(name)]

    def __repr__(self): return 'Obj(%d,%s)' % (self.oid, self.meta.name)


class Arr(Rec):
    """record 7, 15, 16, 17"""
    def __init__(self, kind, oid, length, values, extra=None):
        self.kind, self.oid, self.length, self.values, self.extra = kind, oid, length, values, extra

    def dump(self):
        if self.kind == 15:
            t = self.extra
            return b'\x0f' + struct.pack('<ii', self.oid, self.length) + bytes([t]) + b''.join(w_prim(t, v) for v in self.values)
        if self.kind in (16, 17):
            return bytes([self.kind]) + struct.pack('<ii', self.oid, self.length) + b''.join(v.dump() for v in self.values)
        e = self.extra  # dict
        out = b'\x07' + struct.pack('<i', self.oid) + bytes([e['atype']]) + struct.pack('<i', e['rank'])
        out += b''.join(struct.pack('<i', x) for x in e['lengths'])
        if e['atype'] in (3, 4, 5):
            out += b''.join(struct.pack('<i', x) for x in e['lbounds'])
        out += bytes([e['bt']])
        bt, ai = e['bt'], e['ai']
        if bt in (0, 7): out += bytes([ai])
        elif bt == 3: out += w_lps(ai)
        elif bt == 4: out += w_lps(ai[0]) + struct.pack('<i', ai[1])
        return out + b''.join(v.dump() for v in self.values)

    def __repr__(self): return 'Arr(%d,k%d,n%d)' % (self.oid, self.kind, self.length)


class Reader:
    def __init__(self, data):
        self.f = io.BytesIO(data); self.metas = {}; self.objs = {}; self.libs = {}

    def i32(self): return struct.unpack('<i', self.f.read(4))[0]

    def read_record(self):
        f = self.f
        rt = f.read(1)[0]
        if rt == 0:
            h = Header(); h.root, h.hid, h.major, h.minor = struct.unpack('<iiii', f.read(16)); return h
        if rt == 12:
            l = Library(); l.lid = self.i32(); l.name = r_lps(f); self.libs[l.lid] = l.name; return l
        if rt == 11: return End()
        if rt == 10: return Null()
        if rt == 13: return NullMulti(13, f.read(1)[0])
        if rt == 14: return NullMulti(14, self.i32())
        if rt == 9: return Ref(self.i32())
        if rt == 6:
            s = Str(self.i32(), r_lps(f)); self.objs[s.oid] = s; return s
        if rt == 8:
            t = f.read(1)[0]; return PrimTyped(t, r_prim(f, t))
        if rt in (1, 2, 3, 4, 5):
            oid = self.i32(); metaid = None
            if rt == 1:
                metaid = self.i32(); meta = self.metas[metaid]
            else:
                name = r_lps(f); n = self.i32(); members = [r_lps(f) for _ in range(n)]
                btypes, addinfo, lib = None, None, None
                if rt in (4, 5):
                    btypes = list(f.read(n)); addinfo = []
                    for bt in btypes:
                        if bt in (0, 7): addinfo.append(f.read(1)[0])
                        elif bt == 3: addinfo.append(r_lps(f))
                        elif bt == 4: addinfo.append((r_lps(f), self.i32()))
                        else: addinfo.append(None)
                if rt in (3, 5): lib = self.i32()
                meta = ClassMeta(name, members, btypes, addinfo, lib, rt)
                self.metas[oid] = meta
            o = Obj(rt, oid, meta, [], metaid); self.objs[oid] = o
            o.values = self.read_values(meta)
            return o
        if rt == 15:
            oid, n = self.i32(), self.i32(); t = f.read(1)[0]
            a = Arr(15, oid, n, [r_prim(f, t) for _ in range(n)], t); self.objs[oid] = a; return a
        if rt in (16, 17):
            oid, n = self.i32(), self.i32(); a = Arr(rt, oid, n, []); self.objs[oid] = a
            a.values = self.read_elems(n); return a
        if rt == 7:
            oid = self.i32(); atype = f.read(1)[0]; rank = self.i32()
            lengths = [self.i32() for _ in range(rank)]
            lb = [self.i32() for _ in range(rank)] if atype in (3, 4, 5) else None
            bt = f.read(1)[0]; ai = None
            if bt in (0, 7): ai = f.read(1)[0]
            elif bt == 3: ai = r_lps(f)
            elif bt == 4: ai = (r_lps(f), self.i32())
            total = 1
            for x in lengths: total *= x
            a = Arr(7, oid, total, [], dict(atype=atype, rank=rank, lengths=lengths, lbounds=lb, bt=bt, ai=ai))
            self.objs[oid] = a
            if bt == 0:
                a.values = [Prim(ai, r_prim(f, ai)) for _ in range(total)]
            else:
                a.values = self.read_elems(total)
            return a
        raise ValueError('record type %d at %d' % (rt, f.tell() - 1))

    def read_elems(self, n):
        vals = []; c = 0
        while c < n:
            r = self.read_record(); vals.append(r)
            if isinstance(r, Library): continue
            c += r.n if isinstance(r, NullMulti) else 1
        return vals

    def read_values(self, meta):
        vals = []
        n = len(meta.members); i = 0
        while i < n:
            if meta.btypes is not None and meta.btypes[i] == 0:
                vals.append(Prim(meta.addinfo[i], r_prim(self.f, meta.addinfo[i]))); i += 1
            else:
                r = self.read_record(); vals.append(r)
                if isinstance(r, Library): continue
                i += r.n if isinstance(r, NullMulti) else 1
        return vals

    def read_all(self):
        recs = []
        while True:
            r = self.read_record(); recs.append(r)
            if isinstance(r, End): break
        rest = self.f.read()
        assert not rest, 'trailing %d bytes' % len(rest)
        return recs


def dump_all(recs):
    return b''.join(r.dump() for r in recs)


def unb64gz(s):
    return gzip.decompress(base64.b64decode(s))


def b64gz(raw):
    return base64.b64encode(gzip.compress(raw)).decode()



import copy


class Meta:
    def __init__(self, name, members, btypes, addinfo, lib, system):
        self.name, self.members, self.btypes, self.addinfo = name, list(members), btypes and list(btypes), addinfo and list(addinfo)
        self.lib, self.system = lib, system          # lib = library name (str) or None

    def key(self):
        return (self.name, self.lib)


class Node:
    """an object (class or struct) or an array"""
    def __init__(self, meta=None, vals=None, inline=False, arr=None):
        self.meta, self.vals, self.inline, self.arr = meta, vals, inline, arr   # arr: dict for arrays

    def get(self, name):
        return self.vals[self.meta.members.index(name)]

    def set(self, name, v):
        self.vals[self.meta.members.index(name)] = v


class S(str):
    """a string value (a .NET String object)"""


class Boxed:
    """a primitive stored in an Object slot (MemberPrimitiveTyped)"""
    def __init__(self, t, v): self.t, self.v = t, v


class P:
    """a primitive in a typed member slot / primitive array element"""
    def __init__(self, t, v): self.t, self.v = t, v


def decode(raw):
    rd = Reader(raw)
    recs = rd.read_all()
    libs = rd.libs
    nodes = {}

    def meta_of(o):
        m = o.meta
        addinfo = None
        if m.addinfo is not None:
            addinfo = [(ai[0], libs.get(ai[1])) if bt == 4 else ai for bt, ai in zip(m.btypes, m.addinfo)]
        return Meta(m.name, m.members, m.btypes, addinfo, libs.get(m.lib) if m.lib is not None else None,
                    m.kind in (2, 4))

    strs = {}

    def str_obj(r):            # keep .NET string identity: one S object per string record
        if id(r) not in strs:
            strs[id(r)] = S(r.s)
        return strs[id(r)]

    def conv(v):
        if isinstance(v, Prim):
            return P(v.t, v.v)
        if isinstance(v, Str):
            return str_obj(v)
        if isinstance(v, Null):
            return None
        if isinstance(v, PrimTyped):
            return Boxed(v.t, v.v)
        if isinstance(v, Ref):
            tgt = rd.objs[v.i]
            if isinstance(tgt, Str):
                return str_obj(tgt)
            return node_for(tgt, False)
        if isinstance(v, (Obj, Arr)):
            return node_for(v, True)
        raise TypeError(v)

    def conv_list(vals):
        out = []
        for v in vals:
            if isinstance(v, Library):
                continue
            if isinstance(v, NullMulti):
                out += [None] * v.n
            else:
                out.append(conv(v))
        return out

    def node_for(r, inline):
        if id(r) in nodes:
            return nodes[id(r)]
        if isinstance(r, Obj):
            n = Node(meta_of(r), None, inline)
            nodes[id(r)] = n
            n.vals = conv_list(r.values)
        else:
            if r.kind == 15:
                n = Node(arr=dict(kind=15, ptype=r.extra), vals=[P(r.extra, x) for x in r.values], inline=inline)
            elif r.kind in (16, 17):
                n = Node(arr=dict(kind=r.kind), inline=inline)
                nodes[id(r)] = n
                n.vals = conv_list(r.values)
            else:
                e = dict(r.extra)
                if e['bt'] == 4:
                    e['ai'] = (e['ai'][0], libs.get(e['ai'][1]))
                n = Node(arr=dict(kind=7, **e), inline=inline)
                nodes[id(r)] = n
                n.vals = [P(v.t, v.v) for v in r.values] if e['bt'] == 0 else conv_list(r.values)
            nodes[id(r)] = n
        return n

    hdr = recs[0]
    return node_for(rd.objs[hdr.root], False)


class Writer:
    def __init__(self):
        self.next = 0
        self.ids = {}            # id(node) -> object id
        self.metas = {}          # meta key -> object id of first object of that class
        self.libs = {}           # library name -> id
        self.queue = []
        self.recs = []
        self.strings = {}        # .NET writes each distinct string once, then refers to it
        self.str_ids = {}
        self.keep = []

    def nid(self):
        self.next += 1
        return self.next

    def lib_id(self, name, out):
        if name not in self.libs:
            self.libs[name] = self.nid()
            l = Library(); l.lid = self.libs[name]; l.name = name
            out.append(l)
        return self.libs[name]

    def ref_id(self, node):
        if id(node) not in self.ids:
            self.ids[id(node)] = self.nid()
            self.queue.append(node)
        return self.ids[id(node)]

    def value(self, v, out, typed_prim=None):
        """append the record(s) for one value to out"""
        if typed_prim is not None:
            out.append(Prim(typed_prim, v.v)); return
        if v is None:
            out.append(Null()); return
        if isinstance(v, S) or isinstance(v, str):
            sv = str(v)
            i = self.nid()
            if sv in self.strings:
                out.append(Ref(self.strings[sv])); return        # an equal string: .NET numbers it, then refers
            self.strings[sv] = i
            out.append(Str(i, sv)); return
        if isinstance(v, Boxed):
            out.append(PrimTyped(v.t, v.v)); return
        if isinstance(v, Node):
            if v.inline:
                out.extend(self.record(v, -self.nid())); return
            out.append(Ref(self.ref_id(v))); return
        raise TypeError(v)

    def array_values(self, vals, out):
        # runs of empty elements are written as one 'null-multiple' record, like .NET
        i = 0
        while i < len(vals):
            if vals[i] is None:
                j = i
                while j < len(vals) and vals[j] is None:
                    j += 1
                cnt = j - i
                out.append(Null() if cnt == 1 else NullMulti(13 if cnt < 256 else 14, cnt))
                i = j
            else:
                self.value(vals[i], out)
                i += 1

    def record(self, n, oid):
        out = []
        if n.arr is None:
            m = n.meta
            k = m.key()
            libid = self.lib_id(m.lib, out) if m.lib is not None else None
            # .NET announces the assemblies of referenced member objects before this record
            for v in n.vals:
                if isinstance(v, Node) and v.arr is None and v.meta.lib is not None:
                    self.lib_id(v.meta.lib, out)
            if k in self.metas:
                cm = ClassMeta(m.name, m.members, m.btypes, m.addinfo, libid, 1)
                o = Obj(1, oid, cm, [], metaid=self.metas[k])
            else:
                kind = (4 if m.btypes is not None else 2) if m.system else (5 if m.btypes is not None else 3)
                addinfo = None
                if m.btypes is not None:
                    addinfo = []
                    for bt, ai in zip(m.btypes, m.addinfo):
                        addinfo.append((ai[0], self.lib_id(ai[1], out)) if bt == 4 else ai)
                cm = ClassMeta(m.name, m.members, m.btypes, addinfo, libid, kind)
                o = Obj(kind, oid, cm, [])
                self.metas[k] = oid
            out.append(o)
            vals = []
            for i, v in enumerate(n.vals):
                tp = m.addinfo[i] if (m.btypes is not None and m.btypes[i] == 0) else None
                self.value(v, vals, tp)
            o.values = vals
            return out
        a = n.arr
        if a['kind'] == 15:
            out.append(Arr(15, oid, len(n.vals), [p.v for p in n.vals], a['ptype'])); return out
        if a['kind'] in (16, 17):
            r = Arr(a['kind'], oid, len(n.vals), [])
            out.append(r)
            self.array_values(n.vals, r.values)
            return out
        e = {k: a[k] for k in ('atype', 'rank', 'lengths', 'lbounds', 'bt', 'ai')}
        if e['bt'] == 4:
            e['ai'] = (e['ai'][0], self.lib_id(e['ai'][1], out))
        e['lengths'] = [len(n.vals)] if e['rank'] == 1 else e['lengths']
        r = Arr(7, oid, len(n.vals), [], e)
        out.append(r)
        if e['bt'] == 0:
            r.values = [Prim(p.t, p.v) for p in n.vals]
        else:
            self.array_values(n.vals, r.values)
        return out

    def write(self, root):
        h = Header(); h.root, h.hid, h.major, h.minor = 1, -1, 1, 0
        self.recs.append(h)
        self.ids[id(root)] = self.nid()
        self.recs += self.record(root, 1)
        while self.queue:
            n = self.queue.pop(0)
            self.recs += self.record(n, self.ids[id(n)])
        self.recs.append(End())
        raw = dump_all(self.recs)
        return raw


def encode(root):
    return Writer().write(root)


def same(a, b, seen=None):
    """structural equality of two decoded graphs"""
    seen = seen if seen is not None else set()
    if isinstance(a, Node) and isinstance(b, Node):
        if (id(a), id(b)) in seen:
            return True
        seen.add((id(a), id(b)))
        if (a.arr is None) != (b.arr is None) or a.inline != b.inline or len(a.vals) != len(b.vals):
            return False
        if a.arr is None:
            if (a.meta.name, a.meta.members, a.meta.lib) != (b.meta.name, b.meta.members, b.meta.lib):
                return False
        else:
            if a.arr['kind'] != b.arr['kind']:
                return False
        return all(same(x, y, seen) for x, y in zip(a.vals, b.vals))
    if isinstance(a, (P, Boxed)) and isinstance(b, (P, Boxed)):
        return type(a) is type(b) and a.t == b.t and a.v == b.v
    if isinstance(a, str) and isinstance(b, str):
        return str(a) == str(b)
    return a is None and b is None


def deepcopy_node(n):
    return copy.deepcopy(n)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def say(t=""):
    print(t, flush=True)


def ask_yn(q, default, auto):
    if auto:
        return default
    s = "Y/n" if default else "y/N"
    for _ in range(5):
        try:
            r = input("%s (%s): " % (q, s)).strip().lower()
        except EOFError:
            return default
        if not r:
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False
        say("Please type y or n.")
    return default


def pause(auto):
    if not auto:
        try:
            input("Press Enter to continue...")
        except EOFError:
            pass


def blob_decode(v):
    if not v.startswith("1|"):
        raise ValueError("unknown settings format")
    return decode(gzip.decompress(base64.b64decode(v[2:].replace("[backslash]", "/"))))


def blob_encode(root):
    raw = encode(root)
    same_check = decode(raw)
    if not same(root, same_check):
        raise RuntimeError("internal check failed while encoding")
    return "1|" + base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii").replace("/", "[backslash]")


def fourchar(d1, d2, guid):
    """Thetis' Common.FourChar()"""
    h = base64.b64encode(hashlib.sha256(("%s:%s:%s" % (d1, d2, guid)).encode()).digest()).decode()
    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    idx = [0] * 4
    for i, c in enumerate(h):
        idx[i % 4] = (idx[i % 4] + ord(c)) % len(chars)
    return "".join(chars[i] for i in idx)


def led_fourchar(item_group_id):
    return fourchar("ledindicator", 0, item_group_id)


# ---------------------------------------------------------------------------
# editing a Thetis settings dictionary (ConcurrentDictionary<string, object>)
# ---------------------------------------------------------------------------
class Settings:
    def __init__(self, blob):
        self.root = blob_decode(blob)
        arr = self.root.get("m_serializationArray")
        self.kv = {}
        for e in arr.vals:
            if e is not None:
                self.kv[str(e.get("key"))] = e

    def get(self, k):
        return self.kv[k].get("value")

    def node(self, k):
        v = self.get(k)
        assert isinstance(v, Node), k
        return v

    def set_str(self, k, s):
        self.kv[k].set("value", S(s))

    def set_num(self, k, x):
        old = self.get(k)
        assert isinstance(old, Boxed), (k, old)
        if old.t == 11:
            x = struct.unpack("<f", struct.pack("<f", float(x)))[0]
        elif old.t in (8, 7, 9):
            x = int(x)
        elif old.t == 1:
            x = bool(x)
        self.kv[k].set("value", Boxed(old.t, x))

    def set_enum(self, k, n):
        nd = copy.deepcopy(self.node(k))
        nd.vals[0] = P(nd.vals[0].t, n)
        self.kv[k].set("value", nd)

    def set_color(self, k, argb):
        c = copy.deepcopy(self.node(k))
        assert c.meta.name == "System.Drawing.Color"
        c.set("name", None)
        c.set("value", P(9, struct.unpack("<q", struct.pack("<Q", argb))[0]))
        c.set("knownColor", P(7, 0))
        c.set("state", P(7, 2))
        self.kv[k].set("value", c)

    def dump(self):
        return blob_encode(self.root)


def set_lib_version(node, version_name, seen=None):
    """make every 'Thetis, Version=...' reference (assembly names and the assembly names
    inside generic type names) match the user's Thetis"""
    seen = seen if seen is not None else set()
    ver = re.search(r"Version=[0-9.]+", version_name).group(0)

    def fix(x):
        if isinstance(x, str):
            return re.sub(r"(Thetis, )Version=[0-9.]+", lambda m: m.group(1) + ver, x)
        if isinstance(x, tuple):
            return tuple(fix(y) for y in x)
        return x

    if not isinstance(node, Node) or id(node) in seen:
        return
    seen.add(id(node))
    if node.arr is None:
        node.meta.name = fix(node.meta.name)
        node.meta.lib = fix(node.meta.lib)
        if node.meta.addinfo:
            node.meta.addinfo = [fix(x) for x in node.meta.addinfo]
    else:
        if "ai" in node.arr:
            node.arr["ai"] = fix(node.arr["ai"])
    for v in node.vals:
        set_lib_version(v, version_name, seen)


# ---------------------------------------------------------------------------
# item builders (each returns the settings blob string)
# ---------------------------------------------------------------------------
class Builder:
    def __init__(self, thetis_lib, in_guid, in4, json_out=False):
        self.lib, self.in_guid, self.T = thetis_lib, in_guid, in4
        self.json_out = json_out
        self.dropped = set()

    def _out(self, s, name):
        if not self.json_out:
            return s.dump()
        return to_json_blob(s, name, self.lib, self.dropped)

    def _tpl(self, name):
        s = Settings(TEMPLATES[name])
        if self.lib:
            set_lib_version(s.root, self.lib)
        return s

    def text(self, text, colour, size=12.0, bold=False, gate=None):
        s = self._tpl("TEXT_OVERLAY")
        for k in ("_text_1", "_text_2"):
            s.set_str(k, text)
        s.set_color("_colour", colour)
        s.set_color("_markerColour", colour)
        for k in ("_font_size_1", "_font_size_2"):
            s.set_num(k, size)
        for k in ("_font_family_1", "_font_family_2"):
            s.set_str(k, "Verdana")
        for k in ("_font_style_1", "_font_style_2"):
            s.set_enum(k, 1 if bold else 0)
        s.set_num("_spacerPadding", 0.046)
        s.set_num("textoverlay_rx_ledlogic", bool(gate))
        s.set_num("textoverlay_tx_ledlogic", bool(gate))
        s.set_str("textoverlay_rx_4char", gate or "")
        s.set_str("textoverlay_tx_4char", gate or "")
        return self._out(s, "TEXT_OVERLAY")

    def bar(self, var, title, units, vmin, vmax, vhigh):
        s = self._tpl("CUSTOM_METER_BAR")
        s.set_str("_mmio_variable_0", "%s.%s" % (self.T, var))
        g = copy.deepcopy(s.node("_mmio_guid_0"))
        f = struct.unpack("<ihhBBBBBBBB", uuid.UUID(self.in_guid).bytes_le)
        for i, name in enumerate(["_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_i", "_j", "_k"]):
            g.set(name, P(g.get(name).t, f[i]))
        s.kv["_mmio_guid_0"].set("value", g)
        s.set_num("meter_custom_min", vmin)
        s.set_num("meter_custom_max", vmax)
        s.set_num("meter_custom_high", vhigh)
        s.set_str("meter_custom_title", title)
        s.set_str("meter_custom_units", units)
        s.set_enum("_barStyle", 2)                          # solid filled bar
        s.set_color("_segmentedSolidLowColour", 0xFF1E90FF)  # live bar: blue
        s.set_color("_segmentedSolidHighColour", 0xFFFF2020)  # over the limit: red
        s.set_color("_historyColor", 0xFFFFE000)            # peak band: yellow
        s.set_color("_markerColour", 0xFFFFFFFF)
        s.set_color("_peakHoldMarkerColor", 0xFFFF0000)
        s.set_num("_showHistory", True)
        s.set_num("_peakHold", True)
        s.set_num("_historyDuration", 4000)                 # peak held 4 seconds
        s.set_num("_ignoreHistoryDuration", 0)              # peaks from the first moment of TX
        return self._out(s, "CUSTOM_METER_BAR")

    def led(self, condition, four):
        s = self._tpl("LED")
        s.set_str("_text_1", condition)
        s.set_num("_showSubMarker", False)      # no panel -> takes no space, invisible
        s.set_num("_peakHold", False)
        s.set_num("_showMarker", False)
        s.set_num("_ignoreHistoryDuration", 0)
        s.set_num("_updateInterval", 100)
        s.set_str("led_4char", four)
        s.set_num("led_process_when_hidden", True)
        return self._out(s, "LED")

    def buttons(self, spec, columns, out4):
        """spec: list of dicts on, off, msg_on, msg_off, led(4char or None)"""
        s = self._tpl("OTHER_BUTTONS")
        s.set_num("buttonbox_columns", columns)
        for n in range(12):
            s.set_num("buttonbox_other_buttons_bitfield_%d" % n, 0)
        s.set_num("buttonbox_other_buttons_bitfield_11", (1 << len(spec)) - 1)   # macro buttons M1..Mn
        s.set_num("buttonbox_use_icons", False)
        s.set_num("buttonbox_fix_text_size", True)
        s.set_num("buttonbox_font_scale", 0.42)
        s.set_str("_font_family_1", "Verdana")
        s.set_num("_font_size_1", 12.0)
        for n in range(31):
            ms = s.node("buttonbox_other_buttons_macro_settings_%d" % n)
            ms = copy.deepcopy(ms)
            sp = spec[n] if n < len(spec) else None
            ms.set("_on_text", S(sp["on"] if sp else ""))
            ms.set("_off_text", S(sp["off"] if sp else ""))
            ms.set("_notes", S(""))
            st = ms.get("_buttonstate_type")
            st.vals[0] = P(st.vals[0].t, 3 if (sp and sp.get("led")) else 0)      # 3 = LED, 0 = OFF
            ms.set("_closes_parent", P(1, False))
            ms.set("_on_state", P(1, False))
            ms.set("_run_state_command_on_visible", P(1, False))
            for a in ("_closes_container", "_opens_container", "_open_uses_location"):
                ms.get(a).vals = [P(1, False)] * 4
            real = bool(sp) and not sp.get("gap")
            ms.get("_send_via_mmio").vals = [P(1, real), P(1, False), P(1, False), P(1, False)]
            for a in ("_close_container_id", "_open_container_id"):
                ms.get(a).vals = [S("")] * 4
            ms.get("_mmio_4char").vals = [S(out4 if real else "")] + [S("")] * 3
            ms.get("_mmio_message_on").vals = [S(sp["msg_on"] if sp else "")] + [S("")] * 3
            ms.get("_mmio_message_off").vals = [S(sp["msg_off"] if sp else "")] + [S("")] * 3
            ms.set("_led_indicator_four_char", S((sp.get("led") or "") if sp else ""))
            for f in ("_buttonstate_container_visible_id", "_buttonstate_cat_on_reply", "_cat_macro"):
                ms.set(f, S(""))
            ms.get("_cat_macro_send").vals = [P(1, False)]
            s.kv["buttonbox_other_buttons_macro_settings_%d" % n].set("value", ms)
        return self._out(s, "OTHER_BUTTONS")


# ---------------------------------------------------------------------------
# SDR-VST3 (newer Thetis on .NET 10) keeps item settings as JSON: [{"k","t","v"}, ...]
# The tested binary settings are translated value by value onto SDR-VST3's own templates.
# ---------------------------------------------------------------------------
KNOWN_COLOURS = {35: ("Black", 0xFF000000), 45: ("CornflowerBlue", 0xFF6495ED), 50: ("DarkCyan", 0xFF008B8B),
                 52: ("DarkGray", 0xFFA9A9A9), 95: ("LightGray", 0xFFD3D3D3), 127: ("Orange", 0xFFFFA500),
                 141: ("Red", 0xFFFF0000), 164: ("White", 0xFFFFFFFF), 167: ("Yellow", 0xFFFFFF00)}
MACRO_JSON = [("_number", "Number"), ("_on_text", "OnText"), ("_off_text", "OffText"), ("_notes", "Notes"),
              ("_closes_parent", "ClosesParent"), ("_closes_container", "ClosesContainer"),
              ("_opens_container", "OpensContainer"), ("_close_container_id", "CloseContainerID"),
              ("_open_container_id", "OpenContainerID"), ("_open_uses_location", "OpenUsesLocation"),
              ("_send_via_mmio", "SendsViaMMIO"), ("_mmio_4char", "MMICFourChar"),
              ("_mmio_message_on", "MMIOMessageON"), ("_mmio_message_off", "MMIOMessageOFF"),
              ("_buttonstate_type", "ButtonStateType"), ("_led_indicator_four_char", "LedIndiciatorFourChar"),
              ("_buttonstate_container_visible_id", "ContainerVisibleID"),
              ("_buttonstate_cat_on_reply", "ButtonStateCatReply"),
              ("_run_state_command_on_visible", "RunStateCommandOnVisible"), ("_cat_macro_send", "CatMacroSend"),
              ("_on_state", "OnState"), ("_cat_macro", "CatMacro")]


def json_colour(argb, name=None, known=False):
    a, r, g, b = (argb >> 24) & 255, (argb >> 16) & 255, (argb >> 8) & 255, argb & 255
    return {"R": r, "G": g, "B": b, "A": a, "IsKnownColor": known, "IsEmpty": False, "IsNamedColor": known,
            "IsSystemColor": False, "Name": name if known else "%02x%02x%02x%02x" % (a, r, g, b)}


def _num(x):
    """shortest text that is still the same 32-bit float (how .NET writes a float)"""
    f32 = struct.unpack("<f", struct.pack("<f", x))[0]
    for p in range(6, 10):
        y = float("%.*g" % (p, f32))
        if struct.unpack("<f", struct.pack("<f", y))[0] == f32:
            return y
    return f32


def json_value(v, jt, default):
    """one binary value -> its JSON form (jt: the JSON type tag, default: SDR-VST3's own default)"""
    if v is None:
        return None
    if isinstance(v, (Boxed, P)):
        if jt in ("float", "double"):
            return _num(float(v.v))
        if jt == "int":
            return int(v.v)
        return v.v
    if isinstance(v, str):
        return str(v)
    if not isinstance(v, Node):
        return default
    if v.arr is not None:
        return [json_value(x, "", None) for x in v.vals]
    name = v.meta.name
    if name == "System.Drawing.Color":
        st = v.get("state").v
        if st & 2:
            return json_colour(v.get("value").v & 0xFFFFFFFF)
        kc = v.get("knownColor").v
        if kc in KNOWN_COLOURS:
            n, argb = KNOWN_COLOURS[kc]
            return json_colour(argb, n, True)
        return default
    if name == "System.Guid":
        return guid_of(v)
    if name == "Thetis.OtherButtonMacroSettings":
        out = dict(default) if isinstance(default, dict) else {}
        for f, jk in MACRO_JSON:
            out[jk] = json_value(v.get(f), "", out.get(jk))
        return out
    if len(v.vals) == 1 and isinstance(v.vals[0], P):        # an enum
        return v.vals[0].v
    return default


def to_json_blob(s, name, lib, dropped):
    tpl = copy.deepcopy(JSON_TEMPLATES[name])
    ver = re.search(r"Version=[0-9.]+", lib).group(0) if lib else None
    for e in tpl:
        if ver and e["t"].startswith("aq:"):
            e["t"] = re.sub(r"(Thetis, )Version=[0-9.]+", lambda m: m.group(1) + ver, e["t"])
        if e["k"] in s.kv:
            e["v"] = json_value(s.get(e["k"]), e["t"], e["v"])
    have = {e["k"] for e in tpl}
    for k in s.kv:
        if k not in have:
            dropped.add("%s:%s" % (name, k))
    return json_blob_encode("2", tpl)


# ---------------------------------------------------------------------------
# the Thetis database
# ---------------------------------------------------------------------------
ROW_RE = re.compile(r"<Key>([^<]*)</Key>\s*<Value>([^<]*)</Value>")


class ThetisDB:
    def __init__(self, path):
        self.path = path
        with open(path, encoding="utf-8", newline="") as f:
            self.text = f.read()
        self.nl = "\r\n" if "\r\n" in self.text else "\n"
        self._index()

    def _index(self):
        self.rows = {}
        for m in ROW_RE.finditer(self.text):
            self.rows[html.unescape(m.group(1))] = m

    def val(self, k):
        return html.unescape(self.rows[k].group(2))

    def has(self, k):
        return k in self.rows

    def keys(self):
        return list(self.rows)

    # all edits are collected, then applied from the end of the file backwards
    def begin(self):
        self.edits = []

    def set_val(self, k, v):
        m = self.rows[k]
        self.edits.append((m.start(2), m.end(2), html.escape(v, quote=False)))

    def delete(self, k):
        m = self.rows[k]
        a = self.text.rindex("<Options>", 0, m.start())
        a = self.text.rindex(self.nl, 0, a)
        b = self.text.index("</Options>", m.end()) + len("</Options>")
        self.edits.append((a, b, ""))

    def row_text(self, k, v):
        nl = self.nl
        return (nl + "  <Options>" + nl + "    <Key>%s</Key>" % html.escape(k, quote=False) + nl +
                "    <Value>%s</Value>" % html.escape(v, quote=False) + nl + "  </Options>")

    def insert_after(self, anchor_key, rows):
        m = self.rows[anchor_key]
        at = self.text.index("</Options>", m.end()) + len("</Options>")
        self.edits.append((at, at, "".join(self.row_text(k, v) for k, v in rows)))

    def insert_at_options_end(self, rows):
        at = self.text.rindex("</Options>") + len("</Options>")
        self.edits.append((at, at, "".join(self.row_text(k, v) for k, v in rows)))

    def commit_text(self):
        t = self.text
        for a, b, s in sorted(self.edits, key=lambda e: (e[0], e[1]), reverse=True):
            t = t[:a] + s + t[b:]
        return t


# ---------------------------------------------------------------------------
# which program: Thetis, Thetis HL2 (MI0BOT) or SDR-VST3. All three run as Thetis.exe, so a
# program is told apart by its folder, never by its name.
# ---------------------------------------------------------------------------
PROGRAMS = {
    "thetis": dict(name="Thetis",
                   dbs=[("Thetis-x64", "dbman_settings.json"), ("Thetis", "dbman_settings.json")],
                   exe=["OpenHPSDR\\Thetis"], args=[], backups=""),
    "hl2": dict(name="Thetis HL2",
                dbs=[("Thetis-x64", "HL2_dbman_settings.json"), ("Thetis-HL2-x64", "dbman_settings.json")],
                exe=["OpenHPSDR\\Thetis-HL2", "OpenHPSDR-HL2\\Thetis"], args=["-dbid:HL2"], backups="Thetis-HL2"),
    "vst3": dict(name="SDR-VST3",
                 dbs=[("SDR-VST3-x64", "dbman_settings.json")],
                 exe=["OpenHPSDR\\SDR-VST3"], args=[], backups="SDR-VST3"),
}
PROG_ORDER = ["thetis", "hl2", "vst3"]
PROG = [None]           # the program this run works on (None = any Thetis, the old behaviour)


def pname(prog=None):
    prog = prog if prog is not None else PROG[0]
    return PROGRAMS[prog]["name"] if prog else "Thetis"


def program_db(prog, appdata=None):
    """the active database.xml of one program (None if it was never started)"""
    appdata = appdata or os.environ.get("APPDATA", "")
    for sub, js_name in PROGRAMS[prog]["dbs"]:
        dbdir = os.path.join(appdata, "OpenHPSDR", sub, "DB")
        js = os.path.join(dbdir, js_name)
        if os.path.exists(js):
            try:
                with open(js, encoding="utf-8-sig") as f:
                    d = json.load(f)
                p = os.path.join(dbdir, d.get("ActiveDB_File", "").replace("\\", os.sep))
                if os.path.isfile(p):
                    return p
            except (OSError, ValueError):
                pass
        if js_name == "dbman_settings.json":
            old = os.path.join(appdata, "OpenHPSDR", sub, "database.xml")
            if os.path.isfile(old):
                return old
    return None


def _pf_roots():
    roots = []
    for r in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
              "C:\\Program Files", "C:\\Program Files (x86)"):
        if r and r not in roots:
            roots.append(r)
    return roots


def program_exe(prog):
    """full path of the program's Thetis.exe (None if not found)"""
    for r in _pf_roots():
        for rel in PROGRAMS[prog]["exe"]:
            p = os.path.join(r, rel, "Thetis.exe") if os.name == "nt" else r + "\\" + rel + "\\Thetis.exe"
            if os.path.isfile(p):
                return p
    return None


def installed_programs(appdata=None):
    """programs with settings on this computer, in menu order"""
    return [p for p in PROG_ORDER if program_db(p, appdata)]


def find_active_db(appdata=None):
    if PROG[0]:
        return program_db(PROG[0], appdata)
    for p in PROG_ORDER[:1]:
        x = program_db(p, appdata)
        if x:
            return x
    return None


def _ps(cmd, timeout=20):
    """run a short PowerShell command, return its output ('' on any problem)"""
    try:
        return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                              capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def _any_thetis_exe():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Thetis.exe", "/NH"], capture_output=True,
                             text=True, timeout=10).stdout
        return "thetis.exe" in out.lower()
    except Exception:
        return False


def running_thetis_paths():
    """paths of every running Thetis.exe ('?' when Windows will not tell the path)"""
    if os.name != "nt":
        return []
    out = _ps("Get-Process -Name Thetis -ErrorAction SilentlyContinue | ForEach-Object { if ($_.Path) "
              "{ $_.Path } else { '?' } }")
    paths = [x.strip() for x in out.splitlines() if x.strip()]
    if not paths and _any_thetis_exe():
        paths = ["?"]
    return paths


def _belongs(path, prog):
    if prog is None or path == "?":
        return True                 # cannot tell: be careful and count it
    d = os.path.normcase(os.path.dirname(path)).rstrip("\\/")
    for r in _pf_roots():
        for rel in PROGRAMS[prog]["exe"]:
            if d == os.path.normcase(os.path.join(r, rel)).rstrip("\\/"):
                return True
    return False


def thetis_running():
    return any(_belongs(p, PROG[0]) for p in running_thetis_paths())


def thetis_path():
    """full path of the running copy of this program (None if not found)"""
    for p in running_thetis_paths():
        if p != "?" and _belongs(p, PROG[0]):
            return p
    return None


def close_thetis(wait_s=60):
    """Ask the program to close the normal way (like clicking its X / File > Exit), so it saves
    its settings. Never forced. Only the copy in this program's own folder is asked. True when closed."""
    if os.name != "nt":
        return False
    path = thetis_path()
    if path:
        _ps("Get-Process -Name Thetis -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq '%s' } | "
            "ForEach-Object { [void]$_.CloseMainWindow() }" % path.replace("'", "''"))
    else:
        _ps("Get-Process -Name Thetis -ErrorAction SilentlyContinue | ForEach-Object { [void]$_.CloseMainWindow() }")
    for i in range(wait_s):
        if not thetis_running():
            time.sleep(1.0)          # let it finish writing its files
            return True
        if i and i % 10 == 0:
            say("  ... still waiting for %s to close (%d s)" % (pname(), i))
        time.sleep(1.0)
    return False


CLOSED_THETIS = []      # (exe path, arguments, name) of programs this run closed (to offer starting again)


NO_RESTART = []         # set by --no-restart (the installer starts the program itself at the very end)
ANSWERS = {}            # answers given once, used for every program in this run
DEFER_RESTART = []      # several programs in one run: offer the restart once, at the end


def offer_thetis_restart(auto, final=False):
    if NO_RESTART or (DEFER_RESTART and not final):
        return
    seen = set()
    for exe, args, name in CLOSED_THETIS[-1:]:          # only one can run at a time (TCI port 50001)
        if exe in seen or not os.path.exists(exe):
            continue
        seen.add(exe)
        if ask_yn("Start %s again now?" % name, True, auto):
            try:
                subprocess.Popen([exe] + list(args), cwd=os.path.dirname(exe), close_fds=True)
                say("%s is starting." % name)
            except OSError as e:
                say("Could not start %s (%s) - please start it yourself." % (name, e))


def tci_note(db):
    """warn when the TCI server is not switched on (the bridge cannot connect then)"""
    try:
        on = db.has("chkTCIServerListening") and db.val("chkTCIServerListening").strip().lower() == "true"
        bind = db.val("txtTCIServerBindIPPort").strip() if db.has("txtTCIServerBindIPPort") else ""
    except Exception:
        return
    if not on:
        say("")
        say("NOTE: the TCI server in %s is switched OFF, so the bridge cannot connect to it." % pname())
        say("  In %s: Setup > Serial/Network/Midi CAT > Network: tick 'TCI Server Running'," % pname())
        say("  Bind IP:Port 127.0.0.1:50001, then Apply and OK.")
    elif bind and not bind.endswith(":50001"):
        say("")
        say("NOTE: %s's TCI server listens on %s; the bridge expects port 50001." % (pname(), bind))


def thetis_lib_name(db):
    """the 'Thetis, Version=...' assembly name used by this Thetis"""
    for k in db.keys():
        v = db.val(k)
        if v.startswith("1|H4sI") and (k.startswith("meterIGSettings_2_") or k == "multimeter_io2"):
            try:
                raw = gzip.decompress(base64.b64decode(v[2:].replace("[backslash]", "/")))
            except Exception:
                continue
            m = re.search(rb"Thetis, Version=[0-9.]+, Culture=neutral, PublicKeyToken=null", raw)
            if m:
                return m.group(0).decode()
    return None


def version_tuple(db):
    js = os.path.join(os.path.dirname(db.path), "dbman.json")
    try:
        with open(js, encoding="utf-8-sig") as f:
            s = json.load(f).get("VersionString", "")
        m = re.search(r"v(\d+)\.(\d+)(?:\.(\d+))?(?:\.(\d+))?", s)     # first one = program version
        if m:
            t = tuple(int(x or 0) for x in m.groups())
            if "SDR-VST3" in s:
                t = (2, 10, 3, 99) if t >= (5, 0) else (2, 10, 3, 0)   # SDR-VST3 5.x is built on Thetis 2.10.3.x
            return t, s
    except (OSError, ValueError):
        pass
    return None, ""


# ---------------------------------------------------------------------------
# Multi Meter I/O
# ---------------------------------------------------------------------------
def guid_of(n):
    f = [n.get(x).v for x in ("_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_i", "_j", "_k")]
    return str(uuid.UUID(bytes_le=struct.pack("<ihhBBBBBBBB", *f)))


def set_guid(n, g):
    f = struct.unpack("<ihhBBBBBBBB", uuid.UUID(g).bytes_le)
    for i, x in enumerate(("_a", "_b", "_c", "_d", "_e", "_f", "_g", "_h", "_i", "_j", "_k")):
        n.set(x, P(n.get(x).t, f[i]))


def enum_val(n):
    return n.vals[0].v


def set_enum(n, v):
    n.vals[0] = P(n.vals[0].t, v)


def mmio_entries(root):
    out = []
    for e in root.get("m_serializationArray").vals:
        if e is None:
            continue
        m = e.get("value")
        ep = m.get("_udp_endpoint")
        out.append(dict(kvp=e, node=m, guid=guid_of(m.get("_guid")), four=str(m.get("_four_char")),
                        ip=str(m.get("_ip")), port=m.get("_port").v, type=enum_val(m.get("_type")),
                        dir=enum_val(m.get("_direction")), fin=enum_val(m.get("_format_in")),
                        fout=enum_val(m.get("_format_out")), enabled=m.get("_enabled").v,
                        ep_ip=str(m.get("_udp_endpoint_ip")), ep_port=m.get("_udp_endpoint_port").v,
                        has_ep=isinstance(ep, Node)))
    return out


def new_mmio(template_root, which, guid, four, used_ports):
    """copy a template entry and set it up"""
    tpl = [e for e in mmio_entries(template_root) if e["four"] == {"in": "tatg", "out": "hKCC"}[which]][0]
    m = copy.deepcopy(tpl["node"])
    set_guid(m.get("_guid"), guid)
    m.set("_four_char", S(four))
    m.set("_ip", S("127.0.0.1"))
    m.set("_enabled", P(1, True))
    m.set("_listener_active", P(1, True))
    # no variables yet: they arrive from the bridge
    vars_ = m.get("_io_variables")
    vars_.get("m_serializationArray").vals = []
    if which == "in":
        m.set("_port", P(8, IN_PORT))
        set_enum(m.get("_direction"), 0)     # IN
        set_enum(m.get("_format_in"), 1)     # XML
        set_enum(m.get("_type"), 0)          # UDP listener
        set_enum(m.get("_terminator_in"), 0)
    else:
        port = OUT_LISTEN
        while port in used_ports:
            port += 1
        m.set("_port", P(8, port))
        set_enum(m.get("_direction"), 2)     # BOTH (sends to the bridge)
        set_enum(m.get("_format_out"), 2)    # RAW
        set_enum(m.get("_type"), 0)
        set_enum(m.get("_terminator_out"), 0)
        m.set("_udp_endpoint_ip", S("127.0.0.1"))
        m.set("_udp_endpoint_port", P(8, OUT_PORT))
        ep = m.get("_udp_endpoint")
        ep.set("m_Port", P(8, OUT_PORT))
    kvp = copy.deepcopy(tpl["kvp"])
    set_guid(kvp.get("key"), guid)
    kvp.set("value", m)
    return kvp


def mmio_is_json(v):
    """newer programs (SDR-VST3) keep Multi Meter I/O as gzipped JSON instead of .NET binary"""
    try:
        raw = gzip.decompress(base64.b64decode(v.split("|", 1)[1].replace("[backslash]", "/")))
    except Exception:
        return False
    return raw.lstrip()[:1] == b"{"


def json_blob_decode(v):
    pre, b = v.split("|", 1)
    return pre, json.loads(gzip.decompress(base64.b64decode(b.replace("[backslash]", "/"))).decode("utf-8"))


def json_blob_encode(pre, obj):
    raw = json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return pre + "|" + base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii").replace("/", "[backslash]")


def json_mmio_entries(d):
    out = []
    for g, m in d.items():
        out.append(dict(kvp=None, node=m, guid=m.get("Guid", g), four=str(m.get("FourChar", "")),
                        ip=str(m.get("IP", "")), port=int(m.get("Port", 0)), type=int(m.get("Type", 0)),
                        dir=int(m.get("Direction", 0)), fin=int(m.get("FormatIn", 0)),
                        fout=int(m.get("FormatOut", 0)), enabled=bool(m.get("Enabled", False)),
                        ep_ip=str(m.get("UdpEndpointIP", "")), ep_port=int(m.get("UdpEndpointPort", 0)),
                        has_ep=True))
    return out


JSON_MMIO_TEMPLATE = {"Guid": "", "Direction": 0, "IP": "127.0.0.1", "Port": 0, "UdpEndpointIP": "127.0.0.1",
                      "UdpEndpointPort": 10000, "ComPort": "", "BaudRate": 9600, "DataBits": 8, "StopBits": 1,
                      "Parity": 0, "FormatIn": 0, "FormatOut": 0, "TerminatorIn": 0, "TerminatorOut": 0,
                      "Type": 0, "Active": True, "FourChar": "", "Enabled": True,
                      "CustomTerminatorParsedIn": "\u0000", "CustomTerminatorIn": "", "OutboundQueueEmpty": True,
                      "CustomTerminatorParsedOut": "\u0000", "CustomTerminatorOut": ""}


def json_new_mmio(d, which, guid, four, used_ports):
    m = dict(JSON_MMIO_TEMPLATE)
    for e in d.values():            # copy the user's own entry layout when there is one
        m = {k: e[k] for k in e}
        break
    m.update(Guid=guid, FourChar=four, IP="127.0.0.1", Enabled=True, Active=True, Type=0)
    if which == "in":
        m.update(Port=IN_PORT, Direction=0, FormatIn=1, TerminatorIn=0)
    else:
        port = OUT_LISTEN
        while port in used_ports:
            port += 1
        m.update(Port=port, Direction=2, FormatIn=0, FormatOut=2, TerminatorOut=0,
                 UdpEndpointIP="127.0.0.1", UdpEndpointPort=OUT_PORT)
    d[guid] = m


def lib_from_json_db(db):
    """'Thetis, Version=x' assembly name from the type names inside JSON settings"""
    for k in db.keys():
        if k.startswith("meterIGSettings_2_"):
            v = db.val(k)
            if v.startswith("2|"):
                try:
                    raw = gzip.decompress(base64.b64decode(v[2:].replace("[backslash]", "/")))
                except Exception:
                    continue
                m = re.search(rb"Thetis, Version=[0-9.]+, Culture=neutral, PublicKeyToken=null", raw)
                if m:
                    return m.group(0).decode()
    return None


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------
def containers_named(db, name):
    out = []
    for k in db.keys():
        if k.startswith("meterContData_"):
            f = db.val(k).split("|")
            if len(f) > 15 and f[15].split("\n")[0].strip().startswith(name):
                out.append(k[len("meterContData_"):])
    return out


def items_of(db, cid):
    return [db.val(k).split("|") for k in db.keys()
            if k.startswith("meterIGData_") and db.val(k).split("|")[1] == cid]


def container_rows(cid, notes, width, height, items, x=100, y=100, nfields=23):
    """items: list of (meter_type, settings, item_group_id). Rows for a floating container."""
    f = [cid, "1", str(x), str(y), str(width), str(height), "true", "0", "0", "TOPLEFT", "true", "true",
         "255.0.0.0", "false", "true", notes, "true", "true", "true", "true", "false", "false", "false"]
    f += ["false"] * max(0, nfields - len(f))         # SDR-VST3 has one more field
    cont = "|".join(f)
    rows = [("meterContData_" + cid, cont),
            ("meterData_" + cid, "%s|UCMeter_%s|1|1.0000|1.0000|-1|0.0040|0.0410|0.0500" % (cid, cid))]
    y = 0.0
    geo = {"TEXT_OVERLAY": (0.0, 0.0495), "CUSTOM_METER_BAR": (0.004, 0.0910), "OTHER_BUTTONS": (0.004, 0.36),
           "LED": (0.0, 0.0)}
    for order, item in enumerate(items):
        mt, setting, igid = item[:3]
        x, h = geo[mt]
        if len(item) > 3:
            h = item[3]          # explicit height (button box: 0.12 per row of buttons)
        rows.append(("meterIGData_" + igid, "%s|%s|ITEM_GROUP|%.4f|%.4f|%.4f|%.4f|%s|%d"
                     % (igid, cid, x, y, 1 - 2 * x, h, mt, order)))
        rows.append(("meterIGSettings_2_" + igid, setting))
        y += h
    return rows


DEFAULT_BUTTONS = ["opr", "atu", "ant", "amp", "tune", "atutune", "sot", "dummy",
                   "fault", "lpalarm", "lpmode", "fandn", "fanup"]
BUTTON_IDS = set(DEFAULT_BUTTONS) | {"gap"}


TM_DEFAULT = ["status", "freq", "fwd", "swr", "drive", "refl", "pa", "fault", "lpp", "lps", "lptext"]
TM_IDS = set(TM_DEFAULT) | {"title", "gap"}
TITLE_TELE = "KPA1500 {ver} Telemetry"
TITLE_CTRL = "KPA1500 {ver} Controls"


def clean_title(s, default):
    if not isinstance(s, str):
        return default
    s = "".join(c for c in s if 32 <= ord(c) < 127 and c not in "%|<>&").strip()[:48]
    return s or default


def _settings_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


KW_DEFAULT = ["title", "now", "knob", "b1", "b2", "b3", "status"]
KW_IDS = set(KW_DEFAULT) | {"gap"}
TITLE_KNOB = "FlexControl Knob {ver}"


def read_knob_window(path):
    """(enabled or None if never chosen, order, title) of the KPA1500 Knob window"""
    kw = _settings_json(path).get("thetis_knobwin")
    if not isinstance(kw, dict):
        return None, list(KW_DEFAULT), TITLE_KNOB
    order = [x for x in kw.get("order", []) if isinstance(x, str) and x in KW_IDS] if isinstance(kw.get("order"), list) else []
    order = [x for i, x in enumerate(order) if x == "gap" or x not in order[:i]][:20]
    if not [x for x in order if x != "gap"]:
        order = list(KW_DEFAULT)
    return kw.get("enabled", True) is not False, order, clean_title(kw.get("title"), TITLE_KNOB)


def read_meter_layout(path):
    """(order, title) of the Telemetry window from the bridge settings, or None"""
    tm = _settings_json(path).get("thetis_meters")
    if not isinstance(tm, dict) or not isinstance(tm.get("order"), list):
        return None
    order = [x for x in tm["order"] if isinstance(x, str) and x in TM_IDS]
    order = [x for i, x in enumerate(order) if x == "gap" or x not in order[:i]][:40]
    if not [x for x in order if x != "gap"]:
        return None
    return order, clean_title(tm.get("title"), TITLE_TELE)


def read_button_layout(path):
    """(order, columns) from the bridge settings file, or None for the standard layout"""
    try:
        with open(path, encoding="utf-8") as f:
            tb = json.load(f).get("thetis_buttons")
    except (OSError, ValueError, AttributeError):
        return None
    if not isinstance(tb, dict) or not isinstance(tb.get("order"), list):
        return None
    order = [x for x in tb["order"] if isinstance(x, str) and x in BUTTON_IDS]
    order = [x for i, x in enumerate(order) if x == "gap" or x not in order[:i]]
    try:
        cols = int(tb.get("columns", 4))
    except (TypeError, ValueError):
        cols = 4
    if not [x for x in order if x != "gap"]:
        return None
    return order, max(2, min(6, cols)), clean_title(tb.get("title"), TITLE_CTRL)


def lp100_from_settings(path):
    try:
        with open(path, encoding="utf-8") as f:
            return bool(json.load(f).get("lp100_port"))
    except (OSError, ValueError, AttributeError):
        return None


def build_containers(b, out4, lp100, layout=None, meters=None, knobwin=None):
    T = b.T
    STALE = "DateTimeOffset.UtcNow.ToUnixTimeSeconds() - %%%s.bridge_time%% > 5" % T
    ALIVE = "DateTimeOffset.UtcNow.ToUnixTimeSeconds() - %%%s.bridge_time%% <= 5" % T
    WARN = "!!  BRIDGE NOT RUNNING - data frozen  !!"
    CYAN, WHITE, ORANGE, GREEN, RED, TITLE = 0xFF00E5FF, 0xFFFFFFFF, 0xFFFFA500, 0xFF00FF00, 0xFFFF3030, 0xFFFFFF80
    FREQ = "AMP  %%%s.amp_freq_line%%  %%%s.rx_text%%"          # amp frequency (+ warning) + receive level
    FREQ_V = FREQ + "   %%%s.bridge_version%%"                  # ... + bridge version (Telemetry)
    NOANT = "%%%s.noant_text%%"                                       # 'no antenna?' warning
    NOANT_ON = "(%%%s.noant%% == 1) && %s" % (T, ALIVE)

    def ig():
        return str(uuid.uuid4())

    # ---- Telemetry
    tw = ig()
    tm_order, tele_title = meters if meters else (TM_DEFAULT, TITLE_TELE)
    ctrl_title = layout[2] if (layout and len(layout) > 2) else TITLE_CTRL
    ver_in_title = "title" in tm_order and "{ver}" in tele_title

    def title_text(s):
        return s.replace("%", "").replace("{ver}", "%%%s.bridge_version%%" % T)

    TEL = {
        "title": lambda: ("TEXT_OVERLAY", b.text(title_text(tele_title), TITLE, 12.0, True), ig()),
        "status": lambda: ("TEXT_OVERLAY", b.text("KPA1500  %%%s.amp_power%%  %%%s.amp_mode%%   ATU %%%s.amp_atu%%   ANT %%%s.amp_ant%%"
                                                  % (T, T, T, T), CYAN), ig()),
        "freq": lambda: ("TEXT_OVERLAY", b.text((FREQ % (T, T)) if ver_in_title else (FREQ_V % (T, T, T)),
                                                GREEN, 12.0, True), ig()),
        "fwd": lambda: ("CUSTOM_METER_BAR", b.bar("amp_fwd", "KPA1500 Fwd", "W", 0, 1750, 1500), ig()),
        "swr": lambda: ("CUSTOM_METER_BAR", b.bar("amp_swr", "KPA1500 SWR", ":1", 1, 3, 2), ig()),
        "drive": lambda: ("CUSTOM_METER_BAR", b.bar("amp_drive", "Drive", "W", 0, 100, 80), ig()),
        "refl": lambda: ("TEXT_OVERLAY", b.text("Refl %%%s.amp_refl%% W   Gain %%precis=1%%%%%s.amp_gain_db%% dB   Drive from %%%s.amp_drive_src%%"
                                                % (T, T, T), WHITE), ig()),
        "pa": lambda: ("TEXT_OVERLAY", b.text("PA %%%s.amp_temp%% C   %%precis=1%%%%%s.amp_volts%% V   %%%s.amp_amps%% A   Fan %%%s.amp_fan%% (min %%%s.amp_fan_min%%)"
                                              % (T, T, T, T, T), WHITE), ig()),
        "fault": lambda: ("TEXT_OVERLAY", b.text("Fault: %%%s.amp_fault_text%%   Stby-on-Tune: %%%s.amp_sot_text%%" % (T, T), ORANGE), ig()),
        "lpp": lambda: ("CUSTOM_METER_BAR", b.bar("lp_pwr", "LP-100A Pwr", "W", 0, 1750, 1500), ig()),
        "lps": lambda: ("CUSTOM_METER_BAR", b.bar("lp_swr", "LP-100A SWR", ":1", 1, 3, 2), ig()),
        "lptext": lambda: ("TEXT_OVERLAY", b.text("LP-100A  %%precis=1%%R %%%s.lp_r%%  X %%%s.lp_x%%  |Z| %%%s.lp_z%%  RL %%%s.lp_rl%% dB"
                                                  % (T, T, T, T), GREEN), ig()),
        "gap": lambda: ("TEXT_OVERLAY", b.text(" ", WHITE), ig()),
    }
    tele = [TEL[x]() for x in tm_order if x in TEL and not (x.startswith("lp") and not lp100)]
    tn = ig()
    tele.append(("TEXT_OVERLAY", b.text(NOANT % T, RED, 12.0, True, gate=led_fourchar(tn)), ig()))
    tele.append(("TEXT_OVERLAY", b.text(WARN, RED, 12.0, True, gate=led_fourchar(tw)), ig()))
    tele.append(("LED", b.led(STALE, led_fourchar(tw)), tw))
    tele.append(("LED", b.led(NOANT_ON, led_fourchar(tn)), tn))

    # ---- Controls
    leds = {}

    def L(key, cond):
        g = ig()
        leds[key] = (g, "(%s) && %s" % (cond, ALIVE))
        return led_fourchar(g)

    def B(on, off, mon, moff, four=None):
        return dict(on=on, off=off, msg_on=mon, msg_off=moff, led=four)

    CAT = {
        "opr": B("OPERATE", "STANDBY", "KPA_STANDBY", "KPA_OPERATE", L("OPR", "%%%s.amp_operate%% == 1" % T)),
        "atu": B("ATU IN", "ATU BYP", "KPA_ATU_BYPASS", "KPA_ATU_INLINE", L("ATU", "%%%s.amp_atu_inline%% == 1" % T)),
        "ant": B("ANT 1", "ANT 2", "KPA_ANT_TOGGLE", "KPA_ANT_TOGGLE", L("ANT", "%%%s.amp_ant%% == 1" % T)),
        "amp": B("AMP ON", "AMP OFF", "KPA_POWER_OFF", "KPA_POWER_ON", L("ONL", "%%%s.amp_online%% == 1" % T)),
        "tune": B("STOP TUNE", "TUNE", "KPA_RADIO_TUNE_STOP", "KPA_RADIO_TUNE", L("RTN", "%%%s.amp_tune_active%% == 1" % T)),
        "atutune": B("STOP ATU", "ATU TUNE", "KPA_ATU_CANCEL", "KPA_ATU_TUNE", L("TUN", "%%%s.amp_atu_tuning%% == 1" % T)),
        "sot": B("SoT ON", "SoT OFF", "KPA_SOT_OFF", "KPA_SOT_ON", L("SOT", "%%%s.amp_sot%% == 1" % T)),
        "dummy": B("DUMMY OK", "DUMMY OK", "KPA_DUMMY_OK", "KPA_DUMMY_OK", L("DMY", "%%%s.dummy_ok%% == 1" % T)),
        "fault": B("FAULT - CLR", "NO FAULT", "KPA_CLEAR_FAULT", "KPA_CLEAR_FAULT", L("FLT", "%%%s.amp_fault%% != 0" % T)),
        "lpalarm": B("LP ALARM", "LP ALARM", "KPA_LP_ALARM", "KPA_LP_ALARM"),
        "lpmode": B("LP MODE", "LP MODE", "KPA_LP_MODE", "KPA_LP_MODE"),
        "fandn": B("FAN -", "FAN -", "KPA_FAN_DOWN", "KPA_FAN_DOWN"),
        "fanup": B("FAN +", "FAN +", "KPA_FAN_UP", "KPA_FAN_UP"),
    }
    order, cols = (layout[0], layout[1]) if layout else (DEFAULT_BUTTONS, 4)
    spec = []
    for bid in order:
        if bid == "gap":
            spec.append(dict(on="", off="", msg_on="", msg_off="", led=None, gap=True))
        elif bid in CAT and not (bid.startswith("lp") and not lp100):
            spec.append(CAT[bid])
    while spec and spec[-1].get("gap"):
        spec.pop()
    spec = spec[:31]
    rows = max(1, (len(spec) + cols - 1) // cols)
    cw = ig()
    ctrl = [
        ("TEXT_OVERLAY", b.text(title_text(ctrl_title), TITLE, 12.0, True), ig()),
        ("TEXT_OVERLAY", b.text("%%%s.amp_power%%  %%%s.amp_mode%%   Fan %%%s.amp_fan%% (min %%%s.amp_fan_min%%)   Fault: %%%s.amp_fault_text%%"
                                % (T, T, T, T, T), CYAN), ig()),
        ("TEXT_OVERLAY", b.text(FREQ % (T, T), GREEN, 12.0, True), ig()),
        ("OTHER_BUTTONS", b.buttons(spec, cols, out4), ig(), 0.12 * rows * 4.0 / max(cols, 4)),
    ]
    cn = ig()
    ctrl.append(("TEXT_OVERLAY", b.text(NOANT % T, RED, 12.0, True, gate=led_fourchar(cn)), ig()))
    ctrl.append(("TEXT_OVERLAY", b.text(WARN, RED, 12.0, True, gate=led_fourchar(cw)), ig()))
    ctrl.append(("LED", b.led(NOANT_ON, led_fourchar(cn)), cn))
    for key, (g, cond) in leds.items():
        ctrl.append(("LED", b.led(cond, led_fourchar(g)), g))
    ctrl.append(("LED", b.led(STALE, led_fourchar(cw)), cw))

    # ---- Knob (what the FlexControl knob and buttons do right now)
    knob = None
    if knobwin:
        kw_order, kw_title = knobwin
        KW = {
            "title": lambda: ("TEXT_OVERLAY", b.text(title_text(kw_title), TITLE, 12.0, True), ig()),
            "now": lambda: ("TEXT_OVERLAY", b.text("KNOB:  %%%s.knob_now%%" % T, GREEN, 12.0, True), ig()),
            "knob": lambda: ("TEXT_OVERLAY", b.text("Knob   %%%s.knob_k%%" % T, WHITE), ig()),
            "b1": lambda: ("TEXT_OVERLAY", b.text("B1     %%%s.knob_b1%%" % T, WHITE), ig()),
            "b2": lambda: ("TEXT_OVERLAY", b.text("B2     %%%s.knob_b2%%" % T, WHITE), ig()),
            "b3": lambda: ("TEXT_OVERLAY", b.text("B3     %%%s.knob_b3%%" % T, WHITE), ig()),
            "status": lambda: ("TEXT_OVERLAY", b.text("%%%s.knob_status%%" % T, CYAN), ig()),
            "gap": lambda: ("TEXT_OVERLAY", b.text(" ", WHITE), ig()),
        }
        kwl = ig()
        knob = [KW[x]() for x in kw_order if x in KW]
        knob.append(("TEXT_OVERLAY", b.text(WARN, RED, 12.0, True, gate=led_fourchar(kwl)), ig()))
        knob.append(("LED", b.led(STALE, led_fourchar(kwl)), kwl))
    return tele, ctrl, knob


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def backup_dir(here):
    sub = PROGRAMS[PROG[0]]["backups"] if PROG[0] else ""
    return os.path.join(here, "thetis_backups", sub) if sub else os.path.join(here, "thetis_backups")


def backup(db_path, here):
    d = backup_dir(here)
    os.makedirs(d, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(d, "database_%s.xml" % stamp)
    n = 1
    while os.path.exists(dst):
        n += 1
        stamp2 = "%s_%d" % (stamp, n)
        dst = os.path.join(d, "database_%s.xml" % stamp2)
    stamp = os.path.basename(dst)[len("database_"):-4]
    shutil.copy2(db_path, dst)
    with open(os.path.join(d, "database_%s.source.txt" % stamp), "w", encoding="utf-8") as f:
        f.write(db_path + "\n")
    return dst


def latest_backup(here):
    d = backup_dir(here)
    if not os.path.isdir(d):
        return None, None
    xs = sorted(x for x in os.listdir(d) if x.startswith("database_") and x.endswith(".xml"))
    if not xs:
        return None, None
    b = os.path.join(d, xs[-1])
    src = None
    try:
        with open(b[:-4] + ".source.txt", encoding="utf-8") as f:
            src = f.read().strip()
    except OSError:
        pass
    return b, src


def wait_for_thetis_closed(auto, close=False):
    if thetis_running():
        path = thetis_path()
        say("")
        say("%s is running. It must be closed first, because it saves its own settings" % pname())
        say("when it closes and would overwrite the change.")
        if close or (not auto and ask_yn("Close %s for you now? It closes the normal way and saves its " % pname() +
                                         "settings (like File > Exit). Make sure the radio is not transmitting.",
                                         True, False)):
            say("Closing %s ..." % pname())
            if close_thetis():
                say("%s is closed." % pname())
                if path:
                    CLOSED_THETIS.append((path, PROGRAMS[PROG[0]]["args"] if PROG[0] else [], pname()))
            else:
                say("%s did not close by itself (it may be showing a question). Please close it." % pname())
    n = 0
    while thetis_running():
        n += 1
        say("")
        say("%s is running. Please close it now (it saves its settings when it closes)." % pname())
        if auto or n > 20:
            say("%s is still running - stopping without changing anything." % pname())
            return False
        pause(False)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description="Put the KPA1500 containers into Thetis")
    ap.add_argument("--db", help="path of the Thetis database.xml (default: the active one)")
    ap.add_argument("--check", action="store_true", help="only report, change nothing")
    ap.add_argument("--restore", action="store_true", help="put back the most recent backup")
    ap.add_argument("--remove", action="store_true", help="take the KPA1500 containers out of Thetis")
    ap.add_argument("--yes", action="store_true", help="no questions (use the defaults)")
    ap.add_argument("--no-lp100", action="store_true", help="leave out the LP-100A meters and buttons")
    ap.add_argument("--lp100", action="store_true", help="include the LP-100A meters and buttons")
    ap.add_argument("--backup-dir", help="folder for backups (default: next to this file)")
    ap.add_argument("--settings", help="bridge settings file with the button order "
                                       "(default: kpa1500_settings.json next to this file)")
    ap.add_argument("--close-thetis", action="store_true",
                    help="close a running Thetis without asking (the normal way, it saves its settings)")
    ap.add_argument("--no-restart", action="store_true",
                    help="do not offer to start Thetis again at the end (the caller does it)")
    ap.add_argument("--program", help="which program: thetis, hl2 (Thetis HL2), vst3 (SDR-VST3), "
                                      "a list like thetis,vst3, or all (default: ask)")
    ap.add_argument("--list-programs", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--skip-running-check", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.no_restart:
        NO_RESTART[:] = [True]
    here = a.backup_dir or os.path.dirname(os.path.abspath(__file__))

    if a.list_programs:                 # for the installer: key|name|settings file|program file
        for p in PROG_ORDER:
            print("%s|%s|%s|%s" % (p, PROGRAMS[p]["name"], program_db(p) or "", program_exe(p) or ""))
        return 0

    say("KPA1500 Thetis container setup  v%s   (design: KW4EX)" % SETUP_VERSION)
    say("=" * 60)

    if a.db:
        progs = [None]
    elif a.program:
        want = [x.strip().lower() for x in a.program.split(",") if x.strip()]
        alias = {"thetis-hl2": "hl2", "hl2": "hl2", "sdr-vst3": "vst3", "vst3": "vst3", "vst": "vst3",
                 "thetis": "thetis"}
        if "all" in want:
            progs = installed_programs() or ["thetis"]
        else:
            bad = [x for x in want if x not in alias]
            if bad:
                say("Unknown program: %s (use thetis, hl2, vst3 or all)" % ", ".join(bad))
                return 1
            progs = []
            for x in want:
                if alias[x] not in progs:
                    progs.append(alias[x])
    else:
        inst = installed_programs()
        if len(inst) <= 1:
            progs = inst or ["thetis"]
        elif a.yes:
            progs = ["thetis"] if "thetis" in inst else inst[:1]
        else:
            progs = choose_programs(inst, "restore the backup of" if a.restore else
                                    ("remove the windows from" if a.remove else
                                     ("check" if a.check else "set up")))
            if not progs:
                say("Nothing chosen - nothing changed.")
                return 0
    if len(progs) > 1:
        DEFER_RESTART[:] = [True]
    rc = 0
    for prog in progs:
        PROG[0] = prog
        if len(progs) > 1:
            say("")
            say("==================== %s ====================" % pname())
        r = run_one(a, here)
        rc = rc or r
    if DEFER_RESTART:
        offer_thetis_restart(a.yes, final=True)
    return rc


def choose_programs(inst, verb):
    say("")
    say("Programs found on this computer:")
    for i, p in enumerate(inst, 1):
        say("  %d  %s" % (i, PROGRAMS[p]["name"]))
    say("  A  all of them")
    while True:
        try:
            x = input("Which one(s) to %s? Type a number, several like 1,3, or A (Enter = 1): " % verb).strip()
        except EOFError:
            x = ""
        if not x:
            return inst[:1]
        if x.lower() in ("a", "all"):
            return list(inst)
        try:
            nums = [int(y) for y in x.replace(" ", ",").split(",") if y]
        except ValueError:
            nums = []
        if nums and all(1 <= n <= len(inst) for n in nums):
            out = []
            for n in nums:
                if inst[n - 1] not in out:
                    out.append(inst[n - 1])
            return out
        say("Please type one of the numbers shown, or A.")


def run_one(a, here):
    if a.restore:
        b, src = latest_backup(here)
        if not b:
            say("No %s backup found in %s." % (pname(), backup_dir(here)))
            return 1
        target = a.db or src or find_active_db()
        say("Backup:  %s" % b)
        say("Restore to: %s" % target)
        if not a.skip_running_check and not wait_for_thetis_closed(a.yes, a.close_thetis):
            return 1
        if not ask_yn("Put this backup back (your current %s settings are replaced)?" % pname(), True, a.yes):
            say("Nothing changed.")
            return 0
        shutil.copy2(b, target)
        say("Done.")
        offer_thetis_restart(a.yes)
        if not CLOSED_THETIS:
            say("Start %s again." % pname())
        return 0

    path = a.db or find_active_db()
    if not path:
        say("Could not find the %s settings (database.xml)." % pname())
        say("Is %s installed, and has it been started at least once?" % pname())
        say("You can name the file yourself:  --db \"C:\\...\\database.xml\"")
        return 1
    say("%s settings: %s" % (pname(), path))
    if not a.check and not a.skip_running_check and not wait_for_thetis_closed(a.yes, a.close_thetis):
        return 1

    db = ThetisDB(path)
    if "</Options>" not in db.text:
        say("")
        say("This %s settings file has never been used (it has no saved options yet)." % pname())
        say("Start %s once, set up your radio, close it, then run this again." % pname())
        return 1
    ver, vs = version_tuple(db)
    if vs:
        say("Program version: %s" % vs.split("--")[0].strip())
    if ver and ver < (2, 10, 3, 12):
        say("")
        say("This %s is older than Thetis 2.10.3.12. The containers need 2.10.3.12 or newer" % pname())
        say("(macro buttons and LED scripts). Please update it first.")
        return 1
    tci_note(db)
    lib = thetis_lib_name(db)
    jflav = db.has("multimeter_io2") and mmio_is_json(db.val("multimeter_io2"))
    if jflav:
        lib = lib or lib_from_json_db(db)
        say("Settings format: newer JSON style (SDR-VST3)")

    # ---- Multi Meter I/O
    tpl_mmio = blob_decode(TEMPLATES["MMIO"])
    if jflav:
        jpre, mm = json_blob_decode(db.val("multimeter_io2"))
        ents = json_mmio_entries(mm)
    else:
        if db.has("multimeter_io2"):
            mm = blob_decode(db.val("multimeter_io2"))
        else:
            mm = copy.deepcopy(tpl_mmio)
            mm.get("m_serializationArray").vals = []
        if lib:
            set_lib_version(mm, lib)
            set_lib_version(tpl_mmio, lib)
        ents = mmio_entries(mm)
    used4 = {e["four"] for e in ents}
    used_ports = {e["port"] for e in ents}
    ent_in = [e for e in ents if e["type"] == 0 and e["port"] == IN_PORT and e["dir"] in (0, 2)]
    ent_out = [e for e in ents if e["type"] == 0 and e["dir"] in (1, 2) and e["ep_port"] == OUT_PORT]
    say("")
    say("Multi Meter I/O (Setup > Serial/Network/Midi CAT > Multi Meter I/O):")
    plan = []
    if ent_in:
        e = ent_in[0]
        say("  readings IN : found '%s' on %s:%d%s" % (e["four"], e["ip"], e["port"],
                                                    "" if e["fin"] == 1 else "  (format will be set to XML)"))
        if e["fin"] != 1:
            plan.append("fix_in_format")
        in4, in_guid = e["four"], e["guid"]
    else:
        in4 = "KPAt" if "KPAt" not in used4 else fourchar("127.0.0.1", IN_PORT, uuid.uuid4())
        in_guid = str(uuid.uuid4())
        say("  readings IN : none on port %d - will add '%s' (UDP, 127.0.0.1:%d, XML)" % (IN_PORT, in4, IN_PORT))
        plan.append("add_in")
    if ent_out:
        e = ent_out[0]
        say("  buttons OUT : found '%s' sending to %s:%d%s" % (e["four"], e["ep_ip"], e["ep_port"],
                                                             "" if e["fout"] == 2 else "  (format will be set to Raw)"))
        if e["fout"] != 2:
            plan.append("fix_out_format")
        out4 = e["four"]
    else:
        out4 = "KPAb" if "KPAb" not in used4 else fourchar("127.0.0.1", OUT_PORT, uuid.uuid4())
        say("  buttons OUT : none sending to port %d - will add '%s' (UDP, sends to 127.0.0.1:%d, Raw)" % (OUT_PORT, out4, OUT_PORT))
        plan.append("add_out")
    for e in ent_in[:1] + ent_out[:1]:
        if not e["enabled"]:
            say("  note: entry '%s' is not ticked Enabled - it will be enabled." % e["four"])
            plan.append("enable:" + e["four"])

    old_ctrl, old_tele = containers_named(db, "KPA1500 Controls"), containers_named(db, "KPA1500 Telemetry")
    old_knob = containers_named(db, "KPA1500 Knob")
    say("")
    say("Containers:")
    say("  KPA1500 Telemetry : %s" % ("found - will be replaced" if old_tele else "will be added"))
    say("  KPA1500 Controls  : %s" % ("found - will be replaced" if old_ctrl else "will be added"))
    if old_knob:
        say("  KPA1500 Knob      : found")

    if a.check:
        say("")
        say("--check: nothing was changed.")
        return 0

    if a.remove:
        if not (old_ctrl or old_tele or old_knob):
            say("There are no KPA1500 containers to remove.")
            return 0
        if not ask_yn("Remove the KPA1500 containers from %s?" % pname(), True, a.yes):
            return 0
        bk = backup(path, here)
        say("Backup saved: %s" % bk)
        db.begin()
        for cid in old_ctrl + old_tele + old_knob:
            for p in items_of(db, cid):
                db.delete("meterIGData_" + p[0])
                db.delete("meterIGSettings_2_" + p[0])
            db.delete("meterContData_" + cid)
            db.delete("meterData_" + cid)
        write_db(db)
        say("Removed.")
        offer_thetis_restart(a.yes)
        if not CLOSED_THETIS:
            say("Start %s again." % pname())
        return 0

    spath = a.settings or os.path.join(os.path.dirname(os.path.abspath(__file__)), "kpa1500_settings.json")
    layout = read_button_layout(spath)
    meters = read_meter_layout(spath)
    kw_on, kw_order, kw_title = read_knob_window(spath)
    if kw_on is None and "kw" in ANSWERS:
        kw_on = ANSWERS["kw"]
    if kw_on is None:
        kw_on = ask_yn("Add the KPA1500 Knob window (shows what a FlexControl tuning knob and its buttons "
                       "do right now)?", bool(old_knob), a.yes)
        ANSWERS["kw"] = kw_on
    say("")
    say("KPA1500 Knob window: %s" % ("yes (title '%s')" % kw_title if kw_on else "no"))
    if meters:
        say("Telemetry window: your order from the bridge settings (title '%s'):" % meters[1])
        say("  " + ", ".join(meters[0]))
    if layout:
        say("Controls buttons: your order from the bridge settings (%d columns):" % layout[1])
        say("  " + ", ".join(layout[0]))
    else:
        say("Controls buttons: standard order (change it in the bridge panel: Settings > Thetis Controls buttons)")
    if a.lp100:
        lp100 = True
    elif a.no_lp100:
        lp100 = False
    else:
        lp_def = lp100_from_settings(spath)
        if "lp" in ANSWERS:
            lp100 = ANSWERS["lp"]
        else:
            lp100 = ask_yn("Do you use a Telepost LP-100A wattmeter with the bridge (adds its meters and buttons)?",
                           True if lp_def is None else lp_def, a.yes)
            ANSWERS["lp"] = lp100
    say("")
    if not ask_yn("Make these changes now? (a backup is saved first)", True, a.yes):
        say("Nothing changed.")
        return 0

    bk = backup(path, here)
    say("Backup saved: %s" % bk)

    db.begin()
    # MMIO changes
    if jflav:
        if "add_in" in plan:
            json_new_mmio(mm, "in", in_guid, in4, used_ports)
        if "add_out" in plan:
            json_new_mmio(mm, "out", str(uuid.uuid4()), out4, used_ports | {IN_PORT})
        for e in json_mmio_entries(mm):
            if e["four"] == in4 and "fix_in_format" in plan:
                e["node"]["FormatIn"] = 1
            if e["four"] == out4 and "fix_out_format" in plan:
                e["node"]["FormatOut"] = 2
            if ("enable:" + e["four"]) in plan:
                e["node"]["Enabled"] = True
                e["node"]["Active"] = True
        if any(p.startswith(("add_", "fix_", "enable:")) for p in plan):
            db.set_val("multimeter_io2", json_blob_encode(jpre, mm))
    arr = None if jflav else mm.get("m_serializationArray")
    if arr is None:
        pass
    elif "add_in" in plan:
        arr.vals.append(new_mmio(tpl_mmio, "in", in_guid, in4, used_ports))
    if arr is not None and "add_out" in plan:
        arr.vals.append(new_mmio(tpl_mmio, "out", str(uuid.uuid4()), out4, used_ports | {IN_PORT}))
    for e in ([] if arr is None else mmio_entries(mm)):
        if e["four"] == in4 and "fix_in_format" in plan:
            set_enum(e["node"].get("_format_in"), 1)
        if e["four"] == out4 and "fix_out_format" in plan:
            set_enum(e["node"].get("_format_out"), 2)
        if ("enable:" + e["four"]) in plan:
            e["node"].set("_enabled", P(1, True))
            e["node"].set("_listener_active", P(1, True))
    if arr is not None and any(p.startswith(("add_", "fix_", "enable:")) for p in plan):
        blob = blob_encode(mm)
        if db.has("multimeter_io2"):
            db.set_val("multimeter_io2", blob)
        else:
            db.insert_at_options_end([("multimeter_io2", blob)])

    # containers (keep the old ids so windows stay where they were)
    b = Builder(lib, in_guid, in4, json_out=jflav)
    tele, ctrl, knob = build_containers(b, out4, lp100, layout, meters, (kw_order, kw_title) if kw_on else None)
    cid_t = old_tele[0] if old_tele else str(uuid.uuid4())
    cid_c = old_ctrl[0] if old_ctrl else str(uuid.uuid4())
    cid_k = (old_knob[0] if old_knob else str(uuid.uuid4())) if knob else None
    for cid in old_ctrl + old_tele + old_knob:
        for p in items_of(db, cid):
            db.delete("meterIGData_" + p[0])
            db.delete("meterIGSettings_2_" + p[0])
        if cid not in (cid_t, cid_c, cid_k):
            db.delete("meterContData_" + cid)
            db.delete("meterData_" + cid)
    todo = [(cid_t, TELE_NAME, 360, 370, tele, old_tele, 100, 100), (cid_c, CTRL_NAME, 420, 380, ctrl, old_ctrl, 100, 100)]
    if knob:
        todo.append((cid_k, KNOB_NAME, 400, 200, knob, old_knob, 100, 520))
    for cid, name, w, h, items, old, x, y in todo:
        rows = container_rows(cid, name, w, h, items, x, y, 24 if jflav else 23)
        if old:
            f = db.val("meterContData_" + cid).split("|")
            f[15] = name
            db.set_val("meterContData_" + cid, "|".join(f))
            db.insert_after("meterData_" + cid, rows[2:])
        else:
            db.insert_at_options_end(rows)
    write_db(db)

    say("")
    say("Done. What was set up:")
    say("  Multi Meter I/O: readings IN '%s' (port %d), buttons OUT '%s' (to port %d)" % (in4, IN_PORT, out4, OUT_PORT))
    say("  Containers: %s, %s%s" % (TELE_NAME, CTRL_NAME, (", " + KNOB_NAME) if knob else ""))
    say("")
    say("If anything looks wrong: Undo_Thetis_Containers.bat puts the backup back.")
    say("")
    offer_thetis_restart(a.yes)
    if not CLOSED_THETIS:
        say("Next: start the bridge (start_kpa_bridge.bat) if it is not running, then start %s." % pname())
    return 0


def write_db(db):
    new = db.commit_text()
    # check: still readable, all our rows parse
    import xml.etree.ElementTree as ET
    ET.fromstring(new.encode("utf-8"))
    tmp = db.path + ".kpa_tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new)
    os.replace(tmp, db.path)


# Item templates taken from working Thetis containers (Thetis 2.10.3.15).
TEMPLATES = {
    'TEXT_OVERLAY': (
        '1|H4sIAAAAAAAEAOVW7WscRRjfSXKbvUustaSkxWJWJMXiedzmpWmrh4nXF7CVprnQfghhOrf7XG57+3Kdnc1l88X[backsl'
        'ash]of9MQUREBBFBEEQQRASllIJY8EMpCOk8czvp9VoEUcHSO+5u53l+87z+npkziGEYe[backslash]KFv[backslash]ga'
        'w4ffSSNLBISVehwE4Ao[backslash]jhL5HLkp5xCJgcezvtIynl2f29jIdzUE96Otsh0mbswDv1m2rwJPJKy2UKniu2zX00'
        'CkHGoRpIKzoGyvps3Ady9Cth53IKo1l5bYort40jk9vwDVU6c3y9r45eYNGdG[backslash]a3yzIJMuhdSNwy7jwA+GtAPZ'
        'GrRZ0q7HaSSmQpoA91ng7zLMd4Vzls0MCfer4maXYBuC6WE96zLXF9moMWoYt55V4gsQyQ2u[backslash]j13M5WbRVbPw7'
        'ru[backslash]Kcl3rTu[backslash]FVQcuNVFqSwynz+XDd7Y9OyiiM534uyF8YR+akj+VH6f+wMRjqOkRo4nsfkZ[back'
        'slash]TXF6JXY3[backslash]Ks+nFSBW5NyqPncI2pkNGTDyUDtBWHAnaYqEfZNQxTSSCdOaxiJGHsjZYH9NCIlPIoOGyAK'
        'wJJMpkSX5dyQM+y1lPlePvx1mdby22llqO4y1W2TwjD7RT9DJBhS8CkH2JeXES6fmHVh+Qq5do209EzLM+4GUE3NeAV+RqSs'
        'COiLeBByyjfIcuuG3GzUOK6L9p4JRcHaRJ2vyQ8Q5wtJXy4mHE3NOYabmaHjQmdmgAXhBv+a5FDHJXA4+qqJN23OtbQ+UdrX'
        'xVeeoC6yhK5Z5w3MgvGvOayiscDGUGAT9rwOtY[backslash]H7bEn8X6Bz2Y3mF[backslash]KQBb0iAST1wWdZvFflRq2'
        'axvfIaUmaPo+oHrXpT+e2bFVkg7RZPIOB7DXhLkQAzW8+6gHl9p1VvD+91ihXc+60GVJVfrB+dMx25WFoJu7Ztn+959mrcA3'
        '7GnhVMbFVY2KWtnjdrX5PaxrW1AXHS47M2+UabnH9s0jEX[backslash]onJr7XJk2ogJM3fh10IBrhOvtKQU0PFd[backsl'
        'ash]Lif6kBZ54aqjnzncGh+kIjayoBJgRzO7mbz7XuPVXrFvPgcrS+g7X+TKtWlINEXvXAV5nnybGzJj668W6NfKoh9WGy8i'
        'fJ+okGnhtws6bcfKxVF4anR+TTU8TpIbc17oN+OJIVDT1AFiEFnNWpJw+HihrSMbwGIxZC[backslash]xgqdaK4FylNIZF9'
        'Abx[backslash]iuPjeLiUZL[backslash]2+rfoiEFwuCefkh7W0uXl5cfSY8+Uzmgp[backslash]iPdlx7fl+4Z+9ICUv'
        '[backslash]IUPznZVMbSG+McVyFT6lhlfKbniDlT+SLiUfoXHcF+QoAAA=='),
    'CUSTOM_METER_BAR': (
        '1|H4sIAAAAAAAEAOVW[backslash]2scRRTf3SR3e7nYJjZai1gPSamY5LxLck2rxDTJxSRNjqS5pBVDmMztTe62t1[backs'
        'lash]i7G7Sq4jF3[backslash]zBX[backslash]wLRIRSQSiCIIooIopfCvlBECoiQikiEhFBRIT43mTnklyCRVBQOsfd7c'
        '7nM2[backslash]ee[backslash]N5b1dRFUXZhIH[backslash]OBrx4ic1X[backslash]V8ZidHXMtihm+6jgfXjhFwzh'
        'x[backslash]x2XWFCjl1aWehYVwVd7nplPqStie4XLLLHQlzjHuAW2gL5nCT1diJLD8gLMBhwU+p1ZXYiYoWKYxyapzboU5'
        'A4X+fpoxMifSp3r7WOrkqcUuaXy6cAE8+meNLzZB0M02MVx7hXLGW21SYdVZVqZeecQNHL[backslash]dJh7jJrXMSxTjHe'
        'KcVh+sm6xlxahOsVVmHa7H6Qo1TL[backslash]aoDQoysv7pXiMObDAkP+jzwSw2K+OhG4tpf[backslash]VFC[backsla'
        'sh]qt[backslash]7KKVh4jloBm6Em[backslash]18f9sKirse0UO8xOAslBd8XUfw4+188GfQ0ip4qWJ4d8G24eUecVeMf'
        '0JvujFBRew3QdppWMRxVi2BTaiVl0[backslash]NdXs0GXDQRXX[backslash]loKK0RAE7G3qT5XRNxPr3nUj1LmeW+5fT'
        '6WImRXup+jtkGzMe0VFw0A8tN+CxGOruVwnF4e4IdLaSDf2fFfOuZRbHzRI0SkFuQfIvknwAY7CZzzgxAgjEJrbp6HGh5J8l'
        'qRXu4sQ3fYuhER5rQ3hDwocE7JXdtRzlFcZ1VVV[backslash]lOA9ALYRaNT5oDDhFE2DQrY8pPwgKYf3+kAvgg8f3Miq30'
        'vSEewFZJkW2bQze1FXFfWWhO7fAc0J6KaEHgDoLjCHboXxH0Xfv5OEBD5YyAqjFSFS9OtbiT0E2EESrBSpzyYglRwOXtd7cP'
        '03ktMhNFBbH+5xDDk3JOc43N29K77AMX0v8jB2tPPU99WvJPURmGohtm26pBSYRZKKdeJMN[backslash]wMzpWZb3rbKupJ'
        'plPJ3mQ6c3sdOYFlqV[backslash]KTZIiXwXK837VYrFH0dl1CaZFQMKDVQoPx4LFSDqCQce7u7OjTw7NT811d6vXJb8Pz3'
        'dXcGXQGpze5Xez6ueSdQJYEVJkBq3q8etfTD2hfiqhkwDdS8yS43I2Xl9L61BH6ieS+tieRApNRh7H+cmZoUQ6k0olZtw1xh'
        'PTga9+JBcOiGqBTFOjIvYfVD+U2KCQSFjFW[backslash]I+jRl5XxKGxZmgvEP3UCTvSTQLaLsQwLhrFXM1pfHYKFp5R[ba'
        'ckslash]LGRNLRylx1RejsbQlNCA2WwxLlsTO48C2JTomFlru2BeYQfFOC0wKUu6PVaxI6K1IulBabxUVvSGQO7u6raxBTW['
        'backslash]ZBu[backslash]PIfV1yz9cJMh17CvErEn9ayMULCrmdNbaAnNckB5+OB0ToeclDT1+VONlHcqnIEu7sU7+UpP'
        'YKWV4rNmGba9[backslash]dU5MiKY1YSA612VZrbq447pojkCYPLDB8JseiUezJzYlEYnPrzUJTVOyFaLU5fNlQnofnt9pW'
        'P9uHs0drs+BzzcKxeu5LwG3Eoo2Hno5B2rCfaoRqpKARQyNFjTCNLGukpJGyRkyNXNBIRdkeejSqheNj0vtc5lJ+8spnI18f'
        'enb9tyYs1+NbvSCZw1rIUYeWGO80LG+Y8gnYs3M4rG0MPCpyQoiid4evJ+pp6fTGCxuXa6GMboeyI8Az+87m5OzmjmQ0ocw6'
        'buPaPApyr1841Pn6ZPZg4lFwncreoS5I+tWrV2tOxP8E8GlhsDINAAA='),
    'OTHER_BUTTONS': (
        '1|H4sIAAAAAAAEAOWaB3RU1brHvw1pJwkpEHpHFCmBVBLASAlFpJOIIsIwmZwhA1NgCiRYsF4bAhcLihe7XHt5dkVARFSKAh'
        'cRBUSlCgoCgqiAd[backslash]9nzubLnue9ZtZ6b6331tXFj3327N[backslash][backslash]O2effc6cOUqCiH6X[bac'
        'kslash]+Bv[backslash]BOHxhFRVhMImp7upT6323QEXT5vQLa9jpDfb3qDtZoDXeFP7f6ayXkTJlhWWdDv8k7p1s4TcPj8'
        'bldFt3bjTH9ADisp6J6Df7u1Kw25gyG[backslash]WeI1Q0G[backslash]3d2t3ehQhdvlGGbWlPummd6SiqIie6GjsGdu'
        'r[backslash]wCM6e418RuKnxUxVS5R[backslash]+z4RPj5UEne2wOn2e63W[backslash]6Mzy2aWbNWLPKHqgq9YW8wS'
        'yPLWD6XXa3a7Ydx9vf77fXtI3qPDcrjprh5kzT3TT6c[backslash]t0u8MVrKlP9YkW[backslash]tEUDzG9UnCovwfNCE'
        'k5WFNq7dbk3P[backslash]VKZ6YtO[backslash]f7ZQUx9ndIXO03eX[backslash]f32yJ0xMSjLqWevdkOeCcuSf27H4'
        '0ft[backslash]8cxgTxOxp4TLc7D8U3[backslash]Pf8S5SsWV2be8ygy6AhyY1z03p3t+99zCP4[backslash]0htzuuN'
        'PyBvefMV9YwPXlvSt+Jg5H1EtIkB0XVoSCQZ+3wldt8wWrTL8tsh2weewOv0[backslash]epIJBeZgBW08jESvslJwtzFiC'
        'EYObYyTL0ampEmOsAxzot88KT1[backslash]sx5WT7yx0FjlzcysLc+z5dnFC7VIDmZ9Va5ecTnnTdvtCfiMNu35UjcuIYd'
        'd7GZlwDyu3kdxqYHP6vEFbIFjjNm25RhYGHFIDmsitphweCpg2l7fS5bAHff4kQeI7NbCZHNi5jnuRW2w0R5V9Sm4ptzr8K7'
        'nCFXS6THelLT8pKXxb2KO01rHU7GW0gfyNktvFIvc02kP+SsnnxSIXGR0hb1fyBbHIBUYnyNuU3DmGs51ndIH7mXK7xVK40M'
        'iGvFnJPep0jnKtc7RRabmx1Mwz8iBvUHJBLHK+UQh5rZKL8JVnc9orzVHesdVYrB+pj3rFkptj9EbuB0q+KBY51yiBvErJfe'
        'VWo1qyV13T[backslash]TBshRo2oNa+l4f3[backslash]V310cAYyufnGIOQ+7aSh9TpJBZbJ[backslash]FNpQ2VW830'
        'uwDfjrB3r6uRw2JYnoXGcFR5Rbkj9Srn7jO2YM100xiFsS+psWPqdCBF1oG8oLQy[backslash]XZW4fNXSs86BeUY+qwaOk'
        '5uNeGhkXtklcsZtFUnpYRTn1ZDr6jTzhRaO[backslash]N3pV2p3+AjLTlJ040JGPeEGjcxhjktNibBfVS5k+VWiz+a08ih'
        'J6WsXz+8RDyshlfI4Rk83GP3T3F5rUFL1KBKOahxrZlxud1qCk0UX6wGTolhx4uMKriLlDtVv1DCXzwO6SUJIe5Vg9wxFMg1'
        'PCiwULk+uXXen9[backslash]P5A0Nz6FigfJm1M3Lsc72POUF6rRI8ixtrtJC+iKJLEOH3W1iEV7fV9yhBs7S12uV6ZpSFb'
        'T58XMsvF77itvU0JoYbiF5xcZs7NAtSr6mTsehDv8mpV0XS81exhzI1yv5hljknsaNkK9V8s36mvbbK12hQFLKuvXrS8TVat'
        'BfYqlQZNyKCtVKvl2voF1aM9WgO2OpUGDMRYWAkufFsNDzjflwpyv3r[backslash]rCcMinz2nqel2IkR418h481lpLzDVb'
        'Pgxi4SzoL6apAffFcgyFxiKkVyn5gVjkPGMxZFPJf4tFzjeWQK5Q8sNRl4ZvJt[backslash]0H8FImxr5WCxlcozHIV+l5C'
        'djkXONpZDHK[backslash]mpf[backslash]l1U2N93Vyuhj4jt9IiI5x2j8tdY8tNeBanrtxvVoQc8gdkuxFlolwNf17[ba'
        'ckslash]znO6qm1BszpyivHdPVYNfFEOzKy1UHzukEfecCP3v9Fq1Mt1uv57Wdf[backslash]SKW9EsMKLjBehTtMua[bac'
        'kslash]XqWRPq+RQpb1ZJ63A0oYo7e2orzfMs7VY3pEfxeOHY9vIz[backslash]Tuo5A5IDx2BI6hzDoE[backslash]GxK'
        'tHlDngp5I8BTHmbcCD80oZVg8[backslash]qCZiBNXoy+gBmw4cWKN5ipNuX3XNDukj[backslash]XM2y+6aa3VkejyBDu'
        'sLkqG4YHaV2NIl0hhLl9jvAruTQ5v95K20yX3ebxuHwpYdoKHFV2WSbc9piBgH2KfLTzZkZ1OJ2ZauKC9qAZfiBrZnOblbUe'
        'KJxygmxIa68N5d2a6Qq4KuRPTFdlc32EPYgZ8pvT3TWtbf6Q16ZMj8cu91h+Zqnp4bFqsXgrDXwWHpzMn5B8QKDExIQE[bac'
        'kslash]EmIC2+RwGODEN3[backslash]5LR1HTXAFukuQypeyEgLf+HdQnJyMhnL8GT+LrAcWAGsBN4DVgHvA6uxVPrI9RRj'
        'TVxsieE3GjYbJaE0qdJrwi2Blw+JVn[backslash]Ch9gYNb1HWbCiJuEjtVE+YLwc[backslash]zH2Yy2wDlgPbAA+AT4F'
        'NgKbcJjd5a5id[backslash]FWJeEf4ewtEh3NnJxcR36+PbvQUZyfXeAs7JldYXc6su2FFfmVeaazspe80YfHG1vDRDseby'
        'iy9HcjeBnl88fJD+K8do8ZeWuTPM3rm+UNfxIfUIdvJCbi3UqydYx0Hsm7VKZ11IaajW3Y+AL4EtgO7AB2Al8Bu4CvcXRtra'
        'M7N5ffhlvxeNnRLGo[backslash]B8sVW4aXIfqpSLX2RjS39qShytqNMnuAvcA+YD9wAPgOOAgcgp0VvSc[backslash]RM'
        '5qGyu1keo[backslash]DOUI8CNwFDgGHAd+Ak4AJ5FqRKeeiqS2t1IzVP8vUH4FfgNOA2eAs8Dv4QmWx23Iq4bE2bNRqfVF'
        'OLWjlZqp+vFfUox4IAFIBPC1YRhAMpACpCL1eHRqWiS1k5WapvrToWQAmUBDoBGQBTQGmgBNkXogOrV5JLWLlVpP9beA0hJo'
        'BbQG2gBtgXZAe6ADUndGp3aMpGZbqemq[backslash]3woFwCdgAuBzkAXoCvQDchG6ubo1B6R1DwrNVX150DJBfKAfKAAKA'
        'R6AkVAMVI[backslash]jE7tHUkttFIbqP4+UC4CSoCLgb5AP6A[backslash]MAAoReqy6NRBkdTeVmqy6h8MZQhwCTAUuB'
        'QYBgwHRgAjkfpydOroSGqJlZqi+sdAGQuUAeXAZcA44HLgCmA8UpdGp06IpPZTq0rdUbJxRxlk1WqjRl+FoInAJMAGTAbsQA'
        'XgACpR68HoWs5IreFWarzqnwKlCnABU4FpgBvwAF7Ah9QF0akzwqnxePmhvkhGmEHTP8Luld[backslash]M[backslash]q'
        '4OdyDy5THAV911qPoyLpffz3[backslash]8RSLKo+fiCTkX6XjhcL0I77mQl0p9ipMHkCA3k+T9IFmejlS5fNLkcs+QF31D'
        'eZPKosbUhJpSM2pOLagltaLWciLbUjtqTx3kHbsjnU8XUCe6kDpTF+pK3eScd6celEO5lEf5VECF1JOKqJh6UW[backslash'
        ']qQxdRCV1Mfakf9acBVEoDaRANpiF0CQ2lS2kYDacRNJJG0WgaQ2OpjMrpMhpHl9MVNJ6upAl0FU2kSWSjyWSnCnJQJZnkpC'
        'lURS6aStPITR7yko+m0wzyU4CCFKKZNIuqqYZm09V0DV1L19Ecup5uoBvpJrqZbqG[backslash]0K10G91Od9CdNJfuonk0'
        'nxbQX2kh3U330L10Hy2i++kBWkwP0t9oCT1ED9Mj9Cg9Ro[backslash]LWX2SltLf6Sl6mp6hZ+k5ep5eoBfpJXqZ[backs'
        'lash]oteoVfpNXqd3qA36S16m96hZfQuLacVtJLeo1X0Pq2mD2gNfUgf0ce0ltbRetpAn9CntJE20Wb6B22hz2grfU7b6Av6'
        'krbTDtpJX9Eu+pq+oW9pN+2hvbSP9tMB+o4O0iH6nn6gw3SEfqSjdIyO0090gk7Sz3SKfqFf6Tc6TWfoLP0eft6pJ+qLOBEv'
        'EkSifG4yRLJIEamigUgT6SJDZIqGopHIEo1FE9FUNBPNRQvRUrQSrUUb0Va0E+1FB3Ge6CjOFxeITuJC0Vl0EV1FN5Etuose'
        'IkfkijyRLwpEoegpikSx6CV6iz7iIlEiLhZ9RT[backslash]RXwwQpWKgGCQGiyHiEjFUXCqGieFihBgpRonRYowYK+SvG3'
        'GZGCcuF1eI8eJKMUFcJSaKScImJgu7qBAOUSlM4RRTRJVwialimnALj[backslash]AKn5guZgi[backslash]CIigCImZYp'
        'aoFjVitrhaXCOuFdeJOUJMsq7fJHUF+nFxBoAgEAJmArOAaqAGmI3r95bo6[backslash]eayF3BjL7qws80VVatRDX6WgRd'
        'B8wBcEUaNwA3AjcBNwO3oNbs6Fq3Rmp5rESh+m+DcjtwB3AnMBe4C5gHzAcWINUXnbowkjrbSm2l+u+Gcg9wL3AfsAi4H3gA'
        'WAw8iNTK6NQlkdQ5Vmpr1f8QlIeBR4BHgceAx4EngCeBpUgdH536VCT1Riu1hep[backslash]GsozwLPAc8DzwAvAi8BLwM'
        'tIHRmd+kok9VYrtaXqfxXKa8DrwBvAm8BbwNvAO8AypJZGpy6PpM61Upup[backslash]hVQVgLvAauA94HVwAfAGuBDpBZH'
        'p34cSZ1vpdZX[backslash]WuhrAPWAxuAT4BPgY3AJmAzUrOjU7dEUhdGr+I5WMWLrFrN1ejPELQV+BzYBnwBfAlsB3YAO1'
        'GrQ3StXZFai63UJqr[backslash]ayjfAN8Cu4E9wF5gH7AfOIDUptGpByOpS6zUpqr[backslash]EJTvgR+Aw8AR4EfgKH'
        'AMOI7U1OjUE5HUR6LnxYZ5edyqlaVGn0TQz8Ap4BfgV+A34DRwBjiLWiK6VuQJViy1UhurfiH7jXpAfQD[backslash]X4cR'
        'DyQAiUASgP8NRJw8E5WaEkl91UqNU[backslash]2pUBoAaUA6kAFkAg2BRkAWUg9FpzaJpL7zh88ay6w6mDtKf7f2VuZya6'
        'tBXOaKc830lZrxnmasYuN9bq5meY01VFb+WMtZq+Wss7aMLf8dmev[backslash]zYfpG7TUT9QWNjM[backslash]tbYSms'
        'kZiasaVloa[backslash]n3MyNyoRrSQI1KGje5vGzV60Nj+5YOiB25SA1upgWXl[backslash]UcOHDA+amD6Vj7gbdqufa'
        'Ed8Jc8W9t5tnZoxk7N+IqNXdz8muVvufJuLWePlrOX5X0s79eMA5rxHRsHuXmI5R+48mEt54iW8yPLR1k+phnHNeMnNk5w8y'
        'TLp7jyL1rOr1rObyyfZvmMZpzVjN[backslash]ZQI[backslash]VFOea6fgxblWOE7Vz4mtvZSawnMhykmYYmpHMRgo3U1'
        'lO48rpWk6GlpPJckOWG2lGlmY0ZqMJN5uy3Jwrt9ByWmo5rVhuzXIbzWirGe3YaM[backslash]NDix35MrnazkXaDmdWL6Q'
        '5c6a0UUzurLRjZvZLPfgyjlaTq6Wk8dyPssFmlGoGT3ZKOJmMcu9uXIfLeciLaeE5YtZ7qsZ[backslash]TSjPxsDuFnK8i'
        'CuPFjLGaLlXMLyUJYv1YxhmjGcjRHcHMnyaK48RssZq+WUsVzO8mWaMU4zLmfjCm6OZ3kCV75Ky5mo5Uxi2cbyZM2wa0YFGw'
        '5uVrLs5MpTtJwqLcfF8lSWp2mGWzM8bHi56WN5Blf2azkBLSfIcojlmZoxSzOq2ajh5myWr+HK12o512k5c1i+nuUbNONGzb'
        'iJjZu5eQvLt3Ll27Sc27WcO1i+k+W5mnGXZsxjYz43F7C8kCvfreXco+Xcy[backslash]J9LC[backslash]SjPs14wE2Fn'
        'PzQZaXcOWHtJyHtZxHWH6U5cc043HNeIKNJ7m5lOWnuPLTWs4zWs6zLD[backslash]H8vOa8YJmvMjGS9x8meVXuPKrWs5r'
        'Ws7rLL[backslash]B8pua8ZZmvM3GO9xcxvJyrrxCy1mp5bzH8iqW39eM1ZrxARtruPkhyx9z5bVazjotZz3LG1j+RDM+1Y'
        'yNbGzi5maWt3Dlz7ScrVrO5yxvY[backslash]kLzfhSM7azsYObO1nexZW[backslash]1nK+0XK+ZXk3y3s0Y69m7GNjPz'
        'cPsHyQKx[backslash]Scr7Xcn5g+TDLRzTjR804ysYxbh5n+QRXPqnl[backslash]KzlnGL5F5Z[backslash]1YzfNOM0'
        'G2e4eZZl[backslash]Ay2KuOXL+fUq72VWb[backslash]eOTnuXDM9XjMSNCORjSRuGiyncOVULaeBlpPGcjrLGZqRqRkN'
        '2WjEzSyWm5yrnPJPIzs7kcU0AAA='),
    'LED': (
        '1|H4sIAAAAAAAEAOVWbWvTUBRO9tKl63S+zE0csohOREpp974PVbQTJ042tzGEMbLb5HSNTW7qzc267Gf4t3SIiIgIIjIUQV'
        'RQ[backslash]CDKvOe2d84pgqCgLKVNcp7n5D7n3HNOqumapm2JA894tODFW30uDjn4mULgeWBzN6ChuKZ2xBhQvuNywpUo'
        'YfHywOJiw2uOM5eupE0[backslash]tAPmucW0uQAsFLT8UCaLn7RZiDweMchTiDgjXtqciYqea1+FeD6oAM0XR0fJsD08kh'
        'sfHILs2PhSWj18unhTKPqzD19qFUG3+5Yd+FXCgB3wrQrEs1AmYbkQRJR3+VYIzCWeu04w3guMkbhvl3E7K3Y8Bavg9ezGSZ'
        'XYLo+btWZNu[backslash]2zFF8GKhxsdb50KxLOPC40ZC3n[backslash]mqKl4wXvxIlHBeIF8EMcdl[backslash]vdmL'
        'S4aRbGrUe1LshXZUfAtY[backslash]Gj9F3cGlbahUg3bs1t8mzf3xF61fBazaW+EirXXLMZO6yqGozclcCglLYhhziYeGC'
        'lxe76jTfxeb8iYYKQmg[backslash]z91bODpeHSaCmXc4azZJDon0SaMdUJQzw[backslash]ZXGXeyCyHbBkEovuo4JRxD'
        '6r7IY8YHGd0IGE94qwH0V74FhDdpmwRCd21Y2xEVd[backslash]pxgH5QphOahdI6wCzNB1[backslash]Y0CDwuw04qqDu'
        'FwhXJgIhmG4eASrxXniNTgS2eUELEkdoT+ShGwnROWAzaJjdTG3am8[backslash]lJBx7CRxJyXXr3otamg4+KuB5VXWWBD'
        'GFq1MlARquMANXRNf66Ifbg+EmnA1yzOIkD4mYJPyG3D+ObjKmB0TxV0Uq7OYY1bucQpcXOonxO+kiF+1QprrN88Zw7oTxT7'
        'tCDsx[backslash]2[backslash]COvgNYpAhvNYUc4gZVtKiXih1PJI4WflgoRzYlcavg8VlpY6S8SBaTq[backslash]hm'
        '4PFJSRUBVIZTLwHAzhvoKyOH8sd4UGDCbrZTARMfl+MwwsYf2eog5I[backslash]aF46QGbIY4jStVIffgyn9c3FGVoh4ZZ'
        'qeGOgkbq3iKNc1Fxu1JasRy7vq[backslash][backslash]jKzDFqw0Snyo9097hQY1KpHWUCQZcHAm29qwf9pN09yqj[ba'
        'ckslash]8mTcf6Tf5g7VZWbUv7Zu3dtoo[backslash]Tcqa+go9yodbRQkAAA=='),
    'MMIO': (
        '1|H4sIAAAAAAAC[backslash]+UX224bRXR3vetrm0a0CSAaapAqHrCs2Eka+hBBcS9YaRoLh75E0XS8O4232YuZnY3kRgg+'
        'AfHOM3wAIN6ReAAkLhISdwnU9oWqrQQSFUKCcya7re3YSdSkqFEmynj2zLmfM+fMKKqiKP[backslash]CwF8cOi5uq[back'
        'slash]V2IJhbrPiOw0xh+14Aa88MOWee6FietuUu5e1L5cXFiOpcaFuFvBuYPnfsRiF[backslash]kfEAkGYmi+P4V8hXQk'
        'eEnM14LBScOoV8LWw4tjnL2gv+CvNmGtPTdMqcOlE6OTHJxl84uVRYXGgyYQfFOaC055hgvDr[backslash]vOkEc3PV+UJ+'
        'ffO+oHKxNF6cKJamthblhY6ztGSA0VmXmL7bopzxYZessParrEmDZsUPPXHEJQHjNnXsqxTtPcU5bR[backslash]rAd7zit'
        'k+z1aZ83jvPm1R0xbthJJQlLf7ufgc84DAjH[backslash]PvB4CsWhXIrUulR6ii5fSNzZTCQgvUidkNWrzPR3qxaV0OqNF'
        '2Z6BSCgH4P8Ypj5CH724oJ4p1FPBo4k6Jq7ti0gdwFP54g7Z6P9Acdsf[backslash]pKpAXXLWEVzEnquw5KnNhGNbtb[ba'
        'ckslash]Bjd1EuQw1wjVSEMjpkYsjTCNXNbIskaaGrE1ckUjK8r9kU6ltGh8SibemLpan33vs8qPh9e+uptJYe7eBQEYC[ba'
        'ckslash]UvmFDajZe8O2vvv3b+42[backslash]f[backslash]fKLte[backslash]0TBbPIsqtruuxCz43UPRmtj8J+wZZ'
        'BoOzxLL5em4kiN0ySMvnYpiEVoswz2r5ticA[backslash]Fg3AHGy5LLPXQq7Xi5e+qEwiGi32DBxbLAFMo1Q4L3KEDvkxG'
        'xSngY+tOEwa4iATq7tUeFz4HKo8xM4jRAzDITvdmON9oEC8tE+YChSAbOAZmzwJpAe7LItg90wMrBBQ4twKkB7iwpKGrYIsg'
        'Q4teQyhUygHh4ktk9WYYlWBQm4SqiKrutyVmHg3ULXE51p9ky[backslash]2GBgTsfBwOxMp58ehHdWOhyRtoFydBDKAoQK'
        'EVT12YEo91yGiNtEG4lMvcBEsVo741k1dG1OmvREtFedL9bAy0GxDv58GdyJ26O9mzXp4dzOL2d1wW1vebcrW8R8vnEFNNrl'
        'BigD82dUNXqLi[backslash]EHALfOImydKVkXCVHSRlS0kkN4ByiVp6VypQ8+gS8EDT8HVeEOMN4ipzZyRYh6GyiRGkUYt2'
        'Cxad71V01NHsayLKhYVo2bwGMbGTeA0+9AfTNSJzkqrz2jXVNWzm8dh6xEja8D7sDk7JaRi0w2rgHNgJzdSCFVGENSLPmyO['
        'backslash]w2oCmovwIQQ4zNTcbmozhIH8JC[backslash]SVytfT7z9EH4qo[backslash]weJWZLWaPI7ObM5WKqr6Q4c7'
        '1O87Pnq8sj4V4lXsHvUbQL8ei[backslash]wabxcx[backslash]TgKGduvR[backslash]T[backslash]fT+9s7N7+sNw'
        '8YO9oPZesOULqhSf4zJMmPgjcB6MwsCeJ8+vS05ZFmdBkHQJlghdOdKFHO3K[backslash]ohTZiI+5yhhTBbrWO5k58sNoY'
        '9iRsi3Wzl+uykP+nbbgwUBI9c3uEZXIqRdcpa6ttMG0IXQbYBkWNVNv8WqFlSUV2QFsZiiQ5vI5DsY1n1zhUGTiRitc8GkGc'
        'rI3Hkz8rnxOZTnLQk3NimZsCeU7pGa3IexPIRewNY31OMNJfcf0sq5yj8UAAA='),
}


# SDR-VST3 v5.7 item templates (its own saved defaults).
JSON_TEMPLATES = json.loads(
    '{"TEXT_OVERLAY":[{"k":"_text_2","t":"string","v":""},{"k":"textoverlay_tx_4char","t":"string","v'
    '":""},{"k":"textoverlay_rx_4char","t":"string","v":""},{"k":"_titleColor","t":"color","v":{"R":3'
    '2,"G":32,"B":32,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor'
    '":false,"Name":"ff202020"}},{"k":"_font_style_1","t":"fontstyle","v":0},{"k":"_colour","t":"colo'
    'r","v":{"R":255,"G":255,"B":255,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":fals'
    'e,"IsSystemColor":false,"Name":"ffffffff"}},{"k":"textoverlay_tx_ledlogic","t":"bool","v":false}'
    ',{"k":"_markerColour","t":"color","v":{"R":255,"G":255,"B":255,"A":255,"IsKnownColor":false,"IsE'
    'mpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"ffffffff"}},{"k":"_decay","t":"fl'
    'oat","v":0},{"k":"_showSubMarker","t":"bool","v":true},{"k":"_font_family_2","t":"string","v":"T'
    'rebuchet MS"},{"k":"_spacerPadding","t":"float","v":0.1},{"k":"_text_1","t":"string","v":""},{"k'
    '":"textoverlay_rx_ledlogic","t":"bool","v":false},{"k":"_fadeOnTx","t":"bool","v":false},{"k":"_'
    'font_size_2","t":"float","v":18},{"k":"_eyeScale","t":"float","v":0},{"k":"_subMarkerColour","t"'
    ':"color","v":{"R":64,"G":64,"B":64,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":f'
    'alse,"IsSystemColor":false,"Name":"ff404040"}},{"k":"_font_style_2","t":"fontstyle","v":0},{"k":'
    '"_attack","t":"float","v":0},{"k":"_eyeBezelScale","t":"float","v":0},{"k":"_font_family_1","t":'
    '"string","v":"Trebuchet MS"},{"k":"_historyColor","t":"color","v":{"R":32,"G":32,"B":32,"A":255,'
    '"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"ff202020'
    '"}},{"k":"_font_size_1","t":"float","v":18},{"k":"_peakValueColour","t":"color","v":{"R":64,"G":'
    '64,"B":64,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":fals'
    'e,"Name":"ff404040"}},{"k":"_showMarker","t":"bool","v":false},{"k":"_fadeOnRx","t":"bool","v":f'
    'alse},{"k":"_showType","t":"bool","v":false}],"OTHER_BUTTONS":[{"k":"buttonbox_other_buttons_mac'
    'ro_settings_4","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral'
    ', PublicKeyToken=null","v":{"Number":4,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":'
    'false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"Cl'
    'oseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation"'
    ':[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,nu'
    'll,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonSta'
    'teType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"Cat'
    'MacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"butt'
    'onbox_other_buttons_macro_settings_23","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version='
    '5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":23,"OnText":null,"OffText":null,"No'
    'tes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[fal'
    'se,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null'
    ',null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MM'
    'ICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,n'
    'ull,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"Butto'
    'nStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"Ca'
    'tMacro":null}},{"k":"buttonbox_other_buttons_bitfield_6","t":"int","v":0},{"k":"buttonbox_font_s'
    'cale","t":"float","v":1},{"k":"buttonbox_other_buttons_macro_settings_3","t":"aq:Thetis.OtherBut'
    'tonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":3'
    ',"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,f'
    'alse,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,null],'
    '"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMM'
    'IO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,nu'
    'll,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null'
    ',"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnV'
    'isible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settings_17"'
    ',"t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyTok'
    'en=null","v":{"Number":17,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"Closes'
    'Container":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseContainerI'
    'D":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false'
    ',false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMI'
    'OMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"Le'
    'dIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[f'
    'alse],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_font_sh'
    'ift_x","t":"float","v":0},{"k":"buttonbox_other_buttons_bitfield_8","t":"int","v":0},{"k":"butto'
    'nbox_other_buttons_bitfield_4","t":"int","v":0},{"k":"buttonbox_other_buttons_macro_settings_19"'
    ',"t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyTok'
    'en=null","v":{"Number":19,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"Closes'
    'Container":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseContainerI'
    'D":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false'
    ',false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMI'
    'OMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"Le'
    'dIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[f'
    'alse],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_b'
    'uttons_bitfield_10","t":"int","v":0},{"k":"_font_style_1","t":"fontstyle","v":0},{"k":"buttonbox'
    '_other_buttons_macro_settings_1","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.'
    '0, Culture=neutral, PublicKeyToken=null","v":{"Number":1,"OnText":null,"OffText":null,"Notes":nu'
    'll,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,fals'
    'e,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],'
    '"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourC'
    'har":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,nul'
    'l,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateC'
    'atReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro"'
    ':null}},{"k":"buttonbox_indicator_type","t":"aq:Thetis.MeterManager+clsButtonBox+IndicatorType, '
    'Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":0},{"k":"buttonbox_other_butt'
    'ons_macro_settings_25","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture'
    '=neutral, PublicKeyToken=null","v":{"Number":25,"OnText":null,"OffText":null,"Notes":null,"Close'
    'sParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,f'
    'alse],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUses'
    'Location":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[nul'
    'l,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"'
    'ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":'
    'null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{'
    '"k":"buttonbox_other_buttons_macro_settings_11","t":"aq:Thetis.OtherButtonMacroSettings, Thetis,'
    ' Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":11,"OnText":null,"OffText"'
    ':null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContai'
    'ner":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,'
    'null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,f'
    'alse],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF'
    '":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":nu'
    'll,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":'
    'false,"CatMacro":null}},{"k":"buttonbox_border","t":"float","v":0.049999997},{"k":"buttonbox_oth'
    'er_buttons_macro_settings_28","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, '
    'Culture=neutral, PublicKeyToken=null","v":{"Number":28,"OnText":null,"OffText":null,"Notes":null'
    ',"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,'
    'false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"O'
    'penUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourCha'
    'r":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,'
    'null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCat'
    'Reply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":n'
    'ull}},{"k":"buttonbox_other_buttons_macro_settings_7","t":"aq:Thetis.OtherButtonMacroSettings, T'
    'hetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":7,"OnText":null,"Off'
    'Text":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensC'
    'ontainer":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":['
    'null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,fa'
    'lse,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessa'
    'geOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleI'
    'D":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnSt'
    'ate":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settings_20","t":"aq:Thetis.Oth'
    'erButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Numb'
    'er":20,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,f'
    'alse,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,'
    'null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"Send'
    'sViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,n'
    'ull,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar'
    '":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateComm'
    'andOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settin'
    'gs_13","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, Public'
    'KeyToken=null","v":{"Number":13,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"'
    'ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseCont'
    'ainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false'
    ',false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null'
    '],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType"'
    ':0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSe'
    'nd":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_u'
    'se_off_colour","t":"bool","v":false},{"k":"buttonbox_other_buttons_macro_settings_5","t":"aq:The'
    'tis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v"'
    ':{"Number":5,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[f'
    'alse,false,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null'
    ',null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false]'
    ',"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":['
    'null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFo'
    'urChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunSta'
    'teCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_on_colour","t":"colo'
    'r","v":{"R":100,"G":149,"B":237,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,'
    '"IsSystemColor":false,"Name":"CornflowerBlue"}},{"k":"buttonbox_other_buttons_bitfield_2","t":"i'
    'nt","v":0},{"k":"buttonbox_use_icons","t":"bool","v":true},{"k":"buttonbox_other_buttons_macro_s'
    'ettings_22","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, P'
    'ublicKeyToken=null","v":{"Number":22,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":fa'
    'lse,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"Clos'
    'eContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":['
    'false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null'
    ',null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonState'
    'Type":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMa'
    'croSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"button'
    'box_other_buttons_bitfield_0","t":"int","v":0},{"k":"buttonbox_off_colour","t":"color","v":{"R":'
    '0,"G":0,"B":0,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":fa'
    'lse,"Name":"Black"}},{"k":"buttonbox_other_buttons_bitfield_9","t":"int","v":0},{"k":"_fadeOnTx"'
    ',"t":"bool","v":false},{"k":"buttonbox_use_indicator","t":"bool","v":false},{"k":"buttonbox_othe'
    'r_buttons_macro_settings_15","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, C'
    'ulture=neutral, PublicKeyToken=null","v":{"Number":15,"OnText":null,"OffText":null,"Notes":null,'
    '"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,f'
    'alse,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"Op'
    'enUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar'
    '":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,n'
    'ull],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatR'
    'eply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":nu'
    'll}},{"k":"buttonbox_other_buttons_macro_settings_18","t":"aq:Thetis.OtherButtonMacroSettings, T'
    'hetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":18,"OnText":null,"Of'
    'fText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"Opens'
    'Container":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":'
    '[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,f'
    'alse,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMess'
    'ageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisible'
    'ID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnS'
    'tate":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settings_9","t":"aq:Thetis.Oth'
    'erButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Numb'
    'er":9,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,fa'
    'lse,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,n'
    'ull],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"Sends'
    'ViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,nu'
    'll,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar"'
    ':null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateComma'
    'ndOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_fix_text_size","t":"bool","'
    'v":false},{"k":"buttonbox_other_buttons_macro_settings_24","t":"aq:Thetis.OtherButtonMacroSettin'
    'gs, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":24,"OnText":nul'
    'l,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"'
    'OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContaine'
    'rID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,fa'
    'lse,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMI'
    'OMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVi'
    'sibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false'
    ',"OnState":false,"CatMacro":null}},{"k":"buttonbox_button_map","t":"aq:System.Int16[], System.Pr'
    'ivate.CoreLib, Version=10.0.0.0, Culture=neutral, PublicKeyToken=7cec85d7bea7798e","v":[0,1,2,3,'
    '4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,32,33,34,35,36,37,'
    '38,39,40,41,42,43,44,45,46,47,48,49,50,51,52,53,54,55,56,57,58,59,60,61,62,63,64,65,66,67,68,69,'
    '70,71,72,73,74,75,76,77,78,79,80,81,82,83,84,85,86,87,88,89,90,91,92,93,94,95,96,97,98,99,100,10'
    '1,102,103,104,105,106,107,108,109,110,111,112,113,114,115,116,117,118,119,120,121,122,123,124,12'
    '5,126,127,128,129,130,131,132,133,134,135,136,137,138,139,140,141,142,143,144,145,146,147,148,14'
    '9,150,151,152,153,154,155,156,157,158,159,160,161,162,163,164,165,166,167,168,169,170,171,172,17'
    '3,174,175,176,177,178,179,180,181,182,183,184,185,186,187,188,189,190,191,192,193,194,195,196,19'
    '7,198,199,200,201,202,203,204,205,206,207,208,209,210,211,212,213,214,215,216,217,218,219,220,22'
    '1,222,223,224,225,226,227,228,229,230,231,232,233,234,235,236,237,238,239,240,241,242,243,244,24'
    '5,246,247,248,249,250,251,252,253,254,255,256,257,258,259,260,261,262,263,264,265,266,267,268,26'
    '9,270,271,272,273,274,275,276,277,278,279,280,281,282,283,284,285,286,287,288,289,290,291,292,29'
    '3,294,295,296,297,298,299,300,301,302,303,304,305,306,307,308,309,310,311,312,313,314,315,316,31'
    '7,318,319,320,321,322,323,324,325,326,327,328,329,330,331,332,333,334,335,336,337,338,339,340,34'
    '1,342,343,344,345,346,347,348,349,350,351,352,353,354,355,356,357,358,359,360,361,362,363,364,36'
    '5,366,367,368,369,370,371,372,373,374,375,376,377,378,379,380,381,382,383]},{"k":"buttonbox_othe'
    'r_buttons_macro_settings_26","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, C'
    'ulture=neutral, PublicKeyToken=null","v":{"Number":26,"OnText":null,"OffText":null,"Notes":null,'
    '"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,f'
    'alse,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"Op'
    'enUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar'
    '":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,n'
    'ull],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatR'
    'eply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":nu'
    'll}},{"k":"buttonbox_other_buttons_macro_settings_12","t":"aq:Thetis.OtherButtonMacroSettings, T'
    'hetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":12,"OnText":null,"Of'
    'fText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"Opens'
    'Container":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":'
    '[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,f'
    'alse,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMess'
    'ageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisible'
    'ID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnS'
    'tate":false,"CatMacro":null}},{"k":"buttonbox_margin","t":"float","v":0.049999997},{"k":"buttonb'
    'ox_font_colour","t":"color","v":{"R":255,"G":255,"B":255,"A":255,"IsKnownColor":true,"IsEmpty":f'
    'alse,"IsNamedColor":true,"IsSystemColor":false,"Name":"White"}},{"k":"buttonbox_other_buttons_bi'
    'tfield_3","t":"int","v":0},{"k":"buttonbox_fill_colour","t":"color","v":{"R":0,"G":0,"B":0,"A":2'
    '55,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Black"}'
    '},{"k":"_font_family_1","t":"string","v":"Trebuchet MS"},{"k":"buttonbox_other_buttons_macro_set'
    'tings_8","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, Publ'
    'icKeyToken=null","v":{"Number":8,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,'
    '"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseCon'
    'tainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[fals'
    'e,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,nul'
    'l],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType'
    '":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroS'
    'end":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_'
    'height_ratio","t":"float","v":0.5},{"k":"buttonbox_other_buttons_bitfield_1","t":"int","v":0},{"'
    'k":"buttonbox_indicator_border","t":"float","v":0.049999997},{"k":"buttonbox_radius","t":"float"'
    ',"v":0.099999994},{"k":"buttonbox_other_buttons_macro_settings_14","t":"aq:Thetis.OtherButtonMac'
    'roSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":14,"OnT'
    'ext":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,'
    'false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,null],"Open'
    'ContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":['
    'false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,nu'
    'll],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"Con'
    'tainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisibl'
    'e":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settings_2","t":"'
    'aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=nul'
    'l","v":{"Number":2,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContain'
    'er":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[nul'
    'l,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,'
    'false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessag'
    'eON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndici'
    'atorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"'
    'RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"_font_size_1","t":"float'
    '","v":18},{"k":"buttonbox_other_buttons_macro_settings_30","t":"aq:Thetis.OtherButtonMacroSettin'
    'gs, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":30,"OnText":nul'
    'l,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"'
    'OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContaine'
    'rID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,fa'
    'lse,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMI'
    'OMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVi'
    'sibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false'
    ',"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_bitfield_7","t":"int","v":0},{"'
    'k":"buttonbox_other_buttons_bitfield_11","t":"int","v":0},{"k":"buttonbox_hover_colour","t":"col'
    'or","v":{"R":211,"G":211,"B":211,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true'
    ',"IsSystemColor":false,"Name":"LightGray"}},{"k":"buttonbox_other_buttons_macro_settings_27","t"'
    ':"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=n'
    'ull","v":{"Number":27,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesCont'
    'ainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":['
    'null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,fal'
    'se,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMes'
    'sageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedInd'
    'iciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false'
    '],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_butto'
    'ns_macro_settings_0","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=n'
    'eutral, PublicKeyToken=null","v":{"Number":0,"OnText":null,"OffText":null,"Notes":null,"ClosesPa'
    'rent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false,fals'
    'e],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLoc'
    'ation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,n'
    'ull,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"But'
    'tonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":nul'
    'l,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k"'
    ':"buttonbox_click_colour","t":"color","v":{"R":255,"G":165,"B":0,"A":255,"IsKnownColor":true,"Is'
    'Empty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Orange"}},{"k":"buttonbox_font_sh'
    'ift_y","t":"float","v":0},{"k":"buttonbox_other_buttons_macro_settings_16","t":"aq:Thetis.OtherB'
    'uttonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number"'
    ':16,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,fals'
    'e,false,false],"OpensContainer":[false,false,false,false],"CloseContainerID":[null,null,null,nul'
    'l],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsVi'
    'aMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null'
    ',null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":n'
    'ull,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommand'
    'OnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_other_buttons_macro_settings_'
    '29","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Culture=neutral, PublicKey'
    'Token=null","v":{"Number":29,"OnText":null,"OffText":null,"Notes":null,"ClosesParent":false,"Clo'
    'sesContainer":[false,false,false,false],"OpensContainer":[false,false,false,false],"CloseContain'
    'erID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUsesLocation":[false,fa'
    'lse,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[null,null,null,null],"'
    'MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null],"ButtonStateType":0,'
    '"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply":null,"CatMacroSend"'
    ':[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}},{"k":"buttonbox_othe'
    'r_buttons_bitfield_5","t":"int","v":0},{"k":"_fadeOnRx","t":"bool","v":false},{"k":"buttonbox_ot'
    'her_buttons_macro_settings_6","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, '
    'Culture=neutral, PublicKeyToken=null","v":{"Number":6,"OnText":null,"OffText":null,"Notes":null,'
    '"ClosesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,f'
    'alse,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"Op'
    'enUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar'
    '":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,n'
    'ull],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatR'
    'eply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":nu'
    'll}},{"k":"buttonbox_other_buttons_macro_settings_21","t":"aq:Thetis.OtherButtonMacroSettings, T'
    'hetis, Version=5.7.0.0, Culture=neutral, PublicKeyToken=null","v":{"Number":21,"OnText":null,"Of'
    'fText":null,"Notes":null,"ClosesParent":false,"ClosesContainer":[false,false,false,false],"Opens'
    'Container":[false,false,false,false],"CloseContainerID":[null,null,null,null],"OpenContainerID":'
    '[null,null,null,null],"OpenUsesLocation":[false,false,false,false],"SendsViaMMIO":[false,false,f'
    'alse,false],"MMICFourChar":[null,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMess'
    'ageOFF":[null,null,null,null],"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisible'
    'ID":null,"ButtonStateCatReply":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnS'
    'tate":false,"CatMacro":null}},{"k":"buttonbox_columns","t":"int","v":3},{"k":"buttonbox_other_bu'
    'ttons_macro_settings_10","t":"aq:Thetis.OtherButtonMacroSettings, Thetis, Version=5.7.0.0, Cultu'
    're=neutral, PublicKeyToken=null","v":{"Number":10,"OnText":null,"OffText":null,"Notes":null,"Clo'
    'sesParent":false,"ClosesContainer":[false,false,false,false],"OpensContainer":[false,false,false'
    ',false],"CloseContainerID":[null,null,null,null],"OpenContainerID":[null,null,null,null],"OpenUs'
    'esLocation":[false,false,false,false],"SendsViaMMIO":[false,false,false,false],"MMICFourChar":[n'
    'ull,null,null,null],"MMIOMessageON":[null,null,null,null],"MMIOMessageOFF":[null,null,null,null]'
    ',"ButtonStateType":0,"LedIndiciatorFourChar":null,"ContainerVisibleID":null,"ButtonStateCatReply'
    '":null,"CatMacroSend":[false],"RunStateCommandOnVisible":false,"OnState":false,"CatMacro":null}}'
    ',{"k":"buttonbox_border_colour","t":"color","v":{"R":255,"G":255,"B":255,"A":255,"IsKnownColor":'
    'true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"White"}}],"LED":[{"k":"_p'
    'eakHold","t":"bool","v":true},{"k":"_ignoreHistoryDuration","t":"int","v":0},{"k":"led_notx_true'
    '","t":"bool","v":false},{"k":"_titleColor","t":"color","v":{"R":32,"G":32,"B":32,"A":255,"IsKnow'
    'nColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"ff202020"}},{"k'
    '":"led_process_when_hidden","t":"bool","v":false},{"k":"_colour","t":"color","v":{"R":0,"G":255,'
    '"B":0,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"N'
    'ame":"ff00ff00"}},{"k":"_markerColour","t":"color","v":{"R":192,"G":192,"B":192,"A":255,"IsKnown'
    'Color":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"ffc0c0c0"}},{"k"'
    ':"_decay","t":"float","v":0.05},{"k":"_showSubMarker","t":"bool","v":true},{"k":"led_4char","t":'
    '"string","v":"VsuD"},{"k":"_spacerPadding","t":"float","v":0.1},{"k":"_text_1","t":"string","v":'
    '""},{"k":"_fadeOnTx","t":"bool","v":false},{"k":"_eyeScale","t":"float","v":0.5},{"k":"_attack",'
    '"t":"float","v":0.05},{"k":"_eyeBezelScale","t":"float","v":0.05},{"k":"led_notx_false","t":"boo'
    'l","v":false},{"k":"_historyColor","t":"color","v":{"R":32,"G":32,"B":32,"A":255,"IsKnownColor":'
    'false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"ff202020"}},{"k":"_show'
    'Marker","t":"bool","v":true},{"k":"_fadeOnRx","t":"bool","v":false},{"k":"_showType","t":"bool",'
    '"v":false},{"k":"_updateInterval","t":"int","v":100}],"CUSTOM_METER_BAR":[{"k":"_peakHold","t":"'
    'bool","v":false},{"k":"_barStyle","t":"barstyle","v":1},{"k":"_ignoreHistoryDuration","t":"int",'
    '"v":2000},{"k":"_segmentedSolidLowColour","t":"color","v":{"R":0,"G":139,"B":139,"A":255,"IsKnow'
    'nColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"DarkCyan"}},{"k":'
    '"meter_custom_title","t":"string","v":"Title"},{"k":"_highColor","t":"color","v":{"R":255,"G":0,'
    '"B":0,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Nam'
    'e":"Red"}},{"k":"_peakHoldMarkerColor","t":"color","v":{"R":255,"G":0,"B":0,"A":255,"IsKnownColo'
    'r":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Red"}},{"k":"_lowColor'
    '","t":"color","v":{"R":255,"G":255,"B":255,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedC'
    'olor":true,"IsSystemColor":false,"Name":"White"}},{"k":"_peakValue","t":"bool","v":true},{"k":"_'
    'titleColor","t":"color","v":{"R":169,"G":169,"B":169,"A":255,"IsKnownColor":true,"IsEmpty":false'
    ',"IsNamedColor":true,"IsSystemColor":false,"Name":"DarkGray"}},{"k":"_colour","t":"color","v":{"'
    'R":32,"G":32,"B":32,"A":255,"IsKnownColor":false,"IsEmpty":false,"IsNamedColor":false,"IsSystemC'
    'olor":false,"Name":"ff202020"}},{"k":"_markerColour","t":"color","v":{"R":255,"G":255,"B":0,"A":'
    '255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Yellow'
    '"}},{"k":"_decay","t":"float","v":0.2},{"k":"_showSubMarker","t":"bool","v":true},{"k":"_segment'
    'edSolidHighColour","t":"color","v":{"R":255,"G":165,"B":0,"A":255,"IsKnownColor":true,"IsEmpty":'
    'false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Orange"}},{"k":"_mmio_guid_0","t":"guid"'
    ',"v":"00000000-0000-0000-0000-000000000000"},{"k":"meter_custom_min","t":"float","v":0},{"k":"_m'
    'mio_variable_1","t":"string","v":"--DEFAULT--"},{"k":"_showHistory","t":"bool","v":true},{"k":"_'
    'hasSubIndicators","t":"bool","v":true},{"k":"_units","t":"units","v":0},{"k":"_fadeOnTx","t":"bo'
    'ol","v":false},{"k":"meter_custom_units","t":"string","v":"?"},{"k":"_subMarkerColour","t":"colo'
    'r","v":{"R":169,"G":169,"B":169,"A":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,'
    '"IsSystemColor":false,"Name":"DarkGray"}},{"k":"_mmio_guid_1","t":"guid","v":"00000000-0000-0000'
    '-0000-000000000000"},{"k":"_attack","t":"float","v":0.8},{"k":"_historyDuration","t":"int","v":4'
    '000},{"k":"_mmio_variable_0","t":"string","v":"--DEFAULT--"},{"k":"meter_custom_high","t":"float'
    '","v":7.5},{"k":"_historyColor","t":"color","v":{"R":238,"G":130,"B":238,"A":128,"IsKnownColor":'
    'false,"IsEmpty":false,"IsNamedColor":false,"IsSystemColor":false,"Name":"80ee82ee"}},{"k":"meter'
    '_custom_max","t":"float","v":10},{"k":"_peakValueColour","t":"color","v":{"R":255,"G":0,"B":0,"A'
    '":255,"IsKnownColor":true,"IsEmpty":false,"IsNamedColor":true,"IsSystemColor":false,"Name":"Red"'
    '}},{"k":"_showMarker","t":"bool","v":true},{"k":"_fadeOnRx","t":"bool","v":false},{"k":"_showTyp'
    'e","t":"bool","v":true},{"k":"_updateInterval","t":"int","v":50}]}')


if __name__ == "__main__":
    sys.exit(main())
