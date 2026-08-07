"""娱乐互动插件：成语接龙、知识问答、排行榜。"""

import random

from plugins import _db
from plugins import _shared

NAME = "娱乐互动"
HELP = (
    "/成语 —— 成语接龙｜/答题 —— 知识问答｜/排名 —— 排行榜"
    "（游戏中发「结束」可退出）"
)

_games = {}

IDIOMS = set(
    """一心一意 一帆风顺 一马当先 一见如故 一鸣惊人 一诺千金 一石二鸟 一言九鼎
    一叶知秋 一针见血 二话不说 二龙戏珠 三心二意 三顾茅庐 三言两语 四通八达
    四面八方 五花八门 五光十色 六神无主 六亲不认 七上八下 七嘴八舌 八面玲珑
    九牛一毛 九死一生 十全十美 十拿九稳 百发百中 百花齐放 百折不挠 千军万马
    千钧一发 千变万化 万无一失 万众一心 马到成功 龙飞凤舞 凤毛麟角 虎头蛇尾
    龙争虎斗 画龙点睛 卧虎藏龙 调虎离山 如虎添翼 兔死狐悲 守株待兔 龙马精神
    蛇蝎心肠 画蛇添足 杯弓蛇影 马马虎虎 老马识途 悬崖勒马 亡羊补牢 顺手牵羊
    鸡犬不宁 闻鸡起舞 鹤立鸡群 鸡飞蛋打 狗急跳墙 狼心狗肺 呆若木鸡
    牛刀小试 对牛弹琴 力大如牛 鼠目寸光 胆小如鼠 鱼目混珠
    如鱼得水 浑水摸鱼 漏网之鱼 鸟语花香 惊弓之鸟 笨鸟先飞 一箭双雕 鹏程万里
    金蝉脱壳 螳螂捕蝉 蛛丝马迹 飞蛾扑火 破茧成蝶 蜂拥而至
    心花怒放 心旷神怡 心惊胆战 心平气和 心直口快 心灵手巧 心心相印 心想事成
    眉开眼笑 眉飞色舞 愁眉苦脸 目瞪口呆 手舞足蹈 眼高手低
    脚踏实地 头头是道 唇枪舌剑 口是心非 笑里藏刀 泪流满面
    热火朝天 水深火热 火冒三丈 火上浇油 水落石出 山清水秀 山穷水尽 跋山涉水
    风调雨顺 风和日丽 风雨同舟 风平浪静 雷厉风行 电闪雷鸣 风驰电掣
    天长地久 天翻地覆 天罗地网 天衣无缝 天南地北 开天辟地 惊天动地 顶天立地
    大材小用 大同小异 小题大做 大惊小怪 大快人心 大显身手 得心应手
    出人头地 名列前茅 捷足先登 后来居上 先发制人 进退两难 左右为难
    安居乐业 安分守己 安然无恙 见义勇为 助人为乐 舍己为人
    拔苗助长 叶公好龙 自相矛盾 刻舟求剑 画饼充饥 望梅止渴 掩耳盗铃 滥竽充数
    井底之蛙 狐假虎威 黔驴技穷 东施效颦 邯郸学步
    人山人海 海阔天空 空前绝后 后来居上 上行下效 笑逐颜开 开诚布公 公而忘私
    私心杂念 念念不忘 忘恩负义 义不容辞 辞旧迎新 新仇旧恨 恨之入骨 骨肉相连
    连绵不断 断章取义 义薄云天 天经地义 义正词严 严阵以待 中流砥柱 坚定不移
    定国安邦 光明正大 大义凛然 谈笑风生 生龙活虎 虎视眈眈 龙腾虎跃 跃跃欲试
    势不可挡 挡风遮雨 雨后春笋 损人利己 己所不欲 欲擒故纵 纵横交错 错落有致
    至关重要 重于泰山 山明水秀 秀外慧中 中庸之道 道听途说 说三道四 方兴未艾
    人杰地灵 灵机一动 动人心弦 弦外之音 音容笑貌 貌合神离 离经叛道 道貌岸然
    燃眉之急 急中生智 智勇双全 全力以赴 赴汤蹈火 火上加油 油嘴滑舌 舌战群儒
    流连忘返 返璞归真 真才实学 学富五车 车载斗量 量力而行 行云流水 水到渠成
    成千上万 万众瞩目 目不转睛 精明强干 干柴烈火 火烧眉毛 毛遂自荐 见多识广
    广开言路 路不拾遗 遗臭万年 年富力强 强词夺理 理直气壮 壮志凌云 云开日出
    出类拔萃 堂堂正正 明察秋毫 毫发无伤 伤天害理 理屈词穷
    穷途末路 路见不平 平步青云 云淡风轻 轻描淡写 神采飞扬 扬眉吐气
    气壮山河 河清海晏 若无其事 事与愿违 违心之论 论功行赏 赏心悦目
    目空一切 切中要害 害群之马 马不停蹄 寻根究底 知难而进 进退维谷
    谷贱伤农 节外生枝 枝繁叶茂 茂林修竹 竹报平安 安居乐业
    高山流水 地大物博 博古通今 今非昔比 比翼双飞 飞黄腾达 达官贵人
    人浮于事 事在人为 为人师表 表里如一 一尘不染 口若悬河
    患难与共 共襄盛举 举世闻名 名不虚传 传道授业 业精于勤 勤能补拙
    舌灿莲花 花好月圆 枕戈待旦 旦夕祸福 福星高照 照本宣科 科班出身
    身经百战 战无不胜 胜券在握 握手言和 和盘托出 出言不逊 逊志时敏
    敏而好学 学无止境 境由心生 生不逢时 时来运转 转弯抹角
    原封不动 动辄得咎 咎由自取 取长补短 短兵相接 接二连三
    短小精悍 悍然不顾 顾全大局 局促不安 业广惟勤
    学海无涯 妄自菲薄 薄物细故 故步自封 封官许愿 愿者上钩
    """.split()
)


