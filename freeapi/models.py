"""
models.py — Список моделей FavoriteAPI.

W4: список БОЛЬШЕ не захардкожен жёстко. AI_MODELS внизу — seed/fallback на
случай сбоя парсинга. Актуальный список read_models_from_bot(tg, bot) парсит
inline-кнопки select_gpt_model:* из секции выбора модели TG-бота. Новые модели
(напр. Flash 3.5) подхватываются автоматически без правки кода.

Кеш на уровне модуля (_MODELS_CACHE) с TTL — парсинг идёт только в configure_gpt
(раз за setup), /api/models и /api/v1/models отдают кеш; если кеш пуст — fallback
на AI_MODELS.
"""
import asyncio
import logging
import time

logger = logging.getLogger('freeapi')

# ──────────────────────────────────────────────────────
# SEED / FALLBACK (только если парсинг бота не удался)
# ──────────────────────────────────────────────────────
# Поля: id, displayName, tgCallback, contextK, supportsVision, isDefault, isPopular
AI_MODELS = [
    {'id': 'gemini-1.5-robotics-er-preview', 'displayName': 'Gemini 1.5 Robotics 200k', 'tgCallback': 'select_gpt_model:gemini-robotics-er-1.5-preview', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-2.5-mini', 'displayName': 'Gemini 2.5 Mini 200k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash-lite-no-thinking', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-2.5-mini-thinking', 'displayName': 'Gemini 2.5 Mini Thinking 200k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash-lite', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-2.5-flash', 'displayName': 'Gemini 2.5 Flash 200k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash-no-thinking-200k', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-2.5-flash-thinking', 'displayName': 'Gemini 2.5 Flash Thinking 200k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash-200k', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-3.0-flash', 'displayName': 'Gemini 3.0 Flash 200k', 'tgCallback': 'select_gpt_model:gemini-3-flash-preview-no-thinking-200k', 'contextK': 200, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-3.0-flash-thinking', 'displayName': 'Gemini 3.0 Flash Thinking 200k', 'tgCallback': 'select_gpt_model:gemini-3-flash-preview-200k', 'contextK': 200, 'supportsVision': True, 'isDefault': True, 'isPopular': False},
    {'id': 'gemini-2.5-flash-64k', 'displayName': 'Gemini 2.5 Flash 64k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash-no-thinking', 'contextK': 64, 'supportsVision': True, 'isDefault': False, 'isPopular': True},
    {'id': 'gemini-2.5-flash-thinking-64k', 'displayName': 'Gemini 2.5 Flash Thinking 64k', 'tgCallback': 'select_gpt_model:gemini-2.5-flash', 'contextK': 64, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-3.0-flash-64k', 'displayName': 'Gemini 3.0 Flash 64k', 'tgCallback': 'select_gpt_model:gemini-3-flash-preview-no-thinking', 'contextK': 64, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
    {'id': 'gemini-3.0-flash-thinking-64k', 'displayName': 'Gemini 3.0 Flash Thinking 64k', 'tgCallback': 'select_gpt_model:gemini-3-flash-preview', 'contextK': 64, 'supportsVision': True, 'isDefault': False, 'isPopular': False},
]
DEFAULT_MODEL_ID = 'gemini-3.0-flash-thinking'

# ──────────────────────────────────────────────────────
# КЕШ ДИНАМИЧЕСКОГО СПИСКА
# ──────────────────────────────────────────────────────
_MODELS_CACHE_TTL = 3600   # секунды (1 час)
_MODELS_CACHE = {'models': None, 'ts': 0.0, 'lock': asyncio.Lock()}


def _is_gpt_callback(callback: str) -> bool:
    """W4: фильтр GPT-моделей. callback вида 'select_gpt_model:gpt...'.

    По ТЗ пользователя — GPT-модели исключаются из выдачи FavoriteAPI (они не
    нужны/недоступны на бесплатных ключах). Срабатывает и по префиксу 'gpt'
    в хвосте callback, и если displayName содержит 'GPT'.
    """
    if not callback:
        return True
    tail = callback.split('select_gpt_model:', 1)[-1].lower()
    return tail.startswith('gpt')


async def read_models_from_bot(tg, bot) -> list[dict]:
    """W4: распарсить актуальный список моделей из секции выбора TG-бота.

    Открывает секцию 'open_change_gpt_model_section', читает inline-кнопки
    с data 'select_gpt_model:*', собирает {id, displayName, tgCallback,
    contextK, supportsVision, isDefault}. GPT-модели фильтруются (_is_gpt_callback).

    'id' выводим из tgCallback (хвост после 'select_gpt_model:') — это и есть
    идентификатор, который FavoriteAPI принимает в /api/v1/chat. isDefault=true
    ставим на текущую выбранную (если бот её подсвечивает) — иначе на DEFAULT_MODEL_ID.
    """
    from freeapi.tg import last_message
    msg = await last_message(tg, bot)
    # открыть секцию настроек GPT → смену модели
    for cb in (b'open_gpt_settings_section', b'open_change_gpt_model_section'):
        try:
            if msg and getattr(msg, 'buttons', None):
                await tg.click(msg, data=cb)
                await asyncio.sleep(1.5)
            msg = await last_message(tg, bot)
        except Exception as e:
            logger.warning('[models] click %s failed: %s', cb, e)

    out: list[dict] = []
    seen = set()
    if not msg or not getattr(msg, 'buttons', None):
        return out
    for row in (msg.buttons or []):
        for btn in row:
            data = getattr(btn, 'data', None)
            if isinstance(data, bytes):
                data = data.decode('utf-8', 'replace')
            if not data or not data.startswith('select_gpt_model:'):
                continue
            if _is_gpt_callback(data):
                continue
            tail = data.split('select_gpt_model:', 1)[-1]
            if not tail or tail in seen:
                continue
            seen.add(tail)
            display = (getattr(btn, 'text', '') or tail).strip()
            # 200k vs 64k — по суффиксу в callback/displayName
            ctx_k = 64 if ('64k' in tail or '64k' in display.lower()) else 200
            out.append({
                'id': tail,
                'displayName': display,
                'tgCallback': data,
                'contextK': ctx_k,
                'supportsVision': True,
                'isDefault': (tail == DEFAULT_MODEL_ID),
                'isPopular': False,
            })
    # дефолт: если DEFAULT_MODEL_ID есть в списке — пометить, иначе первый
    if out:
        default_set = any(m['id'] == DEFAULT_MODEL_ID for m in out)
        if not default_set and out:
            out[0]['isDefault'] = True
    return out


async def get_cached_models() -> list[dict]:
    """Вернуть актуальный список моделей (из кеша, иначе seed AI_MODELS).

    /api/models и /api/v1/models зовут это. Кеш наполняется в configure_gpt
    через cache_models(); TTL истёк → отдаём seed (чтобы не блокировать API
    парсингом бота на каждый запрос).
    """
    now = time.time()
    if _MODELS_CACHE['models'] is not None and (now - _MODELS_CACHE['ts']) < _MODELS_CACHE_TTL:
        return _MODELS_CACHE['models']
    return list(AI_MODELS)


async def cache_models(models: list[dict]) -> None:
    """Записать свежеспарсенный список в кеш (звонит configure_gpt)."""
    async with _MODELS_CACHE['lock']:
        _MODELS_CACHE['models'] = list(models) if models else None
        _MODELS_CACHE['ts'] = time.time()


def find_model(model_id):
    """Синхронный поиск по кешу/seed. Для configure_gpt/bootstrap."""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return next((m for m in AI_MODELS if m['id'] == model_id), None)
    if loop.is_running():
        # внутри асинхронного контекста — не зовём async get; берём кеш напрямую
        cache = _MODELS_CACHE['models']
        pool = cache if cache is not None else AI_MODELS
    else:
        cache = loop.run_until_complete(get_cached_models())
        pool = cache
    return next((m for m in pool if m['id'] == model_id), None)


def is_valid_model_id(model_id):
    return find_model(model_id) is not None
