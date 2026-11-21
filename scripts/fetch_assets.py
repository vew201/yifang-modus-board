"""Fetch the pinned OFL font and verify it before replacing any local file."""
import hashlib
import json
from pathlib import Path
import secrets
from urllib.request import Request,urlopen
from urllib.parse import urlparse

ROOT=Path(__file__).resolve().parents[1]


def fetch(asset,opener=urlopen):
    target=(ROOT/asset['path']).resolve()
    if not target.is_relative_to((ROOT/'static/assets').resolve()):raise ValueError('Asset path must stay in static/assets')
    source=urlparse(asset['url'])
    if source.scheme!='https' or source.hostname!='raw.githubusercontent.com':raise ValueError('Unexpected asset source')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()==asset['sha256']:return False
    target.parent.mkdir(parents=True,exist_ok=True)
    pending=target.with_name(target.name+'.download-'+secrets.token_hex(6))
    try:
        size=0;digest=hashlib.sha256()
        with opener(Request(asset['url'],headers={'User-Agent':'MODUS-asset-fetch'}),timeout=30) as response,pending.open('wb') as output:
            while chunk:=response.read(1024*1024):
                size+=len(chunk)
                if size>asset['size']:raise ValueError('Asset exceeds expected size')
                digest.update(chunk);output.write(chunk)
        if size!=asset['size'] or digest.hexdigest()!=asset['sha256']:raise ValueError('Asset checksum did not match; existing asset retained')
        pending.replace(target);return True
    finally:pending.unlink(missing_ok=True)


def main():
    manifest=json.loads((ROOT/'asset-manifest.json').read_text(encoding='utf-8'))
    downloaded=sum(fetch(asset) for asset in manifest['assets'])
    print(f'Assets verified; downloaded {downloaded} pinned file(s)')


if __name__=='__main__':main()
