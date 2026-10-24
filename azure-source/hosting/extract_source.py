"""Offline bootstrap: extract authenticated source from an encrypted server snapshot.

Requires cryptography. Never prints a key or restores/starts data or workers.
"""
import argparse,base64,io,tarfile,zipfile
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def extract(backup,key_file,destination):
    raw=Path(backup).read_bytes();aad=b'drivecast-server-backup-v1';prefix=aad+b'\n'
    if not raw.startswith(prefix):raise ValueError('Unsupported backup.')
    key=base64.b64decode(Path(key_file).read_text().strip(),validate=True)
    offset=len(prefix);plain=AESGCM(key).decrypt(raw[offset:offset+12],raw[offset+12:],aad)
    with tarfile.open(fileobj=io.BytesIO(plain),mode='r:gz') as archive:
        member=archive.getmember('recovery/source.zip')
        if not member.isfile() or member.size>32*1024*1024:raise ValueError('Invalid source bundle.')
        source=archive.extractfile(member).read()
    destination=Path(destination).resolve()
    if destination.exists() and any(destination.iterdir()):raise ValueError('Use an empty source destination.')
    with zipfile.ZipFile(io.BytesIO(source)) as archive:
        for entry in archive.infolist():
            if not (destination/entry.filename).resolve().is_relative_to(destination) or entry.file_size>16*1024*1024:
                raise ValueError('Unsafe source entry.')
        destination.mkdir(parents=True,exist_ok=True);archive.extractall(destination)
    return destination


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backup',required=True);parser.add_argument('--key-file',required=True);parser.add_argument('--destination',required=True)
    args=parser.parse_args();extract(args.backup,args.key_file,args.destination);print('Authenticated source extracted. Data and workers were not started.')
