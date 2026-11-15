"""顺丰生产认证：仅保存本机加密凭据，界面永不回显校验码。"""
import base64
import json
import os
import tempfile
from pathlib import Path
from monitor_secret_store import protect, unprotect


def load(root):
    path = Path(root) / 'shipping-sf-auth.json'
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if data.get('checkWordEncrypted'):
            data['checkWord'] = unprotect(base64.b64decode(data['checkWordEncrypted'])).decode('utf-8')
        return data
    except Exception:
        raise ValueError('无法读取顺丰认证，请使用保存凭据的 Windows 用户或重新配置') from None


def summary(root):
    data = load(root)
    return dict(provider=data.get('provider') if data.get('provider') in ('sf-open', 'sf-sdk') else 'sf-open',
                partnerId=data.get('partnerId', ''), configured=bool(data.get('checkWord')))


def save(root, values):
    # 修改账号时必须重填校验码，避免错误复用另一账号的凭据。
    try:
        previous = load(root)
    except ValueError:
        # 新机器无法解密旧凭据时，仅允许使用完整新认证进行替换。
        if not values.get('partnerId') or not values.get('checkWord'):
            raise
        previous = {}
    provider = str(values.get('provider', 'sf-open'))
    partner = str(values.get('partnerId', '')).strip()
    secret = str(values.get('checkWord', '')).strip()
    if provider not in ('sf-open', 'sf-sdk'):
        raise ValueError('请选择顺丰生产接口类型')
    if not secret and partner == previous.get('partnerId'):
        secret = previous.get('checkWord', '')
    if not partner or not secret:
        raise ValueError('请填写顾客编码和生产校验码；修改顾客编码后须重新填写校验码')
    if any(any(c.isspace() for c in value) or '复制' in value for value in (partner, secret)):
        raise ValueError('顾客编码或校验码含空白或复制按钮文字，请重新核对')
    data = dict(provider=provider, environment='production', partnerId=partner,
                checkWordEncrypted=base64.b64encode(protect(secret.encode('utf-8'))).decode('ascii'))
    directory = Path(root)
    directory.mkdir(parents=True, exist_ok=True)
    # 写入完成后原子替换，巡检不会读取到半份认证文件。
    fd, temporary = tempfile.mkstemp(prefix='sf-auth-', suffix='.pending', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
        os.replace(temporary, directory / 'shipping-sf-auth.json')
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink()
