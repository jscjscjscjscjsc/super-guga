"""Separate, convert and mix a song on this computer, then register the cover."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import wave


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.song_library import MUSIC, library_title, read_library, write_song
from core.guga_voice import PYTHON, reference_audio

MIX_FILTER = (
    '[0:a]highpass=f=82,acompressor=threshold=.08:ratio=2.2:attack=8:release=135:makeup=2.2,'
    'aformat=channel_layouts=stereo[v];'
    '[1:a]volume=-12dB,aformat=channel_layouts=stereo[b];'
    '[v][b]amix=inputs=2:duration=shortest:normalize=0,'
    'alimiter=limit=.92:level=false:latency=true[m]'
)


def run(*arguments, timeout):
    subprocess.run([str(argument) for argument in arguments], cwd=ROOT,
                   check=True, timeout=timeout)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stems_for(source, music):
    result = subprocess.run(
        [str(PYTHON), str(ROOT/'tools/separate_music.py'), str(source), str(music)],
        cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=1200,
    )
    for line in reversed(result.stdout.splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if value.get('ok'):
            return Path(value['vocals']), Path(value['instrumental'])
        raise RuntimeError('分离人声失败：' + str(value.get('message','')))
    raise RuntimeError('分离人声未返回结果：' + result.stderr[-600:])


def wav_length(path):
    with wave.open(str(path), 'rb') as audio:
        return audio.getnframes() / audio.getframerate()


def render(title, octave, steps, preview, start=0., duration=None, *, discover=False, music=MUSIC, progress=print):
    library = read_library(music)
    title = library_title(library, title)
    existing = library.get(title)
    reference = reference_audio()
    if not PYTHON.is_file() or not reference.is_file():
        raise ValueError('缺少专属声线或本地翻唱环境')
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        raise RuntimeError('缺少 FFmpeg')
    original = existing.get('original', existing) if isinstance(existing, dict) else {}
    source = music / original.get('path', '')
    if original.get('source_kind') != 'local_audio' or not source.is_file():
        if not discover:
            raise ValueError(f'尚未导入《{title}》的完整音源')
        from core.song_sources import find_original

        original = find_original(music, title, progress)
        source = music / original['path']
    progress('正在分离主唱和伴奏')
    vocals, instrumental = stems_for(source, music)
    source_seconds = wav_length(instrumental)
    if abs(source_seconds - float(original.get('duration_seconds', source_seconds))) > .6:
        raise RuntimeError('伴奏时长与导入的原曲不匹配')
    progress('正在用咕嘎的音色演唱')
    with tempfile.TemporaryDirectory(prefix='guga-mix-', dir=music) as folder:
        converted = Path(folder) / 'vocals.wav'
        command = [
            PYTHON, '-u', ROOT/'tools/guga_cover_worker.py', vocals, converted,
            '--steps', steps, '--octave', octave,
        ]
        if preview:
            command += ['--start', start, '--duration', duration]
        run(*command, timeout=5400)
        if abs(wav_length(converted) - (duration if preview else source_seconds)) > .45:
            raise RuntimeError('转换人声时长与伴奏不匹配')
        progress('正在混合咕嘎人声和伴奏')
        pending = Path(folder) / 'mix.wav'
        seek = ['-ss', str(start), '-t', str(duration)] if preview else []
        run(ffmpeg, '-v', 'error', '-y', '-i', converted, *seek, '-i', instrumental,
            '-filter_complex', MIX_FILTER,
            '-map', '[m]', '-ar', 44100, '-ac', 2, '-c:a', 'pcm_s16le', pending, timeout=120)
        mixed_seconds = wav_length(pending)
        if abs(mixed_seconds - (duration if preview else source_seconds)) > .45:
            raise RuntimeError('混音长度不匹配')
        if preview:
            preview.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(pending,preview)
            return preview
        covers = music / 'covers'
        covers.mkdir(exist_ok=True)
        target = covers / ('guga-' + sha256(pending)[:16] + '.wav')
        os.replace(pending, target)
    entry = dict(
        path=target.relative_to(music).as_posix(), provider='Seed-VC local',
        source_kind='guga_cover', voice='guga-original-v1',
        reference_sha256=sha256(reference),
        verified_full_length=True, pipeline_version=2, separated_vocals=True,
        duration_seconds=round(mixed_seconds, 3), octave=octave, steps=steps,
        original=original,
    )
    write_song(music, title, entry, expected=existing)
    return target


def report(**event):
    print('GUGA_SONG ' + json.dumps(event, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--title', default='晴天')
    parser.add_argument('--octave', type=int, choices=(-1, 0, 1), default=0)
    parser.add_argument('--steps', type=int, default=32)
    parser.add_argument('--preview', type=Path)
    parser.add_argument('--start', type=float, default=58.)
    parser.add_argument('--duration', type=float, default=24.)
    parser.add_argument('--discover', action='store_true')
    parser.add_argument('--music-root', type=Path, default=MUSIC)
    args = parser.parse_args()
    if not args.discover:
        print(render(args.title, args.octave, args.steps, args.preview, args.start, args.duration, music=args.music_root), flush=True)
    else:
        try:
            path = render(args.title, args.octave, args.steps, args.preview, args.start, args.duration,
                          discover=True, music=args.music_root, progress=lambda message: report(message=message))
        except (ValueError, RuntimeError) as exc:
            report(ok=False, error=str(exc))
            sys.exit(1)
        except Exception as exc:
            report(ok=False, error='翻唱制作失败（' + type(exc).__name__ + '）')
            raise
        report(ok=True, path=str(path))
