# Copyright 2022 d4n13l3k00.
# SPDX-License-Identifier: 	AGPL-3.0-or-later

import contextlib
import hashlib
import io
import os
import sys
import time
import traceback
from pathlib import Path
from typing import *

import config
import models
import speech_recognition as sr
import utils
from fastapi import Cookie, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydub import AudioSegment
from telethon import TelegramClient, errors, functions, types

config = config.Config()
config.access_cookie = (
    hashlib.sha256(os.urandom(32)).hexdigest()
    if not hasattr(config, "access_cookie")
    else config.access_cookie
)

if __name__ == "__main__":
    print("For start: uvicorn main:app --host 0.0.0.0 --port 8888")
    sys.exit(0)

templates = Jinja2Templates(directory="templates")

app = FastAPI(title="Tapkofon API", version="1.0")
path = Path.cwd().parent / "session"
if not path.exists():
    path.mkdir(parents=True)
user = TelegramClient("../session/session", config.api_id, config.api_hash)
user.parse_mode = "html"

##### / Работа с подключением / #####
@app.middleware("http")
async def add_process_time_header(request: Request, call_next):
    if request.url.path != "/pass":
        if config.passwd:
            if (
                "access_token" not in request.cookies
                or request.cookies["access_token"] != config.access_cookie
            ):
                return RedirectResponse("/pass")
        else:
            response = await call_next(request)
            response.set_cookie("access_token", config.access_cookie)
            return response
    response = await call_next(request)
    return response


@app.get("/logout", description="Деавторизоваться", response_class=HTMLResponse)
async def logout():
    await user.log_out()
    return templates.get_template("auth/logout.html").render()


@app.get(
    "/auth_old", description="Авторизация через терминал", response_class=HTMLResponse
)
async def auth_old():
    await user.start()
    me = await user.get_me()
    return templates.get_template("auth/authorized.html").render(me=me)


@app.get("/pass", description="Код-пароль доступа", response_class=HTMLResponse)
async def passwd(
    password: Optional[str] = None, access_token: Optional[str] = Cookie(None)
):
    if not config.passwd:
        r = RedirectResponse("/")
        r.set_cookie("access_token", config.access_cookie)
        return r
    if (password == config.passwd) or (access_token == config.access_cookie):
        r = RedirectResponse("/")
        r.set_cookie("access_token", config.access_cookie)
        return r
    if not password:
        return templates.get_template("pass/pass.html").render()

    return HTMLResponse(
        templates.get_template("pass/pass.html").render(msg="Неверный код/куки")
    )


@app.get("/lock", description="Заблокировать", response_class=HTMLResponse)
async def lock():
    r = RedirectResponse("/pass")
    r.delete_cookie("access_token")
    return r


@app.get("/auth", description="Веб-Авторизация", response_class=HTMLResponse)
async def auth(
    phone: Optional[str] = None, code: Optional[str] = None, tfa: Optional[str] = None
):
    if not phone:
        await user.connect()
        return templates.get_template("auth/auth.html").render()
    if not code:
        try:
            await user.sign_in(phone)
            return templates.get_template("auth/auth.html").render(
                phone=phone, code=code, tfa=tfa
            )
        except errors.FloodWaitError as ex:
            tm = time.strftime("%Hh:%Mm:%Ss", time.gmtime(ex.seconds))
            return templates.get_template("auth/auth.html").render(
                phone=phone, msg=f"Флудвейт! Подождите {tm}"
            )
        except Exception as ex:
            print(traceback.format_exc())
            return templates.get_template("auth/auth.html").render(
                phone=phone, code=code, msg="<br>".join(ex.args)
            )
    try:
        if tfa:
            await user.sign_in(phone, password=tfa)
        else:
            await user.sign_in(code=code)
        await user.sign_in(phone)
        me = await user.get_me()
        return templates.get_template("auth/authorized.html").render(me=me)
    except errors.SessionPasswordNeededError as ex:
        return templates.get_template("auth/auth.html").render(
            phone=phone, msg="Введите 2FA пароль"
        )
    except errors.PhoneCodeInvalidError as ex:
        return templates.get_template("auth/auth.html").render(
            phone=phone, msg="Неверный код"
        )
    except errors.PhoneCodeExpiredError as ex:
        await user.send_code_request(phone, force_sms=True)
        return templates.get_template("auth/auth.html").render(
            phone=phone, msg="Время кода истекло"
        )
    except errors.FloodWaitError as ex:
        tm = time.strftime("%Hh:%Mm:%Ss", time.gmtime(ex.seconds))
        return templates.get_template("auth/auth.html").render(
            phone=phone, msg=f"Флудвейт! Подождите {tm}"
        )
    except Exception as ex:
        return templates.get_template("auth/auth.html").render(
            phone=phone, code=code, msg="<br>".join(ex.args)
        )


