"""Driver and speech configuration, with references to existing DSH credentials."""
import json,os
from pathlib import Path
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'ai_config.json'


def is_placeholder(value):
    value=str(value or '').strip().lower()
    return not value or value.startswith(('your_','your-','<your','replace_')) or 'your_openai_compatible_endpoint' in value


def configuration_error(cfg):
    if is_placeholder(cfg.get('api_key')):return '聊天密钥尚未配置，请在桌面管理台填写 DeepSeek 等模型服务的 API 密钥'
    base=str(cfg.get('base_url','')).strip()
    try:endpoint=urlsplit(base)
    except ValueError:return '聊天服务地址无效，请在桌面管理台重新填写'
    if is_placeholder(base) or not endpoint.hostname or endpoint.scheme not in ('http','https') or endpoint.username or is_placeholder(endpoint.hostname):
        return '聊天服务地址尚未配置，请在桌面管理台填写完整的服务地址'
    if is_placeholder(cfg.get('model')):return '聊天模型名称尚未配置，请在桌面管理台填写模型名称'
    return ''


def read_raw(path=CONFIG):
    try:
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):return {}


def dsh_driver(home=None):
    import yaml
    home=Path(home or Path.home())
    paths=[home/'.dsh/profiles'/profile/'cordis.patch.yml' for profile in ('web','desktop')]
    path=next((p for p in paths if p.is_file()),None)
    if path is None:raise ValueError('没有找到本机 DSH 模型配置，请在管理台选择自定义模型服务')
    try:rows=yaml.safe_load(path.read_text(encoding='utf-8')) or []
    except (OSError,yaml.YAMLError):raise ValueError('DSH 模型配置无法读取，请在管理台手动设置') from None
    if not isinstance(rows,list):raise ValueError('DSH 模型配置格式不正确，请在管理台手动设置')
    selected=next((r.get('config',{}) for r in rows if isinstance(r,dict) and r.get('id')=='agent-default-model'),{})
    provider=selected.get('provider','');model=selected.get('model','')
    providers={}
    for row in rows:
        if isinstance(row,dict):providers.update((row.get('config') or {}).get('providers') or {})
    source=providers.get(provider) or {}
    base=source.get('baseURL') or ('https://api.deepseek.com' if provider=='deepseek' else '')
    ref=source.get('apiKeyEnv') or ('DEEPSEEK_API_KEY' if provider=='deepseek' else '')
    if not base or not ref or not model:raise ValueError('本地 DSH 尚未配置可读取的驱动模型，请在管理台手动设置')
    return dict(base_url=base,model=model,api_key_ref=ref,driver_source='dsh')


def resolve_config(raw,home=None):
    cfg=dict(raw);home=Path(home or Path.home())
    if cfg.get('driver_source')=='dsh':cfg.update(dsh_driver(home))
    ref=cfg.get('api_key_ref')
    if ref:
        import yaml
        try:stored=(yaml.safe_load((home/'.dsh/.credentials.yaml').read_text(encoding='utf-8')) or {}).get('refs',{})
        except (OSError,ValueError,yaml.YAMLError):stored={}
        cfg['api_key']=stored.get(ref) or os.environ.get(ref,'')
    return cfg


def load_config(path=CONFIG):
    return resolve_config(read_raw(path))


def vision_config(cfg):
    """Use the configured visual endpoint, including the older Zhipu setup."""
    from core.model_endpoint import base_of
    target = dict(cfg)
    base = str(cfg.get('vision_base_url') or '').strip()
    model = str(cfg.get('vision_model') or '').strip()
    key = str(cfg.get('vision_api_key') or '').strip()
    legacy_key = str(cfg.get('zhipu_api_key') or '').strip()
    zhipu = 'https://open.bigmodel.cn/api/paas/v4'
    if (not key and legacy_key and (not model or model.startswith('glm-'))
            and (not base or base_of(base) == zhipu)):
        return dict(target, base_url=zhipu,
                    model=model or 'glm-4.6v-flash', api_key=legacy_key)
    main_base = cfg.get('base_url') or ''
    inherit = not base or (base_of(base) and base_of(base) == base_of(main_base))
    return dict(target, base_url=base or main_base, model=model or cfg.get('model', ''),
                api_key=key or (cfg.get('api_key', '') if inherit else ''))


def public_settings():
    try:
        cfg=load_config();endpoint=urlsplit(cfg.get('base_url',''))
        provider=cfg.get('tts_provider') or 'edge'
        voice='咕嘎专属声线' if provider=='guga_local' else cfg.get('edge_voice') or 'zh-CN-XiaoyiNeural'
        return dict(driver=dict(source=cfg.get('driver_source','custom'),model=cfg.get('model',''),host=endpoint.hostname,
                    credential_configured=not is_placeholder(cfg.get('api_key')),ready=not bool(configuration_error(cfg)),
                    error=configuration_error(cfg)),speech=dict(provider=provider,voice=voice),manual_voice_independent=True)
    except Exception:return dict(driver=dict(credential_configured=False),speech=dict(provider='edge'),manual_voice_independent=True)


def save_settings(updates,path=CONFIG):
    cfg=read_raw(path);cfg.update(updates)
    if cfg.get('driver_source')=='dsh':
        cfg.update(dsh_driver());cfg['api_key']=''
    if cfg.get('driver_source')!='dsh':
        endpoint=urlsplit(str(cfg.get('base_url','')))
        if endpoint.scheme not in ('http','https') or not endpoint.netloc or endpoint.username:
            raise ValueError('模型地址需要完整的 http:// 或 https:// 服务地址')
        error=configuration_error(cfg)
        if error:raise ValueError(error)
    target=Path(path);temporary=target.with_name(target.name+'.pending')
    temporary.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    os.replace(temporary,target)
    return resolve_config(cfg)


def test_driver(config):
    import requests
    from core.chat_answer import request_payload
    cfg=resolve_config(config)
    error=configuration_error(cfg)
    if error:raise ValueError(error)
    payload=request_payload(cfg,[dict(role='user',content='只回复：连接成功')],'quick')
    payload.update(stream=False,max_tokens=64);payload.pop('stream_options',None)
    response=requests.post(cfg['base_url'].rstrip('/')+'/chat/completions',
        headers={'Authorization':'Bearer '+cfg['api_key'],'Content-Type':'application/json','User-Agent':'GugaDesktopPet/1.0','x-opencode-session':'guga-console-check'},
        json=payload,timeout=(5,18))
    try:
        if response.status_code!=200:
            explanation='密钥无效或已失效' if response.status_code==401 else '服务未接受请求'
            raise RuntimeError(f'HTTP {response.status_code}：{explanation}。未更改实时语音或朗读配置。')
        text=(response.json().get('choices') or [{}])[0].get('message',{}).get('content','')
        if not text:raise RuntimeError('服务未返回回答正文')
        return '连接成功 · '+str(cfg.get('model',''))
    finally:response.close()
