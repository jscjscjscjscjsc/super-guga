"""Find an exact song in configured local folders or the Openverse audio catalog."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urljoin, urlparse

import requests

from core.song_library import AUDIO_EXTENSIONS, song_key, split_title, store_audio

CATALOG = 'https://api.openverse.org/v1/audio/'
LICENSES = {'cc0', 'pdm', 'by', 'by-sa', 'by-nc', 'by-nc-sa'}
MEDIA_DOMAINS = ('jamendo.com', 'upload.wikimedia.org', 'archive.org', 'ccmixter.org', 'freemusicarchive.org')
MAX_BYTES = 80 * 1024 * 1024
NO_SOURCE = '没有找到可制作翻唱的完整音源。可以补充歌手名，或在“音乐与动作 → 导入歌曲音频”添加本地文件。'


def local_source(music, title, artist):
    folders = [Path.home() / 'Music']
    config = music / 'song_sources.json'
    if config.is_file():
        values = json.loads(config.read_text(encoding='utf-8')).get('folders', [])
        folders.extend(Path(folder) for folder in values)
    expected = song_key(title)
    author = song_key(artist)
    for folder in folders:
        if not folder.is_dir():
            continue
        matches = []
        for path in folder.rglob('*'):
            if path.suffix.lower() not in AUDIO_EXTENSIONS or not path.is_file():
                continue
            first, _, second = path.stem.partition(' - ')
            if author:
                if (song_key(first), song_key(second)) in ((expected, author), (author, expected)):
                    return path
            elif expected in (song_key(path.stem), song_key(first), song_key(second)):
                matches.append(path)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise ValueError('本地有多首同名录音，请说“歌名 - 歌手”，或手动导入想听的版本。')
    return None


def allowed_media(url):
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ''
        return (
            parsed.scheme == 'https' and not parsed.username and not parsed.password
            and parsed.port in (None, 443)
            and any(host == domain or host.endswith('.' + domain) for domain in MEDIA_DOMAINS)
        )
    except (TypeError, ValueError):
        return False


def select_recording(results, title, artist):
    matches = {}
    for item in results:
        if item.get('license') not in LICENSES or not allowed_media(item.get('url', '')):
            continue
        if not isinstance(item.get('duration'), (int, float)) or not 30000 <= item['duration'] <= 15 * 60000:
            continue
        if song_key(item.get('title') or '') != song_key(title):
            continue
        creator = str(item.get('creator') or '').strip()
        if not creator or (artist and song_key(creator) != song_key(artist)):
            continue
        matches.setdefault(song_key(creator), item)
    if len(matches) > 1:
        creators = '、'.join(item['creator'] for item in list(matches.values())[:3])
        raise ValueError(f'搜到多个同名版本（{creators}）。请说“{title} - 歌手”来选择。')
    if not matches:
        raise ValueError(NO_SOURCE)
    return next(iter(matches.values()))


def download_recording(session, url, target):
    deadline = monotonic() + 90
    for _ in range(5):
        if not allowed_media(url):
            raise ValueError('这个版本没有可直接使用的音频下载地址')
        with session.get(url, stream=True, allow_redirects=False, timeout=(5, 15)) as response:
            if response.is_redirect:
                url = urljoin(url, response.headers['Location'])
                continue
            response.raise_for_status()
            media_type = response.headers.get('Content-Type', '').split(';', 1)[0]
            if not (media_type.startswith('audio/') or media_type in ('video/ogg', 'application/ogg', 'application/octet-stream')):
                raise ValueError('搜到的地址未提供可用音频')
            if int(response.headers.get('Content-Length') or 0) > MAX_BYTES:
                raise ValueError('这首歌的音频文件过大')
            size = 0
            with target.open('wb') as output:
                for block in response.iter_content(65536):
                    size += len(block)
                    if size > MAX_BYTES or monotonic() > deadline:
                        raise ValueError('下载音源超过体积或时间上限')
                    output.write(block)
            return
    raise ValueError('音源下载地址重定向次数过多')


def find_original(music, query, progress):
    title, artist = split_title(query)
    if not title:
        raise ValueError('请提供歌曲名称')
    progress('正在电脑上找歌曲音源')
    local = local_source(music, title, artist)
    if local:
        return store_audio(music, local)
    progress('正在网上搜索可用于翻唱的音源')

    with requests.Session() as session:
        session.headers['User-Agent'] = 'SuperGuga/1.0 (personal cover player)'
        try:
            response = session.get(CATALOG, params={'q': title, 'category': 'music', 'page_size': 20}, timeout=(5, 15))
            response.raise_for_status()
            results = response.json().get('results', [])
        except (requests.RequestException, ValueError) as exc:
            raise ValueError('在线音源搜索暂时连不上。可以稍后重试，或导入本地歌曲。') from exc
        item = select_recording(results, title, artist)
        progress('找到了歌曲，正在下载音源')
        suffix = Path(urlparse(item['url']).path).suffix.lower()
        music.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix='guga-source-', dir=music) as folder:
            target = Path(folder) / ('recording' + (suffix if suffix in AUDIO_EXTENSIONS else '.mp3'))
            try:
                download_recording(session, item['url'], target)
                return store_audio(music, target, metadata=dict(
                    display_name=title, creator=item['creator'], provider=item.get('provider'),
                    source_url=item.get('foreign_landing_url') or item['url'],
                    license=item['license'], license_url=item.get('license_url'),
                    duration_seconds=item['duration'] / 1000,
                ))
            except (requests.RequestException, OSError) as exc:
                raise ValueError('找到了歌曲，但没能下载或读取完整音源。可以稍后重试，或导入本地歌曲。') from exc
