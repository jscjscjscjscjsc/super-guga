"""Trusted, per-turn voice facts. No audio, credentials or transcript is stored here."""
import re


_MICROPHONE_CHECK = re.compile(
    r'(?:小?咕嘎)?(?:你)?(?:现在)?(?:还)?(?:能不能|能|可以)?'
    r'听得?[到见](?:我(?:在?说话|讲话|的声音)?|声音)?(?:了)?(?:吗|嘛|么)'
)
MICROPHONE_CHECK_REPLY = '听到啦，本鹅在呢。'


def is_microphone_check(text, context):
    if context.get('input_source') != 'local_asr' or not context.get('microphone_captured'):
        return False
    text = re.sub(r'[\s，,。！!？?]+', '', text)
    # Vosk may transcribe “可以听到我说话吗” as this exact sentence.
    return text == '可以听到了称呼吗' or _MICROPHONE_CHECK.fullmatch(text) is not None


def voice_context(source='keyboard',receipt=None,realtime_connected=False,read_reply=False,muted=False):
    source=source if source in ('keyboard','local_asr','realtime') else 'keyboard'
    receipt=receipt if isinstance(receipt,dict) else {}
    try:frames=max(0,int(receipt.get('frames_captured',0)))
    except (TypeError,ValueError):frames=0
    return dict(input_source=source,local_recording_supported=True,
        transcript_received=source=='local_asr',microphone_captured=source=='local_asr' and frames>0,
        realtime_connected=bool(realtime_connected),read_reply=bool(read_reply),muted=bool(muted))


def voice_instructions(context=None):
    context=context if isinstance(context,dict) else {}
    facts=voice_context(context.get('input_source','keyboard'),
        dict(frames_captured=1 if context.get('microphone_captured') else 0),
        context.get('realtime_connected',False),context.get('read_reply',False),context.get('muted',False))
    text=('\n【宿主核实的本轮语音状态】应用已集成本地麦克风录音、离线中文转写和本地朗读。'
          '语音能力由桌宠程序提供；模型收到转写文字，不需要自己直接访问麦克风。')
    if facts['input_source']=='local_asr':
        text+='本条用户消息来自点击录音后发送的语音转写，已成功取得转写文字；可以理解并回答用户说的话。'
        if facts['read_reply']:
            text+='简短语音问答先直接给结论，日常问题用一到两句自然口语回答；用户明确需要详细解释时再展开。'
        if facts['microphone_captured']:text+='本轮录音器已实际采集到麦克风音频帧。'
        text+='用户问是否听到时，可明确说已收到这句语音转写；不能说用户只是打字、没有录音功能或需要另装语音输入。'
    elif facts['input_source']=='realtime':
        text+='本条来自实时语音通道的转写。'
    else:
        text+='本条是文字输入，尚未收到本条语音，不能声称已经听见本条声音，也不能据此判断麦克风权限；需要验证声音时可请用户点击气泡“点击说话”或 ↑ 录音，再点 ↑ 发送。'
    text+=('实时语音当前已连接。' if facts['realtime_connected'] else '当前没有已连接的持续实时通话；点击录音仍可独立使用，不会持续监听。')
    if facts['muted']:text+='本地朗读当前已静音；这不影响接收录音转写。'
    elif facts['read_reply']:text+='宿主会尝试用本地语音朗读本轮回答；实际发声以播放器状态为准。'
    return text+('没有宿主提供的权限错误，不能声称没有麦克风权限，也不能声称已经持续开麦。'
                 '本地点击录音已经集成，不要让用户另装语音功能或泛称必须先配置麦克风；只有宿主明确报错才排查设置。'
                 '没有原始音频就不判断用户音色、音量或声线。历史回答中的能力或权限判断可能有误，以本轮宿主状态为准。')
