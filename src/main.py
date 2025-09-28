import asyncio
import json
import os
from pathlib import Path
import signal
import sys

from bot import Bot
from log import getlogger

logger = getlogger()


async def main():
    need_import_keys = False
    config_path = Path(os.path.dirname(__file__)).parent / "config.json"
    help_message_path = (
        Path(os.path.dirname(__file__)).parent / "custom_help_message.txt"
    )

    if os.path.isfile(config_path):
        try:
            fp = open(config_path, encoding="utf8")
            config = json.load(fp)
        except Exception:
            logger.error("config.json load error, please check the file")
            sys.exit(1)

        text_url = config.get('text_dev_url') if bool(os.environ.get('DEV', False)) else config.get('text_url')
        audio_url = config.get('audio_dev_url') if os.environ.get('DEV', False) else config.get('audio_url')

        matrix_bot = Bot(
            user_id=config.get('user_id'),
            homeserver=config.get("homeserver"),
            access_token=config.get("access_token"),
            device_id=config.get("device_id"),
            whitelist_room_id=config.get("room_id"),
            timeout=config.get("timeout"),
            text_url=text_url,
            audio_url=audio_url,
            key=config.get('key')
        )

    await matrix_bot.login()
    if need_import_keys:
        logger.info("start import_keys process, this may take a while...")
        await matrix_bot.import_keys()

    sync_task = asyncio.create_task(
        matrix_bot.sync_forever(timeout=30000, full_state=True)
    )

    # handle signal interrupt
    loop = asyncio.get_running_loop()
    for signame in ("SIGINT", "SIGTERM"):
        loop.add_signal_handler(
            getattr(signal, signame),
            lambda: asyncio.create_task(matrix_bot.close(sync_task)),
        )

    if matrix_bot.client.should_upload_keys:
        await matrix_bot.client.keys_upload()

    await sync_task


if __name__ == "__main__":
    logger.info("matrix chatgpt bot start.....")
    asyncio.run(main())