##### / Список чатов / #####


@app.get("/", description="Список чатов", response_class=HTMLResponse)
async def get_dialogs():
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    dialogs = await user.get_dialogs()
    chats = [
        models.Chat(id=chat.id, title=chat.title, unread=chat.unread_count)
        for chat in dialogs
    ]
    return templates.get_template("chats.html").render(
        chats=chats, is_passwd=bool(config.passwd)
    )


##### / Чат / #####
@app.get("/chat/{id}", description="Чат", response_class=HTMLResponse)
async def chat(id: str, page: Optional[int] = 0):
    # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        chat = await user.get_entity(id)
        await user.conversation(chat).mark_read()
        messages = await user.get_messages(id, limit=10, add_offset=10 * page)
        msgs = []
        for m in messages:
            m: types.Message
            r = await m.get_reply_message()
            reply = None
            if r:
                name = (
                    r.sender.title
                    if hasattr(r.sender, "title")
                    else r.sender.first_name
                )
                if r.file:
                    rfile = models.MessageMedia(
                        type=r.file.mime_type,
                        typ=r.file.mime_type.split("/")[0],
                        size=utils.humanize(r.file.size),
                        filename=r.file.name,
                    )
                else:
                    rfile = None
                reply = models.ReplyMessage(
                    name=name,
                    id=r.id,
                    file=rfile,
                    text=utils.replacing_text(r.text) if r.text else None,
                )
            if m.file:
                file = models.MessageMedia(
                    type=m.file.mime_type,
                    typ=m.file.mime_type.split("/")[0],
                    size=utils.humanize(m.file.size),
                    filename=m.file.name,
                )
            else:
                file = None
            msgs.append(
                models.Message(
                    id=m.id,
                    sender=m.sender,
                    text=utils.replacing_text(m.text) if m.text else None,
                    file=file,
                    reply=reply,
                    mentioned=m.mentioned,
                    date=m.date.strftime("%Y-%m-%d %H:%M:%S"),
                    out=m.out,
                )
            )
        return templates.get_template("chat.html").render(
            messages=msgs, chat=chat, page=page
        )
    except Exception as ex:
        return templates.get_template("error.html").render(error="<br>".join(ex.args))


##### / Реплай / #####
@app.get(
    "/chat/{id}/reply/{msg_id}",
    description="Реплай на сообщение",
    response_class=HTMLResponse,
)
async def reply_to_msg(id: str, msg_id: int):
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        return templates.get_template("reply.html").render(chat=id, id=msg_id)
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


##### / Отправка сообщения / #####
@app.post(
    "/chat/{id}/send_message",
    description="API Отправка сообщения",
    response_class=HTMLResponse,
)
async def send_message(
    id: str,
    text: Optional[str] = Form(None),
    reply_to: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
):  # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        chat = await user.get_entity(id)
        if file and file.file.read():
            file.file.seek(0)
            f = io.BytesIO(file.file.read())
            f.name = file.filename
            await user.send_file(chat, f, caption=text, reply_to=reply_to)
        else:
            await user.send_message(chat, text, reply_to=reply_to)
        return templates.get_template("success.html").render(
            id=id, text="Сообщение отправлено"
        )
    except Exception as ex:
        return templates.get_template("error.html").render(error="<br>".join(ex.args))


