"""Localized labels keyed only by stable canonical genre IDs."""

from hakubun import i18n


GENRE_LABELS = {
    'action': {'en': 'Action', 'ja': 'アクション', 'zh-Hans': '动作', 'zh-Hant': '動作', 'es': 'Acción'},
    'adventure': {'en': 'Adventure', 'ja': '冒険', 'zh-Hans': '冒险', 'zh-Hant': '冒險', 'es': 'Aventura'},
    'avant_garde': {'en': 'Avant-Garde', 'ja': '前衛', 'zh-Hans': '前卫', 'zh-Hant': '前衛', 'es': 'Vanguardia'},
    'boys_love': {'en': "Boys' Love", 'ja': 'ボーイズラブ', 'zh-Hans': '耽美', 'zh-Hant': '耽美', 'es': "Boys' Love"},
    'comedy': {'en': 'Comedy', 'ja': 'コメディ', 'zh-Hans': '喜剧', 'zh-Hant': '喜劇', 'es': 'Comedia'},
    'drama': {'en': 'Drama', 'ja': 'ドラマ', 'zh-Hans': '剧情', 'zh-Hant': '劇情', 'es': 'Drama'},
    'ecchi': {'en': 'Ecchi', 'ja': 'お色気', 'zh-Hans': '卖肉', 'zh-Hant': '賣肉', 'es': 'Ecchi'},
    'fantasy': {'en': 'Fantasy', 'ja': 'ファンタジー', 'zh-Hans': '奇幻', 'zh-Hant': '奇幻', 'es': 'Fantasía'},
    'girls_love': {'en': "Girls' Love", 'ja': '百合', 'zh-Hans': '百合', 'zh-Hant': '百合', 'es': "Girls' Love"},
    'gourmet': {'en': 'Gourmet', 'ja': 'グルメ', 'zh-Hans': '美食', 'zh-Hant': '美食', 'es': 'Gastronomía'},
    'horror': {'en': 'Horror', 'ja': 'ホラー', 'zh-Hans': '恐怖', 'zh-Hant': '恐怖', 'es': 'Terror'},
    'mystery': {'en': 'Mystery', 'ja': 'ミステリー', 'zh-Hans': '悬疑', 'zh-Hant': '懸疑', 'es': 'Misterio'},
    'psychological': {'en': 'Psychological', 'ja': '心理', 'zh-Hans': '心理', 'zh-Hant': '心理', 'es': 'Psicológico'},
    'romance': {'en': 'Romance', 'ja': '恋愛', 'zh-Hans': '恋爱', 'zh-Hant': '戀愛', 'es': 'Romance'},
    'sci_fi': {'en': 'Science Fiction', 'ja': 'SF', 'zh-Hans': '科幻', 'zh-Hant': '科幻', 'es': 'Ciencia ficción'},
    'slice_of_life': {'en': 'Slice of Life', 'ja': '日常', 'zh-Hans': '日常', 'zh-Hant': '日常', 'es': 'Recuentos de la vida'},
    'sports': {'en': 'Sports', 'ja': 'スポーツ', 'zh-Hans': '运动', 'zh-Hant': '運動', 'es': 'Deportes'},
    'supernatural': {'en': 'Supernatural', 'ja': '超自然', 'zh-Hans': '超自然', 'zh-Hant': '超自然', 'es': 'Sobrenatural'},
    'suspense': {'en': 'Suspense', 'ja': 'サスペンス', 'zh-Hans': '惊悚', 'zh-Hant': '驚悚', 'es': 'Suspenso'},
    'erotica': {'en': 'Erotica', 'ja': 'エロティカ', 'zh-Hans': '情色', 'zh-Hant': '情色', 'es': 'Erótica'},
    'hentai': {'en': 'Hentai', 'ja': '成人向け', 'zh-Hans': '成人动画', 'zh-Hant': '成人動畫', 'es': 'Hentai'},
    'adult_cast': {'en': 'Adult Cast', 'ja': '成人主人公', 'zh-Hans': '成人角色', 'zh-Hant': '成人角色', 'es': 'Reparto adulto'},
    'anthropomorphic': {'en': 'Anthropomorphic', 'ja': '擬人化', 'zh-Hans': '拟人化', 'zh-Hant': '擬人化', 'es': 'Antropomorfismo'},
    'cgdct': {'en': 'Cute Girls Doing Cute Things', 'ja': '日常系', 'zh-Hans': '萌系日常', 'zh-Hant': '萌系日常', 'es': 'Chicas lindas haciendo cosas lindas'},
    'childcare': {'en': 'Childcare', 'ja': '子育て', 'zh-Hans': '育儿', 'zh-Hant': '育兒', 'es': 'Cuidado infantil'},
    'combat_sports': {'en': 'Combat Sports', 'ja': '格闘技', 'zh-Hans': '格斗运动', 'zh-Hant': '格鬥運動', 'es': 'Deportes de combate'},
    'crossdressing': {'en': 'Cross-Dressing', 'ja': '異性装', 'zh-Hans': '异装', 'zh-Hant': '異裝', 'es': 'Travestismo'},
    'delinquents': {'en': 'Delinquents', 'ja': '不良', 'zh-Hans': '不良少年', 'zh-Hant': '不良少年', 'es': 'Delincuentes'},
    'detective': {'en': 'Detective', 'ja': '探偵', 'zh-Hans': '侦探', 'zh-Hant': '偵探', 'es': 'Detectives'},
    'educational': {'en': 'Educational', 'ja': '教育', 'zh-Hans': '教育', 'zh-Hant': '教育', 'es': 'Educativo'},
    'gag_humor': {'en': 'Gag Humor', 'ja': 'ギャグ', 'zh-Hans': '搞笑', 'zh-Hant': '搞笑', 'es': 'Humor absurdo'},
    'gore': {'en': 'Gore', 'ja': 'ゴア', 'zh-Hans': '血腥', 'zh-Hant': '血腥', 'es': 'Gore'},
    'harem': {'en': 'Harem', 'ja': 'ハーレム', 'zh-Hans': '后宫', 'zh-Hant': '後宮', 'es': 'Harén'},
    'historical': {'en': 'Historical', 'ja': '歴史', 'zh-Hans': '历史', 'zh-Hant': '歷史', 'es': 'Histórico'},
    'idols': {'en': 'Idols', 'ja': 'アイドル', 'zh-Hans': '偶像', 'zh-Hant': '偶像', 'es': 'Ídolos'},
    'isekai': {'en': 'Isekai', 'ja': '異世界', 'zh-Hans': '异世界', 'zh-Hant': '異世界', 'es': 'Isekai'},
    'iyashikei': {'en': 'Iyashikei', 'ja': '癒やし系', 'zh-Hans': '治愈系', 'zh-Hant': '療癒系', 'es': 'Iyashikei'},
    'magical_girl': {'en': 'Magical Girl', 'ja': '魔法少女', 'zh-Hans': '魔法少女', 'zh-Hant': '魔法少女', 'es': 'Chicas mágicas'},
    'martial_arts': {'en': 'Martial Arts', 'ja': '武術', 'zh-Hans': '武术', 'zh-Hant': '武術', 'es': 'Artes marciales'},
    'mecha': {'en': 'Mecha', 'ja': 'メカ', 'zh-Hans': '机甲', 'zh-Hant': '機甲', 'es': 'Mecha'},
    'military': {'en': 'Military', 'ja': 'ミリタリー', 'zh-Hans': '军事', 'zh-Hant': '軍事', 'es': 'Militar'},
    'music': {'en': 'Music', 'ja': '音楽', 'zh-Hans': '音乐', 'zh-Hant': '音樂', 'es': 'Música'},
    'mythology': {'en': 'Mythology', 'ja': '神話', 'zh-Hans': '神话', 'zh-Hant': '神話', 'es': 'Mitología'},
    'parody': {'en': 'Parody', 'ja': 'パロディ', 'zh-Hans': '戏仿', 'zh-Hant': '戲仿', 'es': 'Parodia'},
    'performing_arts': {'en': 'Performing Arts', 'ja': '舞台芸術', 'zh-Hans': '表演艺术', 'zh-Hant': '表演藝術', 'es': 'Artes escénicas'},
    'racing': {'en': 'Racing', 'ja': 'レース', 'zh-Hans': '竞速', 'zh-Hant': '競速', 'es': 'Carreras'},
    'reincarnation': {'en': 'Reincarnation', 'ja': '転生', 'zh-Hans': '转生', 'zh-Hant': '轉生', 'es': 'Reencarnación'},
    'reverse_harem': {'en': 'Reverse Harem', 'ja': '逆ハーレム', 'zh-Hans': '逆后宫', 'zh-Hant': '逆後宮', 'es': 'Harén inverso'},
    'samurai': {'en': 'Samurai', 'ja': '侍', 'zh-Hans': '武士', 'zh-Hant': '武士', 'es': 'Samuráis'},
    'school': {'en': 'School', 'ja': '学園', 'zh-Hans': '校园', 'zh-Hant': '校園', 'es': 'Escolar'},
    'space': {'en': 'Space', 'ja': '宇宙', 'zh-Hans': '宇宙', 'zh-Hant': '宇宙', 'es': 'Espacio'},
    'super_power': {'en': 'Superpowers', 'ja': '超能力', 'zh-Hans': '超能力', 'zh-Hant': '超能力', 'es': 'Superpoderes'},
    'survival': {'en': 'Survival', 'ja': 'サバイバル', 'zh-Hans': '生存', 'zh-Hant': '生存', 'es': 'Supervivencia'},
    'time_travel': {'en': 'Time Travel', 'ja': 'タイムトラベル', 'zh-Hans': '时间旅行', 'zh-Hant': '時間旅行', 'es': 'Viajes en el tiempo'},
    'vampire': {'en': 'Vampires', 'ja': '吸血鬼', 'zh-Hans': '吸血鬼', 'zh-Hant': '吸血鬼', 'es': 'Vampiros'},
    'video_games': {'en': 'Video Games', 'ja': 'ゲーム', 'zh-Hans': '电子游戏', 'zh-Hant': '電子遊戲', 'es': 'Videojuegos'},
    'workplace': {'en': 'Workplace', 'ja': '職場', 'zh-Hans': '职场', 'zh-Hant': '職場', 'es': 'Entorno laboral'},
    'award_winning': {'en': 'Award-Winning', 'ja': '受賞作', 'zh-Hans': '获奖作品', 'zh-Hant': '獲獎作品', 'es': 'Premiado'},
    'high_stakes_game': {'en': 'High-Stakes Games', 'ja': '命懸けのゲーム', 'zh-Hans': '高风险游戏', 'zh-Hant': '高風險遊戲', 'es': 'Juegos de alto riesgo'},
    'love_polygon': {'en': 'Love Polygon', 'ja': '多角関係', 'zh-Hans': '多角恋', 'zh-Hant': '多角戀', 'es': 'Polígono amoroso'},
    'love_status_quo': {'en': 'Unresolved Romance', 'ja': '恋愛未満', 'zh-Hans': '恋爱未满', 'zh-Hant': '戀愛未滿', 'es': 'Romance no resuelto'},
    'magical_sex_shift': {'en': 'Magical Gender Shift', 'ja': '魔法による性転換', 'zh-Hans': '魔法性转', 'zh-Hant': '魔法性轉', 'es': 'Cambio mágico de género'},
    'medical': {'en': 'Medical', 'ja': '医療', 'zh-Hans': '医疗', 'zh-Hant': '醫療', 'es': 'Medicina'},
    'otaku_culture': {'en': 'Otaku Culture', 'ja': 'オタク文化', 'zh-Hans': '御宅文化', 'zh-Hant': '御宅文化', 'es': 'Cultura otaku'},
    'pets': {'en': 'Pets', 'ja': 'ペット', 'zh-Hans': '宠物', 'zh-Hant': '寵物', 'es': 'Mascotas'},
    'showbiz': {'en': 'Show Business', 'ja': '芸能界', 'zh-Hans': '演艺圈', 'zh-Hant': '演藝圈', 'es': 'Mundo del espectáculo'},
    'strategy_game': {'en': 'Strategy Games', 'ja': '頭脳戦', 'zh-Hans': '策略游戏', 'zh-Hant': '策略遊戲', 'es': 'Juegos de estrategia'},
    'team_sports': {'en': 'Team Sports', 'ja': '団体競技', 'zh-Hans': '团队运动', 'zh-Hant': '團隊運動', 'es': 'Deportes de equipo'},
    'urban_fantasy': {'en': 'Urban Fantasy', 'ja': '現代ファンタジー', 'zh-Hans': '都市奇幻', 'zh-Hant': '都市奇幻', 'es': 'Fantasía urbana'},
    'villainess': {'en': 'Villainess', 'ja': '悪役令嬢', 'zh-Hans': '恶役千金', 'zh-Hant': '惡役千金', 'es': 'Villana'},
    'visual_arts': {'en': 'Visual Arts', 'ja': '美術', 'zh-Hans': '视觉艺术', 'zh-Hant': '視覺藝術', 'es': 'Artes visuales'},
    'asia': {'en': 'Asia', 'ja': 'アジア', 'zh-Hans': '亚洲', 'zh-Hant': '亞洲', 'es': 'Asia'},
    'china': {'en': 'China', 'ja': '中国', 'zh-Hans': '中国', 'zh-Hant': '中國', 'es': 'China'},
    'japan': {'en': 'Japan', 'ja': '日本', 'zh-Hans': '日本', 'zh-Hant': '日本', 'es': 'Japón'},
    'earth': {'en': 'Earth', 'ja': '地球', 'zh-Hans': '地球', 'zh-Hant': '地球', 'es': 'Tierra'},
    'modern_day': {'en': 'Modern Day', 'ja': '現代', 'zh-Hans': '现代', 'zh-Hant': '現代', 'es': 'Época actual'},
    'sexual_content': {'en': 'Sexual Content', 'ja': '性的表現', 'zh-Hans': '性内容', 'zh-Hant': '性內容', 'es': 'Contenido sexual'},
    'serialized_story': {'en': 'Serialized Story', 'ja': '連続ストーリー', 'zh-Hans': '连续剧情', 'zh-Hant': '連續劇情', 'es': 'Historia serializada'},
    'kids': {'en': 'Kids', 'ja': '子供向け', 'zh-Hans': '儿童', 'zh-Hant': '兒童', 'es': 'Infantil'},
    'shounen': {'en': 'Shōnen', 'ja': '少年', 'zh-Hans': '少年', 'zh-Hant': '少年', 'es': 'Shōnen'},
    'shoujo': {'en': 'Shōjo', 'ja': '少女', 'zh-Hans': '少女', 'zh-Hant': '少女', 'es': 'Shōjo'},
    'seinen': {'en': 'Seinen', 'ja': '青年', 'zh-Hans': '青年', 'zh-Hant': '青年', 'es': 'Seinen'},
    'josei': {'en': 'Josei', 'ja': '女性', 'zh-Hans': '女性', 'zh-Hant': '女性', 'es': 'Josei'},
}

CATEGORY_LABELS = {
    'genre': {'en': 'Genres', 'ja': 'ジャンル', 'zh-Hans': '类型', 'zh-Hant': '類型', 'es': 'Géneros'},
    'theme': {'en': 'Themes', 'ja': 'テーマ', 'zh-Hans': '主题', 'zh-Hant': '主題', 'es': 'Temas'},
    'demographic': {'en': 'Demographic', 'ja': '対象', 'zh-Hans': '受众', 'zh-Hant': '受眾', 'es': 'Demografía'},
}


def genre_locale(locale='auto'):
    """Convert app locale names to the taxonomy's BCP-47-like keys."""
    language = (i18n.active_language() if locale in (None, 'auto')
                else i18n.effective_language(locale))
    return {'zh_CN': 'zh-Hans', 'zh_TW': 'zh-Hant'}.get(
        language, language)


def get_genre_label(tag_id, locale='auto'):
    labels = GENRE_LABELS.get(tag_id, {})
    language = genre_locale(locale)
    return labels.get(language) or labels.get('en') or tag_id


def get_category_label(category, locale='auto'):
    labels = CATEGORY_LABELS.get(category, {})
    language = genre_locale(locale)
    return labels.get(language) or labels.get('en') or category
