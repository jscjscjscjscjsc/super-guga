"""Bounded by the caller; isolate neural audio dependencies from Qt."""
import os,sys,json,hashlib,logging,shutil,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];MUSIC=ROOT/'runtime/music'
os.environ['PATH']=str(MUSIC/'bin')+os.pathsep+os.environ.get('PATH','')
os.environ.setdefault('OMP_NUM_THREADS','4')

def separate(source, music=MUSIC):
 from audio_separator.separator import Separator
 source=Path(source).resolve()
 if not source.is_file():raise ValueError('Missing source audio')
 roformer='mel_band_roformer_karaoke_aufr33_viperx_sdr_10.1956.ckpt'
 use_roformer=(MUSIC/'models'/roformer).is_file()
 model=roformer if use_roformer else 'UVR_MDXNET_KARA_2.onnx'
 key=f'{source}\0{source.stat().st_size}\0{source.stat().st_mtime_ns}\0{model}'
 ident=hashlib.sha256(key.encode()).hexdigest()[:14]
 folder=music/'stems'/ident;folder.mkdir(parents=True,exist_ok=True)
 manifest=folder/'stems.json'
 if manifest.exists():
  d=json.loads(manifest.read_text(encoding='utf-8'))
  if all(Path(d[k]).is_file() for k in ('vocals','instrumental')):return d
 separator=Separator(output_dir=str(folder),output_format='WAV',model_file_dir=str(MUSIC/'models'),
  log_level=logging.WARNING,normalization_threshold=.99 if use_roformer else .95,use_autocast=use_roformer,
  mdxc_params=dict(segment_size=128,override_model_segment_size=True,batch_size=1,overlap=4,pitch_shift=0),
  mdx_params=dict(hop_length=1024,segment_size=256,overlap=.25,batch_size=1,enable_denoise=True))
 separator.load_model(model_filename=model)
 decoded=folder/'decoded.wav'
 ffmpeg=shutil.which('ffmpeg')
 if not ffmpeg:raise RuntimeError('缺少 FFmpeg')
 subprocess.run([ffmpeg,'-v','error','-y','-i',str(source),'-ar','44100','-ac','2',str(decoded)],check=True,timeout=60,creationflags=0x08000000)
 outputs=separator.separate(str(decoded))
 files=[Path(x) if Path(x).is_absolute() else folder/Path(x).name for x in outputs]
 vocal=next((x for x in files if 'vocal' in x.name.lower()),None)
 instrumental=next((x for x in files if 'instrumental' in x.name.lower()),None)
 if not vocal or not instrumental:raise RuntimeError('Separation did not produce both stems')
 d=dict(vocals=str(vocal),instrumental=str(instrumental),source=str(source),separator=model)
 manifest.write_text(json.dumps(d,ensure_ascii=True,indent=2),encoding='utf-8');return d
if __name__=='__main__':
 try:result=dict(ok=True,**separate(sys.argv[1], Path(sys.argv[2]) if len(sys.argv)>2 else MUSIC))
 except Exception as e:result=dict(ok=False,error=type(e).__name__,message=str(e)[:350])
 print(json.dumps(result,ensure_ascii=True),flush=True)
 sys.exit(0 if result['ok'] else 1)
