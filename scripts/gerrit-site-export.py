# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 The Linux Foundation
"""\
Usage: python3 scripts/gerrit-site-export.py <seed|final|backup> --out <dir> [options]

Copy a Gerrit site to <out>/site and <out>/git with manifest.json; stdlib, Python 3.8+.
Every mode needs gerrit_export_git.py and gerrit_export_host.py beside the tool.
  seed                Incremental rsync; safe while Gerrit runs, and to re-run.
  final               Last rsync, --checksum; refused while Gerrit may serve the site.
  backup              Packed live final: git checks repositories; db/, data/ (H2 flags,
                      events, queue) may be inconsistent: snapshot a stopped site.
  --site <dir>        Gerrit site (default: $GERRIT_SITE).
  --git <dir>         Repositories (default: gerrit.basePath of the site).
  --out <dir>         Owner-only export dir; may not overlap the site or repositories.
  --i-stopped-writes  final: writes are stopped another way; skip the check.
  --pack              final, backup: pack site.tar.zst, git.tar.zst.
  --zstd <how>        Packer: auto (default; zstd, else Python 3.14+), tool or python.
  --encrypt <spec>    Implies --pack: age:<recipient> or gpg:<key-id>; drops plaintext.
  --upload <url>      Implies --pack: aws s3 cp of the outputs to s3://bucket/prefix.
  --sse <how>         --upload's encryption: aws:kms (default), AES256, or none, which
                      leaves it to the bucket's default encryption.
  --upload-urls <file>  Implies --pack, not with --upload: HTTPS PUT of each output to
                      the pre-signed URL a JSON object in <file> maps its name to; no
                      aws needed, no old manifest removed: use a fresh prefix each time.
  --include-cache     Copy <site>/cache too; the import rebuilds caches.
  --exclude <path>    Leave out <path> under the site; repeatable; may end in *.
  --exclude-from <file>  --exclude each line of <file>; skips blank and # lines.
  --drop-foreign-links  Leave out links site-import refuses (absolute, or leaving the
                      site), listed in the manifest; without it, they stop the run.
  --dry-run           Print the commands without writing anything.

Left out of <out>/site, any type: cache, index, logs, tmp, bin, .chart, *.lock files,
.gerritcodereview/tmp, repos as resolved, credentials (.ssh, etc/lfs.secure.config,
.config/gerrit_to_platform). Refused: store, output symlinks; a store, hard link or link
that may leave (absolute, '..') copying credentials; a link on, above or under a path
site-import needs (etc/gerrit.config, data/replication, SAML keystore...), or one left
out. Old manifest (uploaded too), then archives, manifest last: it marks a complete
export; errors exit non-zero. -h, --help.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, NoReturn

CREDENTIALS = [".ssh", ".config/gerrit_to_platform", "etc/lfs.secure.config"]
EXCLUDED = ["cache", "index", "logs", "tmp", "bin", ".gerritcodereview/tmp", ".chart"]
EXCLUDED += CREDENTIALS  # Last; the chart supplies them from Secrets.
G2P = f"{CREDENTIALS[1]}/replication.config"  # May link to etc/replication.config.
VALUES: tuple[str, ...] = ("--site", "--git", "--out", "--encrypt", "--upload")
VALUES += ("--exclude", "--zstd", "--sse", "--upload-urls", "--exclude-from")
FLAGS: tuple[str, ...] = ("--i-stopped-writes", "--pack", "--include-cache")
FLAGS += ("--dry-run", "--drop-foreign-links")
PROG = "gerrit-site-export.py"
NEW_FILE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
S3_COPY, SSE = ["aws", "s3", "cp"], ("aws:kms", "AES256", "none")
ENCRYPT = {"age": ["-r"], "gpg": ["--batch", "--yes", "--encrypt", "--recipient"]}
PARTS, ESCAPE = ("site", "git"), "".maketrans({c: "\\" + c for c in "*?[\\"})
ARCHIVES = [f"{p}.tar.zst{s}" for p in PARTS for s in ("", ".age", ".gpg")]
VERSION_JAR = "WEB-INF/lib/com_google_gerrit_common_libversion.jar"
VERSION_FILE = "com/google/gerrit/common/Version"  # In VERSION_JAR.
HEAD_PROBES = (["symbolic-ref", "-q", "HEAD"], ["rev-parse", "--verify", "HEAD^0"])
# Each .py beside this tool: repositories, --exclude, --upload-urls; links, packing.
COMPANION, HOST = COMPANIONS = ("gerrit_export_git", "gerrit_export_host")
FOR_EACH_REF = ["for-each-ref", "--format=%(refname) %(objectname) %(*objectname)"]


def die(message: str | OSError) -> NoReturn:
    sys.exit(f"{PROG}: error: {' '.join(str(message).splitlines())}")


def say(message: str) -> None:
    print(f"{PROG}: {message}", file=sys.stderr)


def show(command: list[str]) -> None:
    print("+ " + " ".join(shlex.quote(part) for part in command), flush=True)


def run(command: list[str], capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(command, capture_output=capture, text=True, check=False)
    except FileNotFoundError:
        die(f"required tool not found: {command[0]}")


def step(opts: Any, command: list[str], what: str, tolerate: int = 0) -> int:
    if opts.dry_run:
        show(command)
        return 0
    if (status := run(command).returncode) not in (0, tolerate):
        die(f"{what} failed with exit status {status}")
    return status


def parse_args(argv: list[str]) -> Any:
    opts = SimpleNamespace(mode="", **{o[2:].replace("-", "_"): "" for o in VALUES})
    (fields := vars(opts)).update({o[2:].replace("-", "_"): False for o in FLAGS})
    opts.site, opts.exclude, args = os.environ.get("GERRIT_SITE", ""), [], list(argv)
    opts.zstd, opts.urls = "auto", {}
    while args:
        arg = args.pop(0)
        name, equals, value = arg.partition("=")
        if arg in ("-h", "--help"):
            sys.stdout.write(__doc__ or "")
            sys.exit(0)
        elif arg in ("seed", "final", "backup"):
            if opts.mode:
                die(f"mode given twice: {opts.mode} and {arg}")
            opts.mode = arg
        elif arg in FLAGS:
            setattr(opts, arg[2:].replace("-", "_"), True)
        elif name in VALUES:
            if not equals and args and not args[0].startswith("--"):
                value = args.pop(0)
            if not value:
                die(f"{name} needs a value")
            if name in ("--exclude", "--exclude-from"):
                opts.exclude += [value] if name == "--exclude" else exclude_lines(value)
            else:
                fields[name[2:].replace("-", "_")] = value
        else:
            die(f"unknown argument: {arg} (see --help)")
    kind, _, who = opts.encrypt.partition(":")
    bad_key = opts.encrypt and (kind not in ENCRYPT or not who)
    bad_s3 = opts.upload and (opts.upload[:5] != "s3://" or opts.upload == "s3://")
    sending = bool(opts.upload or opts.upload_urls)
    opts.pack = opts.pack or bool(opts.encrypt) or sending or opts.mode == "backup"
    seed_pack = opts.pack and opts.mode == "seed"
    for failed, message in (
        (not opts.mode, "give a mode, seed, final or backup (see --help)"),
        (not opts.site, "no site: pass --site or set GERRIT_SITE"),
        (not opts.out, "no export directory: pass --out"),
        (bad_key, "--encrypt takes age:<recipient> or gpg:<key-id>"),
        (bad_s3, "--upload takes s3://bucket/prefix"),
        (opts.sse not in ("", *SSE), "--sse takes aws:kms, AES256 or none"),
        (opts.sse and not opts.upload, "--sse needs --upload"),
        (opts.upload and opts.upload_urls, "pass --upload or --upload-urls, not both"),
        (seed_pack, "--pack, --encrypt and uploads need final or backup mode"),
    ):
        if failed:
            die(message)
    if missing := [f"{m}.py" for m in COMPANIONS if not importlib.util.find_spec(m)]:
        die(
            f"every mode needs {' and '.join(missing)} beside the script: copy the"
            " tool with its modules, as the chart image holds them"
        )
    opts.excluded = [*EXCLUDED[opts.include_cache :], *opts.exclude]  # cache is first
    if opts.upload_urls:  # Before anything is written; raises an OSError to refuse.
        suffix = f".{kind}" if opts.encrypt else ""
        names = [*(f"{p}.tar.zst{suffix}" for p in PARTS), "manifest.json"]
        opts.urls = importlib.import_module(COMPANION).read_urls(
            opts.upload_urls, names
        )
    return opts


def exclude_lines(path: str) -> list[str]:
    """The --exclude patterns in path, one a line; blank and # lines skip."""
    try:
        lines = Path(path).read_text(encoding="utf-8").split("\n")
    except (OSError, UnicodeError) as exc:
        die(f"--exclude-from {path}: {exc}")
    return [line for line in lines if line.strip() and line[:1] != "#"]


