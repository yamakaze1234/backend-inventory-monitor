"""准备离线签名更新清单；不上传、不发布，不输出私钥。"""
import argparse
import base64
import hashlib
import json
from pathlib import Path
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--public-key', type=Path)
    parser.add_argument('--exe', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--url', action='append', default=[])
    parser.add_argument('--notes', type=Path)
    parser.add_argument('--output', type=Path)
    args=parser.parse_args()
    if not args.key.exists():
        # 私钥只写到发布者指定的外部目录，使用排他创建避免覆盖已有密钥。
        args.key.parent.mkdir(parents=True,exist_ok=True)
        with args.key.open('xb') as stream:
            stream.write(Ed25519PrivateKey.generate().private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    key=serialization.load_pem_private_key(args.key.read_bytes(),password=None)
    if args.public_key:
        args.public_key.write_text(base64.b64encode(key.public_key().public_bytes(
            serialization.Encoding.Raw,serialization.PublicFormat.Raw)).decode(),encoding='ascii')
    if args.exe:
        if not args.version or not args.url or not args.output:
            parser.error('签名需要 version、url 和 output')
        payload=json.dumps(dict(app='backend-inventory-monitor',platform='windows-x86_64',
            version=args.version,size=args.exe.stat().st_size,sha256=hashlib.sha256(args.exe.read_bytes()).hexdigest(),
            urls=args.url,notes=args.notes.read_text(encoding='utf-8') if args.notes else ''),ensure_ascii=False).encode()
        args.output.write_text(json.dumps(dict(payload=base64.b64encode(payload).decode(),
            signature=base64.b64encode(key.sign(payload)).decode())),encoding='utf-8')


if __name__=='__main__':
    main()
