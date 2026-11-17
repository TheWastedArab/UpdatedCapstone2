"""Fetch Eclipse bug reports from the Zenodo record (doi:10.5281/zenodo.14229936).

    python -m ml.fetch_zenodo_eclipse JDT CDT PDE          # one or more projects
    python -m ml.fetch_zenodo_eclipse --list

The published archive is 17 GB because every row carries the full comment
thread, activity log and attachment list. This script reads one project's CSV
straight out of the remote zip with HTTP range requests and keeps only the
columns the three models need, so nothing but the slim result touches the disk
(roughly 1% of the original size). Output: data/raw/zenodo_eclipse/<PROJECT>.csv
in the canonical schema of ml/datasets.py, plus status, resolution and priority.

Licence of the data: CC BY-SA 4.0 (Eclipse issue report dataset, diverso-lab).
"""
import argparse
import io
import struct
import sys
import time
import zipfile
import zlib
from pathlib import Path

import httpx
import inflate64
import pandas as pd

URL = "https://zenodo.org/api/records/15348468/files/Eclipse_dataset.zip/content"
OUT = Path(__file__).resolve().parents[2] / "data" / "raw" / "zenodo_eclipse"
COLUMNS = {
    "ID": "bug_id", "Summary": "title", "Description": "description", "Severity": "severity", "Assigned to": "assignee",
    "Component": "component", "Product": "product", "Dupe of": "dup_id", "Creation time": "created",
    "Status": "status", "Resolution": "resolution", "Priority": "priority",
}
MAX_DESCRIPTION = 5000


class RemoteFile(io.RawIOBase):
    """A seekable read-only view of a URL, backed by HTTP range requests."""

    def __init__(self, url: str):
        self.url, self.position, self.transferred = url, 0, 0
        self.client = httpx.Client(follow_redirects=True, timeout=120, headers={"User-Agent": "bug-dataset-fetch/1.0"})
        self.size = int(self.client.head(url).headers["content-length"])

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.position

    def seek(self, offset: int, whence: int = 0) -> int:
        self.position = offset if whence == 0 else self.position + offset if whence == 1 else self.size + offset
        return self.position

    def readinto(self, buffer) -> int:
        length = min(len(buffer), self.size - self.position)
        if length <= 0:
            return 0
        for attempt in range(6):
            try:
                response = self.client.get(self.url, headers={"Range": f"bytes={self.position}-{self.position + length - 1}"})
                response.raise_for_status()
                data = response.content
                break
            except httpx.HTTPError:
                if attempt == 5:
                    raise
                time.sleep(2 ** attempt)
        buffer[:len(data)] = data
        self.position += len(data)
        self.transferred += len(data)
        return len(data)


class MemberStream(io.RawIOBase):
    """Decompressed bytes of one zip member, streamed over a single ranged download.

    The archive is compressed with Deflate64 (zip method 9), which the standard
    library cannot read, so the member is located by hand and inflated with the
    inflate64 package. A dropped connection resumes from the last byte received.
    """

    def __init__(self, remote: RemoteFile, member: zipfile.ZipInfo):
        header = bytearray(30)
        remote.seek(member.header_offset)
        remote.readinto(header)
        name_length, extra_length = struct.unpack("<HH", header[26:30])
        self.remote = remote
        self.offset = member.header_offset + 30 + name_length + extra_length
        self.end = self.offset + member.compress_size
        if member.compress_type == 9:
            self.inflate = inflate64.Inflater().inflate
        elif member.compress_type == zipfile.ZIP_DEFLATED:
            self.inflate = zlib.decompressobj(-15).decompress
        else:
            raise NotImplementedError(f"zip compression method {member.compress_type}")
        self.pending = b""
        self.chunks = self._download()

    def _download(self):
        failures = 0
        while self.offset < self.end:
            try:
                headers = {"Range": f"bytes={self.offset}-{self.end - 1}"}
                with self.remote.client.stream("GET", self.remote.url, headers=headers) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes(4 * 1024 * 1024):
                        self.offset += len(chunk)
                        self.remote.transferred += len(chunk)
                        failures = 0
                        yield chunk
            except httpx.HTTPError:
                failures += 1
                if failures > 8:
                    raise
                time.sleep(min(60, 2 ** failures))

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self.pending:
            chunk = next(self.chunks, None)
            if chunk is None:
                return 0
            self.pending = self.inflate(chunk)
        count = min(len(buffer), len(self.pending))
        buffer[:count] = self.pending[:count]
        self.pending = self.pending[count:]
        return count


def fetch(remote: RemoteFile, member: zipfile.ZipInfo, target: Path) -> None:
    started, rows, first, reported = time.time(), 0, True, 0.0
    partial = target.with_suffix(".part")
    stream = io.BufferedReader(MemberStream(remote, member), buffer_size=8 * 1024 * 1024)
    reader = pd.read_csv(stream, usecols=list(COLUMNS), chunksize=2000, dtype=str, encoding="utf-8", encoding_errors="replace")
    for chunk in reader:
        chunk = chunk.rename(columns=COLUMNS)[list(COLUMNS.values())]
        chunk["description"] = chunk["description"].str.slice(0, MAX_DESCRIPTION)
        chunk.to_csv(partial, mode="w" if first else "a", header=first, index=False)
        first = False
        rows += len(chunk)
        if time.time() - reported > 30:
            reported = time.time()
            print(f"  {member.filename}: {rows} rows, {remote.transferred / 1e6:.0f} of {member.compress_size / 1e6:.0f} MB, "
                  f"{time.time() - started:.0f}s", flush=True)
    partial.replace(target)
    print(f"Wrote {rows} rows to {target} ({target.stat().st_size / 1e6:.1f} MB) in {time.time() - started:.0f}s", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("projects", nargs="*", help="Project names, e.g. JDT CDT PDE Platform")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    remote = RemoteFile(URL)
    archive = zipfile.ZipFile(io.BufferedReader(remote, buffer_size=32 * 1024 * 1024))
    members = {Path(i.filename).name.split("_dataset")[0].upper(): i for i in archive.infolist() if i.filename.endswith(".csv")}
    if args.list or not args.projects:
        for name, info in members.items():
            print(f"{name:<10}{info.compress_size / 1e6:>9.0f} MB to transfer")
        return
    OUT.mkdir(parents=True, exist_ok=True)
    for project in args.projects:
        info = members.get(project.upper())
        if info is None:
            sys.exit(f"Unknown project {project}; available: {', '.join(members)}")
        target = OUT / f"{project.upper()}.csv"
        if target.exists():
            print(f"{target.name} already present, skipping")
            continue
        remote.transferred = 0
        fetch(remote, info, target)


if __name__ == "__main__":
    main()
