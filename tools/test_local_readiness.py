"""Core regressions for portable voice, honest music import and invalid model settings."""
import os,json,tempfile,time,wave,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from launcher import _prepare_qt
_prepare_qt()
from PyQt5.QtWidgets import QApplication
APP=QApplication.instance() or QApplication([])
from core import asr
from core.model_settings import configuration_error,dsh_driver
from core.music_performance import import_local_song,MusicPerformance


class ReadinessTests(unittest.TestCase):
    def test_template_configuration_is_not_ready(self):
        for key in ('YOUR_API_KEY','your-api-key',''):
            self.assertTrue(configuration_error(dict(api_key=key,base_url='https://api.deepseek.com',model='deepseek-flash')))
        self.assertTrue(configuration_error(dict(api_key='fixture',base_url='https://your_openai_compatible_endpoint',model='YOUR_CHAT_MODEL_ID')))
        self.assertFalse(configuration_error(dict(api_key='fixture',base_url='https://example.test/v1',model='fixture')))

    def test_realtime_placeholder_fails_before_network(self):
        from core.realtime_voice import connection_url
        with self.assertRaisesRegex(ValueError,'点击录音'):
            connection_url(dict(provider='qianwen',model='qwen-audio-3.1-realtime-plus',api_key='YOUR_REALTIME_API_KEY'))

    def test_voice_context_has_no_unverified_capture_or_raw_fields(self):
        from core.voice_context import voice_context,voice_instructions
        context=voice_context('keyboard',dict(frames_captured=8000,api_key='fixture-secret'))
        self.assertFalse(context['microphone_captured']);self.assertNotIn('api_key',context)
        context=voice_context('local_asr',dict(frames_captured='bad'))
        self.assertTrue(context['transcript_received']);self.assertFalse(context['microphone_captured'])
        instructions=voice_instructions(context)
        self.assertNotIn('本轮录音器已实际采集',instructions)
        self.assertIn('不会持续监听',instructions)
        self.assertIn('语音转写',instructions)

    def test_dsh_desktop_profile_and_malformed_file_have_actionable_results(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'.dsh/profiles/desktop/cordis.patch.yml';p.parent.mkdir(parents=True)
            p.write_text(json.dumps([dict(id='agent-default-model',config=dict(provider='deepseek',model='deepseek-flash'))]))
            self.assertEqual(dsh_driver(folder)['model'],'deepseek-flash')
            p.write_text('[')
            with self.assertRaisesRegex(ValueError,'无法读取'):dsh_driver(folder)

    def test_unicode_model_is_cached_to_ascii_and_incomplete_cache_is_not_used(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'中文模型'
            for name in ('am/final.mdl','conf/model.conf','graph/HCLr.fst'):
                f=source/name;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b'model fixture')
            with patch.dict(os.environ,{'GUGA_VOSK_MODEL':str(source),'LOCALAPPDATA':folder}),patch.object(asr,'_ascii_path',side_effect=lambda x:x):
                result=asr._ensure_model_dir()
                self.assertTrue(result.isascii());self.assertTrue(asr._valid_model(result))
                self.assertNotEqual(Path(result),source)

    def test_stopping_drains_last_callback_blocks_and_finalizes_once(self):
        recorder=asr.ASRRecorder();recorder._recording=True
        rec=Mock();rec.AcceptWaveform.return_value=True;rec.Result.return_value='{"text":"尾字"}';rec.FinalResult.return_value='{"text":"完成"}'
        recorder._recognizer=rec
        class Stream:
            def __enter__(self):
                recorder._q.put(b'last block');recorder._recording=False
            def __exit__(self,*args):pass
        with patch('sounddevice.RawInputStream',return_value=Stream()):recorder._record_loop()
        self.assertEqual(recorder.stop(),'尾字完成');self.assertEqual(recorder.stop(),'尾字完成')
        rec.AcceptWaveform.assert_called_once_with(b'last block');rec.FinalResult.assert_called_once()

    def test_config_load_failure_never_sticks_answer_worker(self):
        from core.chat_answer import AnswerWorker
        worker=AnswerWorker();events=[];worker.completed.connect(events.append)
        with patch('core.model_settings.load_config',side_effect=ValueError('bad configuration')),patch('core.chat_answer.requests.post') as request:
            worker.start('fixture',[],'你好',search_mode='off')
            deadline=time.monotonic()+3
            while worker.active and time.monotonic()<deadline:APP.processEvents();time.sleep(.01)
            APP.processEvents()
            self.assertFalse(worker.active);self.assertFalse(events[0]['ok']);request.assert_not_called()

    def test_local_import_is_portable_and_not_mislabeled_as_cover(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'source.wav';music=root/'music'
            with wave.open(str(source),'wb') as stream:
                stream.setparams((1,2,16000,0,'NONE','not compressed'));stream.writeframes(b'\0\0'*16000)
            with patch('core.music_performance.MUSIC',music):
                entry=import_local_song(source,'晴天');source.unlink()
                self.assertTrue((music/entry['path']).is_file());self.assertFalse(entry['guga_cover'])
                performance=MusicPerformance()
                with patch.object(performance.player,'play') as play,patch.object(performance,'prepare',return_value=dict(ok=True,execution_pending=True)) as prepare:
                    result=performance.sing('晴天')
                self.assertTrue(result['execution_pending'])
                prepare.assert_called_once_with('晴天')
                play.assert_not_called()

    def test_corrupted_library_is_reported_without_crashing(self):
        with tempfile.TemporaryDirectory() as folder,patch('core.music_performance.MUSIC',Path(folder)):
            (Path(folder)/'library.json').write_text('{bad')
            result=MusicPerformance().sing('晴天')
            self.assertFalse(result['ok']);self.assertIn('歌曲库',result['error'])


if __name__=='__main__':unittest.main()