def readable_dir(path: str, what: str) -> str:
    if not (os.path.isdir(path) and os.access(path, os.R_OK | os.X_OK)):
        die(f"cannot read {what} directory: {path}")
    if (real := os.path.realpath(path)) != os.path.realpath(os.path.abspath(path)):
        die(f"{what} path {path} has '..' after a symlink; pass {real}")
    return real


def host_keys(etc: Path) -> list[Path]:
    keys = sorted(etc.glob("ssh_host_*"))
    return [k for k in keys if k.suffix != ".pub" and stat.S_ISREG(k.lstat().st_mode)]


def require_tools(opts: Any, site: str) -> None:
    optional = {"ssh-keygen": host_keys(Path(site, "etc")), "zstd": opts.zstd == "tool"}
    optional.update({opts.encrypt.partition(":")[0]: opts.encrypt, "aws": opts.upload})
    needed = ["rsync", "git", *(tool for tool, use in optional.items() if use)]
    if missing := [tool for tool in needed if not shutil.which(tool)]:
        die(f"required tool{'s' * (len(missing) > 1)} not found: {', '.join(missing)}")


def base_path(site: str) -> str:
    if not os.access(config := os.path.join(site, "etc", "gerrit.config"), os.R_OK):
        die(f"cannot read {config}; pass --git")
    result = run(["git", "config", "-f", config, "--get", "gerrit.basePath"], True)
    if result.returncode > 1:
        die(f"cannot read gerrit.basePath from {config}: {result.stderr}")
    if not result.stdout.strip():
        die(f"gerrit.basePath is not set in {config}; pass --git")
    return result.stdout.strip()


