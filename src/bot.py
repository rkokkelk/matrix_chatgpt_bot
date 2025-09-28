import asyncio
import markdown
import os
from pathlib import Path
import re
import sys
import traceback
from typing import Union, Optional
import aiofiles.os
import base64

import httpx

from nio import (
    AsyncClient,
    AsyncClientConfig,
    InviteMemberEvent,
    JoinError,
    KeyVerificationCancel,
    KeyVerificationEvent,
    EncryptionError,
    KeyVerificationKey,
    KeyVerificationMac,
    KeyVerificationStart,
    LocalProtocolError,
    DownloadError,
    LoginResponse,
    MatrixRoom,
    MegolmEvent,
    RoomMessageText,
    RoomMessageAudio,
    ToDeviceError,
    WhoamiResponse,
    UploadResponse
)
from nio.store.database import SqliteStore
from nio.api import Api

from log import getlogger

logger = getlogger()


class Bot:
    def __init__(
        self,
        homeserver: str,
        user_id: str,
        device_id: str,
        text_url: str,
        audio_url: str,
        key: str,
        password: Union[str, None] = None,
        access_token: Union[str, None] = None,
        whitelist_room_id: Optional[list[str]] = None,
        timeout: Union[float, None] = None,
    ):
        if homeserver is None or user_id is None or device_id is None:
            logger.error("homeserver && user_id && device_id is required")
            sys.exit(1)

        if password is None and access_token is None:
            logger.error("password is required")
            sys.exit(1)

        self.homeserver: str = homeserver
        self.user_id: str = user_id
        self.password: str = password
        self.access_token: str = access_token
        self.device_id: str = device_id
        self.text_url = text_url
        self.audio_url = audio_url
        self.key = key

        self.timeout: float = timeout or 120.0
        self.base_path = Path(os.path.dirname(__file__)).parent

        self.whitelist_room_id = whitelist_room_id
        if whitelist_room_id is not None:
            if isinstance(whitelist_room_id, str):
                whitelist_room_id_list = list()
                for room_id in whitelist_room_id.split(","):
                    whitelist_room_id_list.append(room_id.strip())
                self.whitelist_room_id = whitelist_room_id_list

        self.httpx_client = httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.timeout,
        )

        # initialize AsyncClient object
        self.store_path = self.base_path
        self.config = AsyncClientConfig(
            store=SqliteStore,
            store_name="sync_db",
            store_sync_tokens=True,
            encryption_enabled=True,
        )
        self.client = AsyncClient(
            homeserver=self.homeserver,
            user=self.user_id,
            device_id=self.device_id,
            config=self.config,
            store_path=self.store_path,
        )

        # setup event callbacks
        self.client.add_event_callback(self.message_callback, (RoomMessageText,))
        self.client.add_event_callback(self.audio_callback, (RoomMessageAudio,))
        self.client.add_event_callback(self.decryption_failure, (MegolmEvent,))
        self.client.add_event_callback(self.invite_callback, (InviteMemberEvent,))
        self.client.add_to_device_callback(
            self.to_device_callback, (KeyVerificationEvent,)
        )

    async def close(self, task: asyncio.Task) -> None:
        await self.httpx_client.aclose()
        if self.lc_admin is not None:
            self.lc_manager.c.close()
            self.lc_manager.conn.close()
        await self.client.close()
        task.cancel()
        logger.info("Bot closed!")

    # message_callback RoomMessageText event
    async def audio_callback(self, room: MatrixRoom, event: RoomMessageAudio) -> None:
        # prevent command trigger loop
        if self.user_id != event.sender:

            audio = await self.client.download(event.url)

            r = await self.httpx_client.post(
                self.audio_url,
                headers={'x-n8n-auth': self.key},
                files={'file': (audio.filename, audio.body, audio.content_type)}
            )
            if r.status_code > 299:
                logger.error("Failed to send audio to N8N")
            else:
                logger.info("Succesfully send audio to N8N")

    # message_callback RoomMessageText event
    async def message_callback(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if self.whitelist_room_id is not None:
            if room.room_id not in self.whitelist_room_id:
                return
        room_id = room.room_id

        # reply event_id
        reply_to_event_id = event.event_id

        # sender_id
        sender_id = event.sender

        # event source
        event_source = event.source

        # user_message
        raw_user_message = event.body

        # prevent command trigger loop
        if self.user_id != event.sender:

            r = await self.httpx_client.post(
                self.text_url,
                headers={'x-n8n-auth': self.key},
                json=event.source
            )

            if r.status_code > 299:
                logger.error("Failed to send txt to N8N")
            else:
                logger.info("Succesfully send txt to N8N")

    # message_callback decryption_failure event
    async def decryption_failure(self, room: MatrixRoom, event: MegolmEvent) -> None:
        if not isinstance(event, MegolmEvent):
            return

        logger.error(
            f"Failed to decrypt message: {event.event_id} \
                from {event.sender} in {room.room_id}\n"
            + "Please make sure the bot current session is verified"
        )

    # invite_callback event
    async def invite_callback(self, room: MatrixRoom, event: InviteMemberEvent) -> None:
        """Handle an incoming invite event.
        If an invite is received, then join the room specified in the invite.
        code copied from: https://github.com/8go/matrix-eno-bot/blob/ad037e02bd2960941109e9526c1033dd157bb212/callbacks.py#L104
        """
        logger.debug(f"Got invite to {room.room_id} from {event.sender}.")
        # Attempt to join 3 times before giving up
        for attempt in range(3):
            result = await self.client.join(room.room_id)
            if type(result) is JoinError:
                logger.error(
                    f"Error joining room {room.room_id} (attempt %d): %s",
                    attempt,
                    result.message,
                )
            else:
                break
        else:
            logger.error("Unable to join room: %s", room.room_id)

        # Successfully joined room
        logger.info(f"Joined {room.room_id}")

    # to_device_callback event
    async def to_device_callback(self, event: KeyVerificationEvent) -> None:
        """Handle events sent to device.

        Specifically this will perform Emoji verification.
        It will accept an incoming Emoji verification requests
        and follow the verification protocol.
        code copied from: https://github.com/8go/matrix-eno-bot/blob/ad037e02bd2960941109e9526c1033dd157bb212/callbacks.py#L127
        """
        try:
            client = self.client
            logger.debug(
                f"Device Event of type {type(event)} received in " "to_device_cb()."
            )

            if isinstance(event, KeyVerificationStart):  # first step
                """first step: receive KeyVerificationStart
                KeyVerificationStart(
                    source={'content':
                            {'method': 'm.sas.v1',
                             'from_device': 'DEVICEIDXY',
                             'key_agreement_protocols':
                                ['curve25519-hkdf-sha256', 'curve25519'],
                             'hashes': ['sha256'],
                             'message_authentication_codes':
                                ['hkdf-hmac-sha256', 'hmac-sha256'],
                             'short_authentication_string':
                                ['decimal', 'emoji'],
                             'transaction_id': 'SomeTxId'
                             },
                            'type': 'm.key.verification.start',
                            'sender': '@user2:example.org'
                            },
                    sender='@user2:example.org',
                    transaction_id='SomeTxId',
                    from_device='DEVICEIDXY',
                    method='m.sas.v1',
                    key_agreement_protocols=[
                        'curve25519-hkdf-sha256', 'curve25519'],
                    hashes=['sha256'],
                    message_authentication_codes=[
                        'hkdf-hmac-sha256', 'hmac-sha256'],
                    short_authentication_string=['decimal', 'emoji'])
                """

                if "emoji" not in event.short_authentication_string:
                    estr = (
                        "Other device does not support emoji verification "
                        f"{event.short_authentication_string}. Aborting."
                    )
                    logger.info(estr)
                    return
                resp = await client.accept_key_verification(event.transaction_id)
                if isinstance(resp, ToDeviceError):
                    estr = f"accept_key_verification() failed with {resp}"
                    logger.info(estr)

                sas = client.key_verifications[event.transaction_id]

                todevice_msg = sas.share_key()
                resp = await client.to_device(todevice_msg)
                if isinstance(resp, ToDeviceError):
                    estr = f"to_device() failed with {resp}"
                    logger.info(estr)

            elif isinstance(event, KeyVerificationCancel):  # anytime
                """at any time: receive KeyVerificationCancel
                KeyVerificationCancel(source={
                    'content': {'code': 'm.mismatched_sas',
                                'reason': 'Mismatched authentication string',
                                'transaction_id': 'SomeTxId'},
                    'type': 'm.key.verification.cancel',
                    'sender': '@user2:example.org'},
                    sender='@user2:example.org',
                    transaction_id='SomeTxId',
                    code='m.mismatched_sas',
                    reason='Mismatched short authentication string')
                """

                # There is no need to issue a
                # client.cancel_key_verification(tx_id, reject=False)
                # here. The SAS flow is already cancelled.
                # We only need to inform the user.
                estr = (
                    f"Verification has been cancelled by {event.sender} "
                    f'for reason "{event.reason}".'
                )
                logger.info(estr)

            elif isinstance(event, KeyVerificationKey):  # second step
                """Second step is to receive KeyVerificationKey
                KeyVerificationKey(
                    source={'content': {
                            'key': 'SomeCryptoKey',
                            'transaction_id': 'SomeTxId'},
                        'type': 'm.key.verification.key',
                        'sender': '@user2:example.org'
                    },
                    sender='@user2:example.org',
                    transaction_id='SomeTxId',
                    key='SomeCryptoKey')
                """
                sas = client.key_verifications[event.transaction_id]

                logger.info(f"{sas.get_emoji()}")
                # don't log the emojis

                # The bot process must run in forground with a screen and
                # keyboard so that user can accept/reject via keyboard.
                # For emoji verification bot must not run as service or
                # in background.
                # yn = input("Do the emojis match? (Y/N) (C for Cancel) ")
                # automatic match, so we use y
                yn = "y"
                if yn.lower() == "y":
                    estr = (
                        "Match! The verification for this " "device will be accepted."
                    )
                    logger.info(estr)
                    resp = await client.confirm_short_auth_string(event.transaction_id)
                    if isinstance(resp, ToDeviceError):
                        estr = "confirm_short_auth_string() " f"failed with {resp}"
                        logger.info(estr)
                elif yn.lower() == "n":  # no, don't match, reject
                    estr = (
                        "No match! Device will NOT be verified "
                        "by rejecting verification."
                    )
                    logger.info(estr)
                    resp = await client.cancel_key_verification(
                        event.transaction_id, reject=True
                    )
                    if isinstance(resp, ToDeviceError):
                        estr = f"cancel_key_verification failed with {resp}"
                        logger.info(estr)
                else:  # C or anything for cancel
                    estr = "Cancelled by user! Verification will be " "cancelled."
                    logger.info(estr)
                    resp = await client.cancel_key_verification(
                        event.transaction_id, reject=False
                    )
                    if isinstance(resp, ToDeviceError):
                        estr = f"cancel_key_verification failed with {resp}"
                        logger.info(estr)

            elif isinstance(event, KeyVerificationMac):  # third step
                """Third step is to receive KeyVerificationMac
                KeyVerificationMac(
                    source={'content': {
                        'mac': {'ed25519:DEVICEIDXY': 'SomeKey1',
                                'ed25519:SomeKey2': 'SomeKey3'},
                        'keys': 'SomeCryptoKey4',
                        'transaction_id': 'SomeTxId'},
                        'type': 'm.key.verification.mac',
                        'sender': '@user2:example.org'},
                    sender='@user2:example.org',
                    transaction_id='SomeTxId',
                    mac={'ed25519:DEVICEIDXY': 'SomeKey1',
                         'ed25519:SomeKey2': 'SomeKey3'},
                    keys='SomeCryptoKey4')
                """
                sas = client.key_verifications[event.transaction_id]
                try:
                    todevice_msg = sas.get_mac()
                except LocalProtocolError as e:
                    # e.g. it might have been cancelled by ourselves
                    estr = (
                        f"Cancelled or protocol error: Reason: {e}.\n"
                        f"Verification with {event.sender} not concluded. "
                        "Try again?"
                    )
                    logger.info(estr)
                else:
                    resp = await client.to_device(todevice_msg)
                    if isinstance(resp, ToDeviceError):
                        estr = f"to_device failed with {resp}"
                        logger.info(estr)
                    estr = (
                        f"sas.we_started_it = {sas.we_started_it}\n"
                        f"sas.sas_accepted = {sas.sas_accepted}\n"
                        f"sas.canceled = {sas.canceled}\n"
                        f"sas.timed_out = {sas.timed_out}\n"
                        f"sas.verified = {sas.verified}\n"
                        f"sas.verified_devices = {sas.verified_devices}\n"
                    )
                    logger.info(estr)
                    estr = (
                        "Emoji verification was successful!\n"
                        "Initiate another Emoji verification from "
                        "another device or room if desired. "
                        "Or if done verifying, hit Control-C to stop the "
                        "bot in order to restart it as a service or to "
                        "run it in the background."
                    )
                    logger.info(estr)
            else:
                estr = (
                    f"Received unexpected event type {type(event)}. "
                    f"Event is {event}. Event will be ignored."
                )
                logger.info(estr)
        except BaseException:
            estr = traceback.format_exc()
            logger.info(estr)

    # thread chat
    async def thread_chat(
        self, room_id, reply_to_event_id, thread_root_id, prompt, sender_id
    ):
        try:
            await self.client.room_typing(room_id, timeout=int(self.timeout) * 1000)
            content = await self.chatbot.ask_async_v2(
                prompt=prompt,
                convo_id=thread_root_id,
            )
            await self.send_room_message(
                self.client,
                room_id,
                reply_message=content,
                reply_to_event_id=reply_to_event_id,
                sender_id=sender_id,
                reply_in_thread=True,
                thread_root_id=thread_root_id,
            )
        except Exception as e:
            logger.error(e)
            await self.send_room_message(
                self.client,
                room_id,
                reply_message=GENERAL_ERROR_MESSAGE,
                sender_id=sender_id,
                reply_to_event_id=reply_to_event_id,
                reply_in_thread=True,
                thread_root_id=thread_root_id,
            )

    # send general error message
    async def send_general_error_message(
        self, room_id, reply_to_event_id, sender_id, user_message
    ):
        await self.send_room_message(
            self.client,
            room_id,
            reply_message='ERROR',
            reply_to_event_id=reply_to_event_id,
            sender_id=sender_id,
            user_message=user_message,
        )

    # send Invalid number of parameters to room
    async def send_invalid_number_of_parameters_message(
        self, room_id, reply_to_event_id, sender_id, user_message
    ):
        await self.send_room_message(
            self.client,
            room_id,
            reply_message='INVALID_NUMBER',
            reply_to_event_id=reply_to_event_id,
            sender_id=sender_id,
            user_message=user_message,
        )

    # bot login
    async def login(self) -> None:
        try:
            if self.password is not None:
                resp = await self.client.login(
                    password=self.password, device_name='N8N'
                )
                if not isinstance(resp, LoginResponse):
                    logger.error("Login Failed")
                    await self.httpx_client.aclose()
                    await self.client.close()
                    sys.exit(1)
                logger.info("Successfully login via password")
                self.access_token = resp.access_token
            elif self.access_token is not None:
                self.client.restore_login(
                    user_id=self.user_id,
                    device_id=self.device_id,
                    access_token=self.access_token,
                )
                resp = await self.client.whoami()
                if not isinstance(resp, WhoamiResponse):
                    logger.error("Login Failed")
                    await self.close()
                    sys.exit(1)
                logger.info("Successfully login via access_token")
        except Exception as e:
            logger.error(e)
            await self.close()
            sys.exit(1)

    # import keys
    async def import_keys(self):
        resp = await self.client.import_keys(
            self.import_keys_path, self.import_keys_password
        )
        if isinstance(resp, EncryptionError):
            logger.error(f"import_keys failed with {resp}")
        else:
            logger.info("import_keys success, you can remove import_keys configuration")

    # sync messages in the room
    async def sync_forever(self, timeout=30000, full_state=True) -> None:
        await self.client.sync_forever(timeout=timeout, full_state=full_state)

    # get event from http
    async def get_event(self, room_id: str, event_id: str) -> dict:
        method, path = Api.room_get_event(self.access_token, room_id, event_id)
        url = self.homeserver + path
        if method == "GET":
            resp = await self.httpx_client.get(url)
            return resp.json()
        elif method == "POST":
            resp = await self.httpx_client.post(url)
            return resp.json()

    # download mxc
    async def download_mxc(self, mxc: str, filename: Optional[str] = None):
        response = await self.client.download(mxc, filename)
        return response

    async def send_room_message(
        client: AsyncClient,
        room_id: str,
        reply_message: str,
        sender_id: str = "",
        user_message: str = "",
        reply_to_event_id: str = "",
        reply_in_thread: bool = False,
        thread_root_id: str = "",
    ) -> None:
        if reply_to_event_id == "":
            content = {
                "msgtype": "m.text",
                "body": reply_message,
                "format": "org.matrix.custom.html",
                "formatted_body": markdown.markdown(
                    reply_message,
                    extensions=["nl2br", "tables", "fenced_code"],
                ),
            }
        elif reply_in_thread and thread_root_id:
            content = {
                "msgtype": "m.text",
                "body": reply_message,
                "format": "org.matrix.custom.html",
                "formatted_body": markdown.markdown(
                    reply_message,
                    extensions=["nl2br", "tables", "fenced_code"],
                ),
                "m.relates_to": {
                    "m.in_reply_to": {"event_id": reply_to_event_id},
                    "rel_type": "m.thread",
                    "event_id": thread_root_id,
                    "is_falling_back": True,
                },
            }

        else:
            body = "> <" + sender_id + "> " + user_message + "\n\n" + reply_message
            format = r"org.matrix.custom.html"
            formatted_body = (
                r'<mx-reply><blockquote><a href="https://matrix.to/#/'
                + room_id
                + r"/"
                + reply_to_event_id
                + r'">In reply to</a> <a href="https://matrix.to/#/'
                + sender_id
                + r'">'
                + sender_id
                + r"</a><br>"
                + user_message
                + r"</blockquote></mx-reply>"
                + markdown.markdown(
                    reply_message,
                    extensions=["nl2br", "tables", "fenced_code"],
                )
            )

            content = {
                "msgtype": "m.text",
                "body": body,
                "format": format,
                "formatted_body": formatted_body,
                "m.relates_to": {"m.in_reply_to": {"event_id": reply_to_event_id}},
            }

        await client.room_send(
            room_id,
            message_type="m.room.message",
            content=content,
            ignore_unverified_devices=True,
        )
        await client.room_typing(room_id, typing_state=False)