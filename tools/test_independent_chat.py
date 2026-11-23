"""Independent model, plain-language prompting and read-only memory regressions."""
import json,os,threading,time,unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock
from tools import test_personal_harness as harness
from core.companion_dialogue import needs_web_search
from core import chat_memory


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        environment=patch.dict(os.environ,{'GUGA_LINGSHU':'on'})
        environment.start();self.addCleanup(environment.stop)
        chat_memory._CACHE.clear();chat_memory._INFLIGHT=None

    def test_cache_is_query_specific_and_other_member_does_not_receive_shared_memory(self):
        with patch.object(chat_memory,'_read',side_effect=lambda q:dict(state='ok',items=[q])):
            self.assertEqual(chat_memory.recall_context('fixture a')['items'],['fixture a'])
            self.assertEqual(chat_memory.recall_context('fixture b')['items'],['fixture b'])
            self.assertEqual(chat_memory.recall_context('fixture a','003')['items'],[])

    def test_slow_memory_does_not_block_reply_or_supply_unrelated_cached_text(self):
        release=threading.Event()
        def read(q):release.wait(1);return dict(state='ok',items=['fixture'])
        with patch.object(chat_memory,'_read',side_effect=read):
            try:
                started=time.monotonic();r=chat_memory.recall_context('slow fixture',wait_seconds=.01)
                self.assertEqual(r['state'],'pending');self.assertLess(time.monotonic()-started,.3)
                self.assertEqual(chat_memory.recall_context('different fixture')['items'],[])
            finally:
                release.set();chat_memory._INFLIGHT[1].wait(1) if chat_memory._INFLIGHT else None

    def test_memory_failure_does_not_look_like_success(self):
        with patch.object(chat_memory,'_read',side_effect=RuntimeError('offline')):
            self.assertEqual(chat_memory.recall_context('failed fixture')['state'],'unavailable')

    def test_ordinary_questions_do_not_require_web_but_current_information_does(self):
        self.assertFalse(needs_web_search('解释一下什么叫大模型，用生活里的例子'))
        self.assertFalse(needs_web_search('你能听见我说话吗'))
        self.assertTrue(needs_web_search('查一下今天的天气'))