def refuse_unsafe(path: Path, regular: bool) -> None:
    # rsync --delete follows a symlinked root; open and rename follow links.
    mode = os.lstat(path).st_mode if os.path.lexists(path) else stat.S_IFREG
    if stat.S_ISLNK(mode):
        die(f"refusing to write through symlink: {path}")
    if regular and not stat.S_ISREG(mode):
        die(f"not a regular file: {path}")


def left_out(opts: Any, site: str, git_dir: str, git_given: str) -> list[str]:
    """The site paths the copy leaves out, by name; --exclude adds patterns."""
    # Any type matches; a linked basePath goes as both, or as its first linked ancestor.
    names, given = list(opts.excluded), os.path.abspath(opts.site)
    for path, base in ((git_given, site), (git_given, given), (git_dir, site)):
        if path != base and os.path.commonpath([path, base]) == base:
            p = Path(path[len(base.rstrip("/")) + 1 :]).parts
            up = [i for i in range(1, len(p)) if Path(base, *p[:i]).is_symlink()]
            names.append("/".join(p[: (up or [len(p)])[0]]))
    return names


def copy(opts: Any, site: str, git_dir: str, names: list[str], out: Path) -> None:
    sites = ("/var/gerrit", site, os.path.abspath(opts.site))
    g2p = [f"{root}/etc/replication.config" for root in sites]
    for cred, top in ((c, os.path.join(site, c)) for c in CREDENTIALS):  # As import
        bad = [str(p) for p in (Path(top), *Path(top).parents) if p.is_symlink()]
        for f in () if bad else tree_paths(top) if os.path.isdir(top) else [top]:
            to = os.readlink(f) if os.path.islink(f) else ""
            safe = cred == ".ssh" and to[:1] != "/" and ".." not in to.split("/")
            safe = not to or safe or f[len(site) + 1 :] == G2P and to in g2p
            bad += [f] * (not safe or os.path.isfile(f) and os.stat(f).st_nlink > 1)
        if os.path.exists(top) and os.path.commonpath([top, git_dir]) in (top, git_dir):
            bad.append(git_dir)
        if bad:
            die(f"{bad[0]} could copy credential {top} under another name")
    names = [*names, *opts.dropped]
    globs = {**{n: n.translate(ESCAPE) for n in names}, **{n: n for n in opts.exclude}}
    filters = [f"--exclude=/{glob}" for glob in globs.values()]
    rsync = ["rsync", "-a", "--chmod=go-rwx", "--delete"]
    rsync += ["--checksum"] * (opts.mode == "final")
    git = [("git", git_dir, ("--delete-excluded",))] * (opts.mode != "backup")
    for part, source, extra in [("site", site, ("--delete-excluded", *filters)), *git]:
        command = [*rsync, "--exclude=*.lock", *extra, f"{source}/", f"{out}/{part}/"]
        # 24: files vanished mid-copy, expected while Gerrit writes.
        if step(opts, command, "rsync", 0 if opts.mode == "final" else 24) == 24:
            say("warning: files vanished during the copy; re-run to copy them")
    if not git:  # rsync would race live repositories, so backup uses COMPANION.
        importlib.import_module(COMPANION).copy(git_dir, out, opts.dry_run)