##### / Работа с сообщениями / #####
@app.get(
    "/chat/{id}/edit/{msg_id}",
    description="Изменить сообщение",
    response_class=HTMLResponse,
)
async def edit(id: str, msg_id: int):  # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        msg = await user.get_messages(id, ids=msg_id)
        if msg:
            msg: types.Message
            return templates.get_template("edit.html").render(
                chat=id, id=msg.id, text=msg.text
            )
        return HTMLResponse(
            templates.get_template("error.html").render(
                error="Такого сообщения не существует"
            )
        )
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


@app.post(
    "/chat/{id}/edit_message",
    description="API Изменить соообщение",
    response_class=HTMLResponse,
)
async def edit_message(id: str, msg_id: int = Form(...), text: str = Form(...)):
    # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        msg = await user.get_messages(id, ids=msg_id)
        if msg:
            msg: types.Message
            await msg.edit(text)
            return templates.get_template("success.html").render(
                id=id, text="Сообщение изменено"
            )
        return HTMLResponse(
            templates.get_template("error.html").render(
                error="Такого сообщения не существует"
            )
        )
    except Exception as ex:
        return templates.get_template("error.html").render(error="<br>".join(ex.args))


@app.get(
    "/chat/{id}/delete/{msg_id}",
    description="Удаление сообщения",
    response_class=HTMLResponse,
)
async def delete_message(id: str, msg_id: int):
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        msg = await user.get_messages(id, ids=msg_id)
        if msg:
            msg: types.Message
            await msg.delete()
            return templates.get_template("success.html").render(
                id=id, text="Сообщение удалено"
            )
        return HTMLResponse(
            templates.get_template("error.html").render(
                error="Такого сообщения не существует"
            )
        )
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


##### / Загрузка и стримминг файла из кеша / #####
@app.get("/chat/{id}/download/{msg_id}", description="Загрузка файла")
async def download(id: str, msg_id: int):
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        msg = await user.get_messages(id, ids=msg_id)
        if not msg or not msg.file:
            return HTMLResponse(
                templates.get_template("error.html").render(
                    error="Такого сообщения не существует"
                )
            )
        msg: types.Message
        if (
            os.path.isdir(f"cache/{id}/{msg_id}")
            and os.listdir(f"cache/{id}/{msg_id}/") != []
        ):
            file = f"cache/{id}/{msg_id}/" + os.listdir(f"cache/{id}/{msg_id}/")[0]
        else:
            for i in ["cache/", f"cache/{id}/", f"cache/{id}/{msg_id}/"]:
                if not os.path.isdir(i):
                    os.mkdir(i)
            if msg.file.mime_type.split("/")[0] == "audio" and msg.file.ext != ".mp3":
                file = f"cache/{id}/{msg_id}/audio.mp3"
                m_ = io.BytesIO(await msg.download_media(bytes))
                m_.name = "audio.wav"
                AudioSegment.from_file(m_).export(file)
            elif msg.file.mime_type.split("/")[0] == "image":
                file = f"cache/{id}/{msg_id}/image.{config.pic_format}"
                m_ = io.BytesIO(await msg.download_media(bytes))
                m_.name = "pic.png"
                im = Image.open(m_).convert("RGBA")
                im.load()
                bg = Image.new("RGB", im.size, (255,) * 3)
                bg.paste(im, mask=im.split()[3])
                bg.thumbnail((config.pic_max_size,) * 2, 1)
                bg.save(file, config.pic_format, quality=config.pic_quality)
            else:
                path = f"cache/{id}/{msg_id}/{msg.file.name}"
                file = await msg.download_media(path)
        stream = open(file, mode="rb")
        return StreamingResponse(stream, media_type=msg.file.mime_type)
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