def load_idioms():
    """内置词库 + data/idioms.txt 外部词库合并（外部每行一个成语）。"""
    base = set(IDIOMS)
    extra_file = _shared.DATA_DIR / "idioms.txt"
    try:
        if extra_file.exists():
            for line in extra_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                word = line.strip()
                if len(word) == 4:
                    base.add(word)
    except Exception as e:
        print(f"加载外部成语词库失败：{e}")
    return base


IDIOMS = load_idioms()


def _build_index():
    """预计算接龙索引：首字 → 成语列表、尾字 → 可接数量（O(1) 查表）。"""
    by_first, follow = {}, {}
    for w in IDIOMS:
        by_first.setdefault(w[0], []).append(w)
    for w in IDIOMS:
        follow[w] = sum(1 for x in by_first.get(w[-1], []) if x != w)
    return by_first, follow


_BY_FIRST, _FOLLOW = _build_index()

QUIZ = [
    {"q": "《BanG Dream!》中梦限大MewType的DJ担当是谁？", "options": ["A. 仲町阿拉蕾", "B. 千石由乃", "C. 藤都子", "D. 宫永野乃花"], "answer": "B"},
    {"q": "千石由乃的生日是哪一天？", "options": ["A. 1月4日", "B. 4月1日", "C. 11月4日", "D. 4月11日"], "answer": "C"},
    {"q": "千石由乃的代表色是？", "options": ["A. #EE5577", "B. #9977CC", "C. #55AA77", "D. #FFCC00"], "answer": "A"},
    {"q": "由乃喜欢喝的饮料是？", "options": ["A. 咖啡", "B. 能量饮料", "C. 牛奶", "D. 果汁"], "answer": "B"},
    {"q": "由乃最喜欢吃的零食是？", "options": ["A. 白巧克力", "B. 薯片", "C. 饼干", "D. 棉花糖"], "answer": "A"},
    {"q": "《BanG Dream!》中「Poppin'Party」的主唱是？", "options": ["A. 花园多惠", "B. 户山香澄", "C. 山吹沙绫", "D. 市谷有咲"], "answer": "B"},
    {"q": "成语「画龙点睛」中的「睛」指的是？", "options": ["A. 眼睛", "B. 镜子", "C. 精彩", "D. 精神"], "answer": "A"},
    {"q": "「亡羊补牢」告诉我们？", "options": ["A. 出了差错及时补救", "B. 不要养羊", "C. 牢房要结实", "D. 羊会逃跑"], "answer": "A"},
    {"q": "中国的首都是？", "options": ["A. 上海", "B. 广州", "C. 北京", "D. 深圳"], "answer": "C"},
    {"q": "太阳从哪个方向升起？", "options": ["A. 东方", "B. 西方", "C. 北方", "D. 南方"], "answer": "A"},
    {"q": "一年有多少个月？", "options": ["A. 10", "B. 11", "C. 12", "D. 13"], "answer": "C"},
    {"q": "水的化学式是？", "options": ["A. CO2", "B. H2O", "C. O2", "D. NaCl"], "answer": "B"},
    {"q": "「床前明月光」的下一句是？", "options": ["A. 低头思故乡", "B. 疑是地上霜", "C. 举头望明月", "D. 春风又绿江南岸"], "answer": "B"},
    {"q": "我国的国宝动物是？", "options": ["A. 老虎", "B. 熊猫", "C. 狮子", "D. 大象"], "answer": "B"},
    {"q": "彩虹通常有几种颜色？", "options": ["A. 五种", "B. 六种", "C. 七种", "D. 八种"], "answer": "C"},
    {"q": "「千钧一发」中的「钧」是古代什么单位？", "options": ["A. 长度", "B. 重量", "C. 时间", "D. 面积"], "answer": "B"},
    {"q": "由乃喜欢玩的卡牌游戏是？", "options": ["A. 炉石传说", "B. 卡片战斗先导者", "C. 三国杀", "D. 游戏王"], "answer": "B"},
    {"q": "「梦限大MewType」的名字寓意是？", "options": ["A. 梦想无限大", "B. 梦醒了", "C. 梦境限定", "D. 梦幻组合"], "answer": "A"},
    {"q": "一天有多少小时？", "options": ["A. 12", "B. 20", "C. 24", "D. 48"], "answer": "C"},
    {"q": "「一心一意」的意思是？", "options": ["A. 三心二意", "B. 专一用心", "C. 贪得无厌", "D. 马马虎虎"], "answer": "B"},
]