def pack(opts: Any, out: Path) -> list[Path]:
    archives = []
    for part in PARTS:
        archive = out / f"{part}.tar.zst"
        importlib.import_module(HOST).pack(out / part, archive, opts.zstd, opts.dry_run)
        if opts.encrypt:
            kind, _, who = opts.encrypt.partition(":")
            sealed = archive.with_name(f"{archive.name}.{kind}")
            command = [kind, *ENCRYPT[kind], who, "-o", str(sealed), str(archive)]
            step(opts, command, f"{kind} encryption of {archive.name}")
            refuse_unsafe(sealed, regular=True)
            step(opts, ["rm", "-f", str(archive)], f"removal of {archive.name}")
            archive = sealed
        archives.append(archive)
    return archives


def tree_paths(root: Path | str, err: Any = die) -> Iterator[str]:
    return (os.path.join(t, n) for t, d, f in os.walk(root, onerror=err) for n in d + f)


def gerrit_version(site: str) -> str | None:
    war = Path(site, "bin", "gerrit.war")
    try:  # A release war keeps its version in a nested jar, not in its manifest.
        with zipfile.ZipFile(war) as archive, archive.open(VERSION_JAR) as nested:
            text = zipfile.ZipFile(nested).read(VERSION_FILE).decode()
    except (FileNotFoundError, KeyError):  # No war, or one that records no version.
        return None
    except (UnicodeError, zipfile.BadZipFile) as exc:
        die(f"cannot read the Gerrit version from {war}: {exc}")
    return text.strip().lstrip("v") or None


def build_manifest(opts: Any, site: str, git_dir: str, out: Path) -> dict[str, Any]:
    repos, keys = {}, {}
    for top, dirs, _files in os.walk(out / "git", onerror=die):
        for name in [d for d in dirs if d.endswith(".git")]:
            dirs.remove(name)
            rel = Path(top, name).relative_to(out / "git").as_posix()
            repo = ["git", "-c", "safe.directory=*", f"--git-dir={Path(top, name)}"]
            result = run([*repo, *FOR_EACH_REF], True)
            if result.returncode:
                die(f"cannot read refs of repository: {rel}\n{result.stderr.strip()}")
            refs = [line.split(" ") for line in result.stdout.splitlines()]
            cfg = next((r[2] or r[1] for r in refs if r[0] == "refs/meta/config"), None)
            heads = (run([*repo, *p], True).stdout.strip() for p in HEAD_PROBES)
            head = next(filter(None, heads), None)  # The ref HEAD names, or its commit.
            repos[rel] = {"refs": len(refs), "meta_config": cfg, "head": head}
    for key in host_keys(out / "site" / "etc"):
        result = run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(key)], True)
        fields = result.stdout.split()
        if result.returncode or len(fields) < 2 or not fields[1].startswith("SHA256:"):
            die(f"cannot fingerprint SSH host key: {key.name}")
        keys[key.name] = fields[1]
    size = {n: sum(os.lstat(p).st_size for p in tree_paths(out / n)) for n in PARTS}
    git = {"path": git_dir, "bytes": size["git"]}
    h2 = {"final": "stopped", "backup": "best-effort"}.get(opts.mode)
    return {
        "schema": 1,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": opts.mode,
        **({"consistency": {"git": "verified", "h2": h2}} if h2 else {}),
        "gerrit_version": gerrit_version(site),
        "site": {"path": site, "bytes": size["site"]},
        "git": {**git, "repositories": dict(sorted(repos.items()))},
        "ssh_host_keys": keys,
        "excluded": opts.excluded,
        **({"dropped_links": opts.dropped} if opts.dropped else {}),
    }


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:
        die(f"cannot compute the sha256 of {path.name}: {exc.strerror}")
    return digest.hexdigest()


