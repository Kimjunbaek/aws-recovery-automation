"""CodeBuild 전용 Terraform 설치. 공식 배포본의 SHA256과 비교합니다."""
import hashlib
import os
from pathlib import Path
import urllib.request
import zipfile

VERSION = "1.13.5"
base = f"https://releases.hashicorp.com/terraform/{VERSION}/"
name = f"terraform_{VERSION}_linux_amd64.zip"
checksums = urllib.request.urlopen(base + f"terraform_{VERSION}_SHA256SUMS", timeout=60).read().decode()
expected = next(line.split()[0] for line in checksums.splitlines() if line.split()[-1] == name)
data = urllib.request.urlopen(base + name, timeout=180).read()
if hashlib.sha256(data).hexdigest() != expected:
    raise RuntimeError("Terraform 체크섬 불일치")
Path(name).write_bytes(data)
Path("bin").mkdir(exist_ok=True)
with zipfile.ZipFile(name) as archive:
    Path("bin/terraform").write_bytes(archive.read("terraform"))
os.chmod("bin/terraform", 0o755)
