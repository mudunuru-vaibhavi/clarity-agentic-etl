"""CI helper: upload the generated outbox to the SFTP container, like Clarity's nightly push."""

import os
import sys
from pathlib import Path

import paramiko

host, port, user, root = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
local = Path(sys.argv[5])
t = paramiko.Transport((host, port))
t.connect(username=user, password=os.environ["SFTP_PASSWORD"])
sftp = paramiko.SFTPClient.from_transport(t)
for drop in sorted(p for p in local.iterdir() if p.is_dir()):
    remote = f"{root}/{drop.name}"
    sftp.mkdir(remote)
    for f in sorted(drop.iterdir(), key=lambda p: p.name == "manifest.json"):  # manifest last
        sftp.put(str(f), f"{remote}/{f.name}")
print(f"uploaded {len(list(local.iterdir()))} drops")
sftp.close()
t.close()