def upload(opts: Any, files: list[tuple[Path, str]]) -> None:
    """Upload each (path, name) in files, in order: the manifest goes last."""
    if opts.upload_urls:
        importlib.import_module(COMPANION).upload(files, opts.urls, opts.dry_run)
        return
    prefix = opts.upload.rstrip("/")
    # Old manifest first: it names archives about to change.
    remove = ["aws", "s3", "rm", f"{prefix}/manifest.json"]
    step(opts, remove, "removal of the uploaded manifest.json")
    sse = [] if opts.sse == "none" else ["--sse", opts.sse or SSE[0]]
    for path, name in files:
        step(opts, [*S3_COPY, *sse, str(path), f"{prefix}/{name}"], f"upload of {name}")


def main(argv: list[str]) -> None:
    os.umask(0o077)  # Before anything is written: every output is owner-only.
    opts = parse_args(argv)
    host = importlib.import_module(HOST)
    opts.zstd = host.zstd_method(opts.zstd, opts.pack)
    site = readable_dir(opts.site, "site")
    require_tools(opts, site)
    git_raw = opts.git or os.path.join(site, base_path(site))
    git_dir, git_given = readable_dir(git_raw, "repository"), os.path.abspath(git_raw)
    if opts.exclude:  # Before anything is written; raises an OSError to refuse.
        importlib.import_module(COMPANION).refuse_exclude(opts, EXCLUDED, git_raw)
    out = Path(opts.out).resolve()  # Before mkdir: no '..' or link dodges the check.
    for source in (site, git_dir):
        if os.path.commonpath([str(out), source]) in (str(out), source):
            die(f"export directory {out} overlaps {source}")
    # Walk errors pass: seed sees entries vanish, and rsync fails on unreadable ones.
    if links := [p for p in tree_paths(git_dir, err=None) if os.path.islink(p)]:
        die(f"symlinks in the repository store are not followed: {', '.join(links)}")
    outputs = [out / n for n in ("manifest.json", "manifest.json.tmp", *ARCHIVES)]
    manifest, temp = outputs[:2]
    for path in (out / "site", out / "git", *outputs):
        refuse_unsafe(path, regular=path in outputs)
    if opts.mode == "final" and not opts.i_stopped_writes:
        host.refuse_running(site)
    names = left_out(opts, site, git_dir, git_given)
    store = names[len(opts.excluded) :]  # The repository store, packed as git/.
    needed = importlib.import_module(COMPANION).protected(site)
    host.refuse_structure(site, needed, names, store, opts.exclude)
    found = host.foreign_links(site, names, opts.exclude)
    foreign = [  # copy() checks the credential paths and their parents.
        k for k in found if not any(f"{c}/".startswith(f"{k[0]}/") for c in CREDENTIALS)
    ]
    opts.dropped = [name for name, _ in foreign]
    if foreign:  # As --exclude patterns: none may be site structure.
        importlib.import_module(COMPANION).refuse_exclude(
            SimpleNamespace(site=opts.site, exclude=[]), EXCLUDED, git_raw, opts.dropped
        )
    if foreign and not opts.drop_foreign_links:
        die(
            f"links site-import refuses: {', '.join(' -> '.join(k) for k in foreign)};"
            " leave each out with --exclude <path>, or pass --drop-foreign-links"
        )
    for name, target in foreign:
        say(f"leaving out {name} -> {target}, a link site-import refuses")
    dry = "dry run, nothing is written: " if opts.dry_run else ""
    say(f"{dry}{opts.mode} export of {site} into {out}")
    # Old manifest first, then archives: a failed run must not look complete.
    stale = [path for path in outputs if os.path.lexists(path)]
    removal = [["rm", "-f", *map(str, stale)]] * bool(stale)
    for command in [*removal, ["mkdir", "-p", f"{out}/site", f"{out}/git"]]:
        step(opts, command, command[0])
    try:
        copy(opts, site, git_dir, names, out)
        data = {} if opts.dry_run else build_manifest(opts, site, git_dir, out)
        archives = pack(opts, out) if opts.pack else []
        if opts.dry_run:
            show(["write", str(manifest)])
        else:
            data["archives"] = {a.name: sha256_of(a) for a in archives}
            with os.fdopen(os.open(temp, NEW_FILE, 0o600), "w") as handle:
                handle.write(json.dumps(data, indent=2) + "\n")
        if opts.upload or opts.upload_urls:
            upload(opts, [(a, a.name) for a in archives] + [(temp, "manifest.json")])
        if not opts.dry_run:
            os.replace(temp, manifest)
    except BaseException:  # Archives without a manifest would look usable.
        for path in [] if opts.dry_run else filter(os.path.isfile, outputs):
            path.unlink()
        raise
    say(f"done: {manifest}")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except OSError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        sys.exit(130)
