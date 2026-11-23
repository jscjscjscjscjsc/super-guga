"""Preview/export the exact boot renderer without starting the pet or any AI tools."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', type=float, help='Render a selected second as PNG')
    parser.add_argument('--record', action='store_true', help='Export the full sequence as MP4')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if (args.snapshot is not None or args.record) and args.output is None:
        parser.error('--output is required for exports')
    from launcher import _prepare_qt
    _prepare_qt()
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtGui import QImage
    from ui.startup_splash import DURATION, GugaStartupSplash
    app = QApplication.instance() or QApplication([])
    splash = GugaStartupSplash(ROOT / 'resources')
    if args.snapshot is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if not splash.render_frame(args.snapshot).save(str(args.output)):
            raise RuntimeError('Could not save startup preview')
        return 0
    if args.record:
        import av
        import numpy as np
        import wave
        from PIL import Image
        from ui.startup_splash import SOUND_FILE
        args.output.parent.mkdir(parents=True, exist_ok=True)
        fps, width, height = 24, 960, 600
        with av.open(str(args.output), 'w') as output:
            stream = output.add_stream('libx264', rate=fps)
            stream.width, stream.height, stream.pix_fmt = width, height, 'yuv420p'
            stream.options = {'crf':'19', 'preset':'medium'}
            sound = ROOT / 'resources' / 'startup' / SOUND_FILE
            audio = output.add_stream('aac', rate=48000) if sound.is_file() else None
            if audio is not None:
                audio.layout = 'stereo'
            for index in range(round(DURATION * fps)):
                image = splash.render_frame(index/fps, width, height, fade_out=True).convertToFormat(QImage.Format_RGBA8888)
                data = image.bits()
                data.setsize(image.byteCount())
                frame = av.VideoFrame.from_image(Image.frombytes('RGBA', (width,height), bytes(data)))
                for packet in stream.encode(frame):
                    output.mux(packet)
            for packet in stream.encode():
                output.mux(packet)
            if audio is not None:
                with wave.open(str(sound), 'rb') as cues:
                    while samples := cues.readframes(1024):
                        stereo = np.frombuffer(samples, dtype='<i2').reshape(-1, 2)
                        frame = av.AudioFrame.from_ndarray(stereo.reshape(1, -1), format='s16', layout='stereo')
                        frame.sample_rate = cues.getframerate()
                        for packet in audio.encode(frame):
                            output.mux(packet)
                for packet in audio.encode():
                    output.mux(packet)
        return 0
    splash.finished.connect(app.quit)
    splash.play()
    return app.exec_()


if __name__ == '__main__':
    raise SystemExit(main())