@app.get("/chat/{id}/recognize/{msg_id}", description="Загрузка файла")
async def recognize(id: str, msg_id: int):  # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        msg = await user.get_messages(id, ids=msg_id)
        if not msg or not msg.file:
            return HTMLResponse(
                templates.get_template("error.html").render(
                    error="Такого сообщения не существует"
                )
            )
        msg: types.Message
        if (
            os.path.isdir(f"cache/{id}/{msg_id}")
            and os.listdir(f"cache/{id}/{msg_id}/") != []
        ):
            file = f"cache/{id}/{msg_id}/" + os.listdir(f"cache/{id}/{msg_id}/")[0]
        else:
            for i in ["cache/", f"cache/{id}/", f"cache/{id}/{msg_id}/"]:
                if not os.path.isdir(i):
                    os.mkdir(i)
            if msg.file.mime_type.split("/")[0] == "audio" and msg.file.ext != ".mp3":
                file = f"cache/{id}/{msg_id}/audio.mp3"
                m_ = io.BytesIO(await msg.download_media(bytes))
                m_.name = "audio.wav"
                AudioSegment.from_file(m_).export(file)
            else:
                path = f"cache/{id}/{msg_id}/{msg.file.name}"
                file = await msg.download_media(path)
        if not os.path.isfile(f"{file}.wav"):
            song = AudioSegment.from_file(file)
            song.export(f"{file}.wav", format="wav")
        r = sr.Recognizer()
        with sr.AudioFile(f"{file}.wav") as source:
            audio_data = r.record(source)
            text = r.recognize_google(audio_data, language=config.recognize_lang)

        r = sr.Recognizer()
        with sr.AudioFile(f"{file}.wav") as source:
            audio_data = r.record(source)
            text = r.recognize_google(audio_data, language="ru-RU")
        return HTMLResponse(
            templates.get_template("voice_recognized.html").render(id=id, text=text)
        )
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


##### / Юзер / #####


@app.get("/user/{id}/avatar", description="Аватарка пользователя")
async def user_avatar(id: str):  # sourcery skip: avoid-builtin-shadow
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        user_ = await user.get_entity(id)
        out = io.BytesIO()
        out.name = f"..{config.pic_format}"
        im = Image.open(io.BytesIO(await user.download_profile_photo(user_, bytes)))
        im.thumbnail((config.pic_avatar_max_size,) * 2, 1)
        im.save(out, format=config.pic_format)
        out.seek(0)
        return StreamingResponse(out)
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


@app.get("/user/{id}", description="Профиль пользователя", response_class=HTMLResponse)
async def user_info(id: str):
    if not user.is_connected():
        await user.connect()
    if not await user.is_user_authorized():
        return templates.get_template("auth/not_authorized.html").render()
    try:
        with contextlib.suppress(Exception):
            id = int(id)
        user_ = await user.get_entity(id)
        user_full = await user(functions.users.GetFullUserRequest(id=id))
        statuses = {
            types.UserStatusEmpty: "Ничего",
            types.UserStatusOnline: "Онлайн",
            types.UserStatusOffline: "Оффлайн",
            types.UserStatusRecently: "Недавно",
            types.UserStatusLastWeek: "Был на этой неделе",
            types.UserStatusLastMonth: "Был в этом месяце",
        }
        return HTMLResponse(
            templates.get_template("user.html").render(
                user=user_,
                user_full=user_full,
                status=statuses[
                    next(filter(lambda x: isinstance(user_.status, x), statuses))
                ],
            )
        )
    except Exception as ex:
        return HTMLResponse(
            templates.get_template("error.html").render(error="<br>".join(ex.args))
        )


##### / Кеш / #####
@app.get("/cache", description="Кеш", response_class=HTMLResponse)
async def cache():
    try:
        size = utils.humanize(utils.get_size("cache"))
    except Exception:
        size = "0.0B"
    return templates.get_template("cache.html").render(size=size)


@app.get("/cache/clear", description="Очистить кеш", response_class=HTMLResponse)
async def cache_clear():
    with contextlib.suppress(Exception):
        utils.clear_dir("cache")
    return RedirectResponse("/cache")


@app.get("/cache/list", description="Дерево кеша", response_class=HTMLResponse)
async def cache_list():
    if not os.path.isdir("cache") or os.listdir("cache") == []:
        return "Кеш пустой"
    paths = utils.DisplayablePath.make_tree(Path("cache"))
    var = "".join(path.displayable() + "\n" for path in paths)
    return var.replace("\n", "<br>").replace(" ", " ")


@app.get("/about", description="О проекте", response_class=HTMLResponse)
async def about():
    return templates.get_template("about.html").render()
