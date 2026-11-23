"""Shared song library and the audio files used to produce covers."""
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from unicodedata import normalize

from filelock import FileLock

MUSIC = Path(__file__).resolve().parents[1] / 'runtime/music'
AUDIO_EXTENSIONS = {'.wav', '.flac', '.mp3', '.m4a', '.ogg', '.opus', '.aac'}


def song_key(value):
    return ' '.join(normalize('NFKC', str(value)).split()).strip(' 《》').casefold()


def split_title(query):
    named = re.fullmatch(r'(.+?)的《([^《》]+)》', query.strip())
    if named:
        return named[2].strip(), named[1].strip()
    title, _, artist = re.sub(r'》\s*[—–-]\s*', '》 - ', query).strip().partition(' - ')
    return title.strip(' 《》'), artist.strip()


def canonical_title(query):
    title, artist = split_title(query)
    return f'{title} - {artist}' if artist else title


def read_library(music):
    path = music / 'library.json'
    try:
        library = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    except (OSError, ValueError):
        raise ValueError('歌曲库无法读取，请检查 runtime/music/library.json') from None
    if not isinstance(library, dict):
        raise ValueError('歌曲库格式不正确')
    return library


def library_title(library, title):
    if title in library:
        return title
    key = song_key(canonical_title(title))
    return next((name for name in library if song_key(canonical_title(name)) == key), title)


def is_cover(entry):
    return (
        isinstance(entry, dict)
        and entry.get('source_kind', 'guga_cover') == 'guga_cover'
        and entry.get('verified_full_length') is True
        and isinstance(entry.get('pipeline_version'), int)
        and entry['pipeline_version'] >= 2
    )


def write_song(music, title, entry, *, expected):
    """Merge a single change and fail if the same song was updated meanwhile."""
    music.mkdir(parents=True, exist_ok=True)
    with FileLock(str(music / 'library.lock'), timeout=5):
        library = read_library(music)
        title = library_title(library, title)
        if library.get(title) != expected:
            raise ValueError('制作期间该歌曲已更新，请重新点歌')
        library[title] = entry
        fd, name = tempfile.mkstemp(prefix='library-', suffix='.pending', dir=music)
        temporary = Path(name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(library, output, ensure_ascii=False, indent=2)
            os.replace(temporary, music / 'library.json')
        finally:
            temporary.unlink(missing_ok=True)


def store_audio(music, source, *, metadata=None):
    import av

    source = Path(source)
    if not source.is_file():
        raise ValueError('选择的音频文件不存在')
    with av.open(str(source)) as container:
        if not container.streams.audio:
            raise ValueError('文件里没有音轨')
        stream = container.streams.audio[0]
        duration = float(stream.duration * stream.time_base) if stream.duration else (container.duration or 0) / av.time_base
        if duration <= 0 or next(container.decode(audio=0), None) is None:
            raise ValueError('音频无法解码或时长无效')
    if metadata and metadata.get('duration_seconds'):
        if abs(duration - metadata['duration_seconds']) > 3:
            raise ValueError('下载的录音时长与曲库不符，可能是片段')

    with source.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    folder = music / 'local'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (digest[:20] + source.suffix.lower())
    if not target.exists():
        fd, name = tempfile.mkstemp(prefix='import-', suffix='.pending', dir=folder)
        os.close(fd)
        temporary = Path(name)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return dict(
        metadata or {},
        path=target.relative_to(music).as_posix(), source_kind='local_audio',
        verified_audio=True, duration_seconds=round(duration, 3), sha256=digest,
        display_name=(metadata or {}).get('display_name', source.stem),
        voice='original-file', guga_cover=False,
    )
