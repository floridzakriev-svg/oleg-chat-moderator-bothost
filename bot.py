import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.constants import ChatType
from telegram.ext import Application, ContextTypes, MessageHandler, filters

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')

TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
OPENROUTER_KEY = os.environ['OPENROUTER_API_KEY']
OPENROUTER_MODEL = os.getenv('OPENROUTER_MODEL', 'meta-llama/llama-3.1-8b-instruct:free')
ALLOWED_CHAT_ID = os.getenv('ALLOWED_CHAT_ID', '').strip()
DB_PATH = os.getenv('DB_PATH', str(BASE / 'oleg_bot.sqlite3'))

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'), format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('oleg-bot')

# Intentional: profanity is not included. Mat is allowed by the project rules.
INSULT_WORDS = {'идиот', 'дебил', 'тупой', 'тупица', 'кретин', 'урод', 'ничтожество', 'лох', 'мразь', 'мудак'}
HATE_WORDS = {'нацист', 'расист', 'ксенофоб', 'чурка', 'жид', 'нигер', 'пидор', 'гомофоб'}
SPAM_PATTERNS = [r'https?://\S+', r't\.me/\S+', r'заработок', r'казино', r'ставк', r'крипто', r'подпишись', r'промокод']
PROVOCATION_PATTERNS = [r'заткнись', r'иди сюда', r'слабо', r'а ну докажи', r'все вы туп', r'провоцирую']
ADULT_WORDS = {'секс', 'порно', 'эротик', '18+', 'интим', 'наркотик'}
POLITICS_WORDS = {'президент', 'выборы', 'правительств', 'партия', 'войн', 'политик'}
RELIGION_WORDS = {'бог', 'церков', 'религ', 'ислам', 'христиан', 'атеизм'}
TRIGGER = 'ОЛЕГ ОТВЕТ:'


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('''CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, level INTEGER NOT NULL DEFAULT 1,
        level_violations INTEGER NOT NULL DEFAULT 0, daily_questions INTEGER NOT NULL DEFAULT 0,
        daily_date TEXT, last_question REAL, active_mute_until INTEGER, PRIMARY KEY(chat_id,user_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS mutes (
        chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, until_ts INTEGER NOT NULL,
        PRIMARY KEY(chat_id,user_id))''')
    conn.commit()
    return conn


def get_user(conn, chat_id, user_id):
    row = conn.execute('SELECT * FROM users WHERE chat_id=? AND user_id=?', (chat_id, user_id)).fetchone()
    if not row:
        today = datetime.now(timezone.utc).date().isoformat()
        conn.execute('INSERT INTO users(chat_id,user_id,daily_date) VALUES(?,?,?)', (chat_id,user_id,today))
        conn.commit()
        row = conn.execute('SELECT * FROM users WHERE chat_id=? AND user_id=?', (chat_id, user_id)).fetchone()
    return row


def is_allowed_chat(update: Update) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(update.effective_chat.id) == ALLOWED_CHAT_ID


def contains_any(text, words):
    low = text.casefold()
    return any(w in low for w in words)


def violation_reason(text):
    low = text.casefold()
    if any(re.search(p, low) for p in SPAM_PATTERNS): return 'спам'
    if contains_any(low, HATE_WORDS): return 'разжигание ненависти'
    if contains_any(low, INSULT_WORDS): return 'оскорбление'
    if any(re.search(p, low) for p in PROVOCATION_PATTERNS): return 'провокация'
    return None


def restricted_permissions():
    return ChatPermissions(can_send_messages=False, can_send_audios=False, can_send_documents=False,
        can_send_photos=False, can_send_videos=False, can_send_video_notes=False,
        can_send_voice_notes=False, can_send_polls=False, can_send_other_messages=False,
        can_add_web_page_previews=False, can_change_info=False, can_invite_users=False,
        can_pin_messages=False)


def normal_permissions():
    return ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
        can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
        can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
        can_add_web_page_previews=True, can_invite_users=True)


def format_duration(seconds):
    if seconds == 86400: return '24 часа'
    if seconds == 172800: return '48 часов'
    return '7 дней'


async def punish_or_warn(update, context, reason):
    chat, user = update.effective_chat, update.effective_user
    conn = db(); row = get_user(conn, chat.id, user.id)
    level, count = row['level'], row['level_violations']
    durations = {1: 86400, 2: 172800, 3: 7 * 86400}
    try:
        await update.effective_message.delete()
    except Exception as e:
        log.warning('Could not delete violation: %s', e)
    if level <= 3 and count == 0:
        warning = {1: 'Ещё раз нарушишь — замучу на 24 часа', 2: 'Ещё раз нарушишь — замучу на 48 часов', 3: 'Ещё раз нарушишь — замучу на 7 дней'}[level]
        conn.execute('UPDATE users SET level_violations=1 WHERE chat_id=? AND user_id=?', (chat.id,user.id)); conn.commit()
        await context.bot.send_message(chat.id, f'Предупреждение для {user.mention_html()}: {warning}.', parse_mode='HTML')
        return
    if level <= 3:
        seconds = durations[level]; until = int(time.time()) + seconds
        await context.bot.restrict_chat_member(chat.id, user.id, permissions=restricted_permissions(), until_date=until)
        conn.execute('UPDATE users SET level=?, level_violations=0, active_mute_until=? WHERE chat_id=? AND user_id=?', (level+1,0,until,chat.id,user.id))
        conn.execute('INSERT OR REPLACE INTO mutes(chat_id,user_id,until_ts) VALUES(?,?,?)', (chat.id,user.id,until)); conn.commit()
        await context.bot.send_message(chat.id, f'{user.mention_html()} получил мут на {format_duration(seconds)}.', parse_mode='HTML')
        return
    await context.bot.ban_chat_member(chat.id, user.id)
    conn.execute('UPDATE users SET level=4, level_violations=0 WHERE chat_id=? AND user_id=?', (chat.id,user.id)); conn.commit()
    await context.bot.send_message(chat.id, f'{user.mention_html()} заблокирован навсегда.', parse_mode='HTML')