class IndependentChatTests(unittest.TestCase):
    setUpClass=harness.IntegrationTests.__dict__['setUpClass']
    tearDownClass=harness.IntegrationTests.__dict__['tearDownClass']
    setUp=harness.IntegrationTests.setUp
    pump=harness.IntegrationTests.pump

    def test_inline_text_uses_companion_chat_instead_of_old_game_model(self):
        with patch.object(self.panel,'send',return_value=True) as send,patch.object(self.pet._ai,'ask') as legacy:
            self.pet._ask_with_ctx('给我讲个通俗的比喻')
            send.assert_called_once_with('给我讲个通俗的比喻');legacy.assert_not_called()

    def test_real_chat_dispatch_includes_prompt_and_memories_without_dsh(self):
        p=self.panel;captured=[]
        response=Mock(status_code=200)
        response.iter_lines.return_value=[('data: '+json.dumps({'choices':[{'delta':{'content':'大模型像个读过很多书的助手。'}}]},ensure_ascii=False)).encode(),'data: [DONE]']
        def post(*args,**kwargs):captured.append(kwargs['json']);return response
        cfg=dict(api_key='fixture',base_url='https://example.test/v1',model='fixture-model')
        p.send('记住：我喜欢大白话解释')
        try:
            with patch('core.model_settings.load_config',return_value=cfg),patch('core.chat_memory.recall_context',return_value=dict(state='ok',items=['共享工作结论 fixture-memory'])),patch('core.chat_answer.requests.post',side_effect=post),patch.object(p.runtime.dsh,'start') as dsh:
                self.assertTrue(p.send('解释大模型是什么'))
                self.pump(lambda:not p.busy)
                dsh.assert_not_called()
            system=captured[0]['messages'][0]['content']
            self.assertIn('大白话',system);self.assertIn('用户长期信息',system)
            self.assertIn('fixture-memory',system)
            self.assertEqual(p.store.messages(p.active_id)[-1]['content'],'大模型像个读过很多书的助手。')
            self.assertEqual(p.answer.last_receipt['state'],'completed')
        finally:p.busy=False;p._run_kind=None

    def test_local_transcript_reaches_model_with_capture_facts_and_next_text_resets(self):
        p=self.panel;captured=[]
        response=Mock(status_code=200)
        response.iter_lines.side_effect=lambda **kwargs:iter([
            'data: '+json.dumps({'choices':[{'delta':{'content':'收到这句语音了。'}}]},ensure_ascii=False),'data: [DONE]'])
        cfg=dict(api_key='fixture',base_url='https://example.test/v1',model='fixture-model')
        p.store.append(p.active_id,'assistant','我没有麦克风权限，只能看到打字。')
        p.short_voice.last_capture=dict(frames_captured=8000)
        try:
            with patch('core.model_settings.load_config',return_value=cfg),patch('core.chat_answer.requests.post',side_effect=lambda *a,**kw:(captured.append(kw['json']) or response)):
                p._short_transcript('你怎么知道我刚才说了什么')
                self.pump(lambda:not p.busy)
                self.assertEqual(p.answer.last_receipt['input_source'],'local_asr')
                self.assertIn('你怎么知道我刚才说了什么',p.pet._agent_bubble.recognized.text())
                self.assertFalse(p.pet._agent_bubble.recognized.isHidden())
                self.assertTrue(p.send('那文字呢'))
                self.pump(lambda:not p.busy)
                self.assertTrue(p.pet._agent_bubble.recognized.isHidden())
            first=captured[0]['messages'][-2]['content'];second=captured[1]['messages'][-2]['content']
            self.assertIn('本条用户消息来自点击录音',first);self.assertIn('实际采集到麦克风音频帧',first)
            self.assertIn('历史回答中的能力或权限判断可能有误',first)
            self.assertIn('本条是文字输入',second);self.assertNotIn('本条用户消息来自点击录音',second)
            self.assertEqual(p.store.messages(p.active_id)[-2]['content'],'那文字呢')
        finally:p.busy=False;p._run_kind=None

    def test_recorded_microphone_check_answers_locally_and_preserves_history(self):
        from core.voice_context import MICROPHONE_CHECK_REPLY, is_microphone_check, voice_context
        p=self.panel
        text='可以听到了称呼吗'
        self.assertFalse(is_microphone_check(text,voice_context('local_asr')))
        self.assertFalse(is_microphone_check(text,voice_context('keyboard',dict(frames_captured=8000))))
        captured=voice_context('local_asr',dict(frames_captured=8000))
        for phrase in ('你能听到我说话吗？','咕嘎，可以听到我的声音吗？',text):
            self.assertTrue(is_microphone_check(phrase,captured))
        self.assertFalse(is_microphone_check('你能听到我说话吗，帮我打开微信',captured))
        with patch.object(p.answer,'start') as answer,patch.object(p.dialogue,'start') as dialogue,patch.object(p.pet._tts,'speak') as speak,patch.object(p.pet._tts,'_silent',False):
            p.short_voice.last_capture=dict(frames_captured=8000)
            p._short_transcript(text)
            answer.assert_not_called();dialogue.assert_not_called()
            speak.assert_called_once_with(MICROPHONE_CHECK_REPLY,voice_profile=None)
            self.assertEqual(p.pet._agent_bubble.full_text,MICROPHONE_CHECK_REPLY)
            self.assertEqual([m['content'] for m in p.store.messages(p.active_id)[-2:]],[text,MICROPHONE_CHECK_REPLY])

    def test_recorded_question_during_background_task_keeps_voice_source(self):
        p=self.panel;p.busy=True;p._run_kind='dsh'
        try:
            with patch.object(p.dialogue,'start') as dialogue:
                self.assertTrue(p.send('给我讲个笑话',read_reply=True,input_source='local_asr',voice_receipt=dict(frames_captured=4000)))
                self.assertEqual(dialogue.call_args.kwargs['voice_context']['input_source'],'local_asr')
                self.assertTrue(p.busy)
        finally:p._dialogue_context=None;p.busy=False;p._run_kind=None


if __name__=='__main__':unittest.main()
