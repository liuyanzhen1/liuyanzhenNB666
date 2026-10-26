"""Read-only discovery of buttons in the explicitly selected Telegram group."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from telethon import TelegramClient, utils
from telethon.crypto import AuthKey
from telethon.sessions import MemorySession


async def main():
    root = Path(r"C:\Users\Administrator\Desktop\下注脚本")
    config = {}
    for line in (root / '.env').read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            config[key.strip()] = value.strip().strip('"').strip("'")
    connection = sqlite3.connect((root / 'data/tg_big_bet.session').as_uri() + '?mode=ro', uri=True)
    row = connection.execute('SELECT dc_id,server_address,port,auth_key FROM sessions').fetchone()
    connection.close()
    if not row or not row[3]:
        raise RuntimeError('Existing session is not authorized')
    session = MemorySession()
    session.set_dc(row[0], row[1], row[2])
    session.auth_key = AuthKey(row[3])
    client = TelegramClient(session, int(config['TG_API_ID']), config['TG_API_HASH'],
                            proxy=('socks5', '127.0.0.1', 10808), connection_retries=1)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError('Login required; no messages read')
        matches = []
        async for dialog in client.iter_dialogs():
            if dialog.name == '启点国际新VIP红包群':
                matches.append(dialog)
        if len(matches) != 1:
            print(json.dumps({'matching_groups': len(matches)}, ensure_ascii=False))
            return
        group = matches[0].entity
        print(json.dumps({'group_id': utils.get_peer_id(group), 'title': matches[0].name}, ensure_ascii=False))
        count = 0
        async for message in client.iter_messages(group, limit=200):
            if '红包' not in (message.raw_text or '') or not message.reply_markup:
                continue
            buttons = []
            for button_row in message.reply_markup.rows:
                for button in button_row.buttons:
                    action = getattr(button, 'type', button)
                    entry = {'type': type(action).__name__, 'text': button.text}
                    url = getattr(action, 'url', None)
                    if url:
                        parts = urlsplit(url)
                        entry.update(host=parts.hostname, path=parts.path,
                                     query_keys=[s.split('=')[0] for s in parts.query.split('&') if s])
                    data = getattr(action, 'data', None)
                    if data:
                        entry['callback_bytes'] = len(data)
                    buttons.append(entry)
            # Only packet metadata, excluding participants and their balances.
            body = message.raw_text or ''
            metadata = [line for line in body.splitlines() if '总金额' in line or '剩余' in line]
            sender = await message.get_sender()
            print(json.dumps({'message_id': message.id, 'sender_id': message.sender_id,
                              'sender_username': getattr(sender, 'username', None),
                              'sender_is_bot': getattr(sender, 'bot', False),
                              'metadata': metadata, 'buttons': buttons}, ensure_ascii=False))
            count += 1
            if count >= 6:
                break
        print(json.dumps({'inspected_packet_messages': count}))
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), timeout=45))