async def unmute_expired(context: ContextTypes.DEFAULT_TYPE):
    now = int(time.time()); conn = db()
    rows = conn.execute('SELECT * FROM mutes WHERE until_ts <= ?', (now,)).fetchall()
    for row in rows:
        try:
            await context.bot.restrict_chat_member(row['chat_id'], row['user_id'], permissions=normal_permissions())
            conn.execute('DELETE FROM mutes WHERE chat_id=? AND user_id=?', (row['chat_id'],row['user_id']))
            conn.execute('UPDATE users SET active_mute_until=NULL WHERE chat_id=? AND user_id=?', (row['chat_id'],row['user_id']))
            conn.commit()
        except Exception as e:
            log.warning('Could not unmute %s/%s: %s', row['chat_id'], row['user_id'], e)


async def ask_openrouter(text):
    topic_block = contains_any(text, ADULT_WORDS | POLITICS_WORDS | RELIGION_WORDS)
    system = ('Ты Олег из проекта «Психика на минималках». Отвечай по-русски, спокойно, коротко, с мягкой самоиронией и бытовым юмором. '
              'Не утверждай, что ты настоящий человек. На темы 18+, политики и религии вежливо уклоняйся, не молчи: пошути и переведи разговор на нейтральную бытовую тему. '
              'Ответ до 500 символов, без токсичности, угроз и медицинских советов.')
    if topic_block: system += ' Пользователь затронул чувствительную тему — обязательно мягко уклонись и переведи разговор.'
    headers = {'Authorization': f'Bearer {OPENROUTER_KEY}', 'Content-Type': 'application/json', 'HTTP-Referer': 'https://openrouter.ai', 'X-Title': 'Oleg Chat Bot'}
    payload = {'model': OPENROUTER_MODEL, 'messages': [{'role':'system','content':system},{'role':'user','content':text}], 'max_tokens':180, 'temperature':0.75}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post('https://openrouter.ai/api/v1/chat/completions', headers=headers, json=payload)
        r.raise_for_status(); data = r.json()
    return data['choices'][0]['message']['content'].strip()[:1000]


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or not update.effective_chat:
        log.info('Ignored update without effective message/chat: update_id=%s', update.update_id)
        return
    log.info('Incoming message: update_id=%s chat_id=%s chat_type=%s user_id=%s',
             update.update_id, update.effective_chat.id, update.effective_chat.type,
             update.effective_user.id if update.effective_user else None)
    if update.effective_chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        log.info('Ignored non-group chat: chat_type=%s', update.effective_chat.type)
        return
    if not is_allowed_chat(update):
        log.warning('Ignored chat not allowed: received=%s configured=%s', update.effective_chat.id, ALLOWED_CHAT_ID or 'all groups')
        return
    text = msg.text or msg.caption or ''
    if msg.from_user and msg.from_user.is_bot:
        # Keep Oleg's own warnings and answers visible; remove messages from other bots.
        if msg.from_user.id == context.bot.id:
            return
        try: await msg.delete()
        except Exception as e: log.warning('Could not delete bot message: %s', e)
        return

    # Recognize the Oleg trigger before applying the whole-message moderation
    # filter. The trigger itself must never be treated as a violation. The
    # question text is still checked below, so spam/hate/insults in a question
    # remain moderated.
    is_trigger = text.startswith(TRIGGER)
    if is_trigger:
        question = text[len(TRIGGER):].strip()
        reason = violation_reason(question)
        if reason:
            log.info('Trigger question moderated: reason=%s', reason)
            await punish_or_warn(update, context, reason)
            return
    else:
        reason = violation_reason(text)
        if reason:
            await punish_or_warn(update, context, reason)
        return

    if len(question) > 300:
        await msg.reply_text('Вопрос слишком длинный. Олег уже устал до первой строки — максимум 300 символов.')
        return
    conn=db(); row=get_user(conn, update.effective_chat.id, update.effective_user.id); now=time.time()
    today=datetime.now(timezone.utc).date().isoformat()
    count = 0 if row['daily_date'] != today else row['daily_questions']
    if row['last_question'] and now-row['last_question'] < 15:
        await msg.reply_text('Олег отвечает не чаще одного раза в 15 секунд. Даже кофе так быстро не остывает.')
        return
    if count >= 10:
        await msg.reply_text('Лимит на сегодня исчерпан: 10 вопросов. Олег ушёл перезаряжаться.')
        return
    conn.execute('UPDATE users SET daily_questions=?, daily_date=?, last_question=? WHERE chat_id=? AND user_id=?', (count+1,today,now,update.effective_chat.id,update.effective_user.id)); conn.commit()
    try:
        answer = await ask_openrouter(question)
        await msg.reply_text(answer)
    except Exception:
        log.exception('OpenRouter request failed')
        await msg.reply_text('Олег хотел ответить умно, но нейросеть ушла на обед. Попробуй ещё раз чуть позже.')


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.ALL, on_message))
    app.job_queue.run_repeating(unmute_expired, interval=60, first=10)
    log.info('Oleg bot started; model=%s allowed_chat=%s', OPENROUTER_MODEL, ALLOWED_CHAT_ID or 'all groups')
    # Explicitly request ordinary message updates. Update.ALL_TYPES also works,
    # but this list makes the polling contract visible in Bothost logs/config.
    app.run_polling(allowed_updates=['message', 'edited_message', 'channel_post', 'edited_channel_post'])

if __name__ == '__main__': main()