def add_score(ukey, game):
    _db.score_add(str(ukey), game)


def leaderboard():
    scores = _db.scores_all()
    rows = sorted(
        scores.items(),
        key=lambda kv: kv[1].get("idiom", 0) + kv[1].get("quiz", 0),
        reverse=True,
    )[:10]
    if not rows:
        return "还没有排名记录，来玩成语接龙或答题吧～"
    return "\n".join(
        f"{i + 1}. {_display_name(k)}　成语 {s.get('idiom', 0)}　答题 {s.get('quiz', 0)}"
        for i, (k, s) in enumerate(rows)
    )


def _mask_key(key) -> str:
    k = str(key)
    return (k[:6] + "…") if len(k) > 8 else k


def _display_name(key) -> str:
    """排行榜显示名：优先用昵称，没有则打码 ID。"""
    nick = _shared.nickname_of(key)
    return nick if nick else _mask_key(key)


def start_idiom(ckey):
    # 只选「尾字能接得上」的成语开局，避免开局即死
    first = random.choice([i for i in IDIOMS if _FOLLOW[i] > 0])
    _games[ckey] = {"type": "idiom", "active": True, "last_char": first[-1]}
    return f"成语接龙开始！我先来：「{first}」，请接「{first[-1]}」字开头的成语～"


def _best_bot_word(word):
    """选一个「后续仍有活路」且接得最多的成语；无活路则认输。"""
    candidates = [w for w in _BY_FIRST.get(word[-1], []) if w != word]
    alive = [w for w in candidates if _FOLLOW[w] > 0]
    if not alive:
        return None
    return max(alive, key=lambda w: _FOLLOW[w])


def idiom_guess(ckey, ukey, text):
    game = _games.get(ckey)
    if not game or game.get("type") != "idiom" or not game.get("active"):
        return None
    word = text.strip()
    if word in ("结束", "认输", "退出"):
        game["active"] = False
        return "本局结束，下次再战～"
    if len(word) != 4 or word not in IDIOMS:
        return f"「{word[:8]}」不在词库里，换个四字成语试试（发「结束」可退出）"
    if game["last_char"] and word[0] != game["last_char"]:
        return f"要接「{game['last_char']}」开头的成语哦（发「结束」可退出）"
    add_score(ukey, "idiom")
    bot_word = _best_bot_word(word)
    if not bot_word:
        game["active"] = False
        return f"「{word}」我接不上了，这局你赢！(+1)"
    game["last_char"] = bot_word[-1]
    return f"「{word}」(+1) 我接：「{bot_word}」"


def start_quiz(ckey):
    _games[ckey] = {"type": "quiz", "active": True, "qindex": 0}
    return quiz_question(ckey)


def quiz_question(ckey):
    game = _games[ckey]
    q = QUIZ[game["qindex"]]
    game["answer"] = q["answer"]
    return f"第 {game['qindex'] + 1} 题：{q['q']}\n" + "\n".join(q["options"])


def quiz_guess(ckey, ukey, text):
    game = _games.get(ckey)
    if not game or game.get("type") != "quiz" or not game.get("active"):
        return None
    ans = text.strip().upper()
    if ans in ("结束", "退出", "停止"):
        game["active"] = False
        return "答题结束，感谢参与！"
    correct = game.get("answer", "")
    letter = correct[:1] if correct else ""
    if ans and (ans == correct or (letter and ans[0] == letter)):
        add_score(ukey, "quiz")
        game["qindex"] += 1
        if game["qindex"] >= len(QUIZ):
            game["active"] = False
            return "全部答对，你是题库终结者！(+1)"
        return "答对啦！(+1) 下一题：\n" + quiz_question(ckey)
    return "不对哦，再想想～（答不出就发「结束」）"


def cmd_idiom(text, ctx):
    rest = text[len("/成语"):].strip()
    if rest in ("结束", "停止", "退出"):
        _games[ctx.chat_key] = {"type": "idiom", "active": False}
        return "成语接龙已结束。"
    return start_idiom(ctx.chat_key)


def cmd_quiz(text, ctx):
    rest = text[len("/答题"):].strip()
    if rest in ("结束", "停止", "退出"):
        _games[ctx.chat_key] = {"type": "quiz", "active": False}
        return "答题已结束。"
    return start_quiz(ctx.chat_key)


def cmd_rank(text, ctx):
    return leaderboard()


COMMANDS = {
    "/成语": cmd_idiom,
    "/答题": cmd_quiz,
    "/排名": cmd_rank,
}


def game_try(ctx, text):
    """非指令消息先交给游戏作答。"""
    game = _games.get(ctx.chat_key)
    if not game or not game.get("active"):
        return None
    if game.get("type") == "idiom":
        return idiom_guess(ctx.chat_key, ctx.user_key, text)
    return quiz_guess(ctx.chat_key, ctx.user_key, text)
