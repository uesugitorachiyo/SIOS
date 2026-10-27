"""Helper-owned Git; captures every byte without candidate hooks or filters."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import hashlib

from ..contract import below, digest, plain, relative, require


def git_environment():
    address = "factory" + "@" + "example.invalid"
    return {"PATH": os.defpath, "LANG": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0",
            "GIT_AUTHOR_NAME": "Local Factory", "GIT_COMMITTER_NAME": "Local Factory",
            "GIT_AUTHOR_EMAIL": address, "GIT_COMMITTER_EMAIL": address}


def rows(root, limit=100 * 1024 * 1024):
    root = plain(Path(root), directory=True)
    out, names, total = [], set(), 0
    for base, dirs, names_here in os.walk(root, followlinks=False, onerror=lambda e: (_ for _ in ()).throw(e)):
        for name in sorted(dirs + names_here):
            p = Path(base) / name
            rel = relative(p.relative_to(root).as_posix())
            require(all(c.casefold() != '.git' and c != '__pycache__' for c in rel.split('/')), 'candidate_metadata')
            require(rel.casefold() not in names, "case_alias")
            names.add(rel.casefold())
            s = p.lstat()
            require(stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode), "special_candidate_file")
            require(not getattr(s, "st_file_attributes", 0) & 0x400, "candidate_reparse")
            if stat.S_ISREG(s.st_mode):
                require(s.st_nlink == 1, "hardlinked_candidate")
                raw = p.read_bytes()
                total += len(raw)
                require(total <= limit, "source_overflow")
                out.append({"path": rel, "mode": "100755" if os.name != "nt" and s.st_mode & 0o111 else "100644", "bytes": len(raw), "sha256": digest(raw)})
    return sorted(out, key=lambda x: x["path"].encode())


def tree_identity(root):
    """Recompute the fixed SHA-1 Git tree from raw files without trusting metadata."""
    def oid(kind, data):
        return hashlib.sha1(kind + b" " + str(len(data)).encode() + b"\0" + data).digest()
    tree = {}
    for row in rows(root):
        node = tree
        parts = row["path"].split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        raw = below(root, row["path"]).read_bytes()
        node[parts[-1]] = (row["mode"].encode(), oid(b"blob", raw))
    def encode(node):
        data = b""
        for name, value in sorted(node.items(), key=lambda x: x[0].encode() + (b"/" if type(x[1]) is dict else b"")):
            mode, content = (b"40000", encode(value)) if type(value) is dict else value
            data += mode + b" " + name.encode() + b"\0" + content
        return oid(b"tree", data)
    return encode(tree).hex()


class Capture:
    def __init__(self, supervisor, git_path):
        self.supervisor, self.run, self.git_path = supervisor, supervisor.run, git_path
        self.meta = self.run / "meta.git"
        self.work = self.run / "work"
        self.hooks = self.run / "empty-hooks"
        self.hooks.mkdir()
        self.command(["init", "--bare", str(self.meta)])
        (self.meta / "info/attributes").write_bytes("* -filter -ident -text -crlf -eol -working-tree-encoding -export-ignore -export-subst\n".encode("utf-8"))

    def command(self, args, work=None):
        argv = [self.git_path, "-c", "core.hooksPath=" + str(self.hooks), "-c", "core.attributesFile=" + os.devnull,
                "-c", "core.autocrlf=false", "-c", "core.filemode=false" if os.name == "nt" else "core.filemode=true", "-c", "core.excludesFile=" + os.devnull,
                "-c", "diff.external=", "-c", "commit.gpgSign=false", "-c", "core.fsmonitor=false",
                "-c", "core.precomposeunicode=false", "-c", "core.ignorecase=false"]
        if self.meta.exists():
            argv.append("--git-dir=" + str(self.meta))
        if work is not None:
            argv.extend(["--work-tree=" + str(work), "-C", str(work)])
        record = self.supervisor.call(argv + list(args), cwd=self.run, env=self.supervisor.git_environment() if hasattr(self.supervisor, "git_environment") else git_environment())
        return Path(record["stdout"]).read_bytes()

    def text(self, *args, work=None):
        return self.command(args, work).decode().strip()

    def tree(self, target):
        rows(target)
        self.command(["read-tree", "--empty"])
        self.command(["add", "-f", "-A", "--", "."], target)
        return self.text("write-tree")
