import os
import re
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, PicklePersistence,
                          CallbackQueryHandler, MessageHandler,
                          ContextTypes, filters)

BOT_TOKEN = os.environ["BOT_TOKEN"]
RAILWAY_API = "https://backboard.railway.com/graphql/v2"

DEFAULT_REPO = "USERNAME/panel-repo"   # ریپوی پیش‌فرض پنل تو
REGION = "europe-west4-drams3a"        # هلند (آمستردام)

# محل ذخیره اطلاعات ربات. روی Railway باید یه Volume به /data وصل کنی
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
DATA_FILE = os.path.join(DATA_DIR, "bot_data.pickle")

# توکن Railway کاربرها فقط تو حافظه می‌مونه و عمداً روی دیسک ذخیره نمی‌شه
TOKENS = {}


# ---------------------------------------------------------------
# Railway GraphQL
# ---------------------------------------------------------------
def gql(token, query, variables=None):
    r = requests.post(
        RAILWAY_API,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    data = r.json()
    if "errors" in data:
        raise Exception(data["errors"][0]["message"])
    return data["data"]


def deploy_panel(token, repo, port, env_vars=None):
    # 1) پروژه
    p = gql(token, """
        mutation($n:String!){ projectCreate(input:{name:$n}){
            id environments{ edges{ node{ id } } } } }
    """, {"n": "my-panel"})["projectCreate"]
    project_id = p["id"]
    env_id = p["environments"]["edges"][0]["node"]["id"]

    # 2) سرویس از ریپوی گیت‌هاب
    s = gql(token, """
        mutation($pid:String!, $repo:String!){
          serviceCreate(input:{projectId:$pid, source:{repo:$repo}}){ id } }
    """, {"pid": project_id, "repo": repo})["serviceCreate"]
    service_id = s["id"]

    # 3) متغیرهای محیطی
    if env_vars:
        gql(token, """
            mutation($i:VariableCollectionUpsertInput!){
              variableCollectionUpsert(input:$i) }
        """, {"i": {"projectId": project_id, "environmentId": env_id,
                    "serviceId": service_id, "variables": env_vars}})

    # 4) ریجن هلند (اگه فیلد region قبول نشد، multiRegionConfig رو امتحان می‌کنه)
    region_mutation = """
        mutation($s:String!, $e:String!, $i:ServiceInstanceUpdateInput!){
          serviceInstanceUpdate(serviceId:$s, environmentId:$e, input:$i) }
    """
    try:
        gql(token, region_mutation,
            {"s": service_id, "e": env_id, "i": {"region": REGION}})
    except Exception:
        gql(token, region_mutation,
            {"s": service_id, "e": env_id,
             "i": {"multiRegionConfig": {REGION: {"numReplicas": 1}}}})

    # 5) دامنه عمومی HTTP
    d = gql(token, """
        mutation($s:String!, $e:String!){
          serviceDomainCreate(input:{serviceId:$s, environmentId:$e}){ domain } }
    """, {"s": service_id, "e": env_id})["serviceDomainCreate"]

    # 6) TCP Proxy روی پورت دلخواه کاربر
    t = gql(token, """
        mutation($s:String!, $e:String!, $port:Int!){
          tcpProxyCreate(input:{serviceId:$s, environmentId:$e,
                                applicationPort:$port}){
            domain proxyPort } }
    """, {"s": service_id, "e": env_id, "port": port})["tcpProxyCreate"]

    return {
        "http": f"https://{d['domain']}",
        "tcp": f"{t['domain']}:{t['proxyPort']}",
    }


# ---------------------------------------------------------------
# اعتبارسنجی ورودی‌ها
# ---------------------------------------------------------------
def valid_repo(text):
    m = re.match(r"^(?:https?://github\.com/)?([\w.-]+/[\w.-]+?)(?:\.git)?/?$",
                 text.strip())
    return m.group(1) if m else None


def valid_port(text):
    text = text.strip()
    if not text.isdigit():
        return None
    p = int(text)
    return p if 1 <= p <= 65535 else None


# ---------------------------------------------------------------
# کیبورد شیشه‌ای
# ---------------------------------------------------------------
def main_kb(ctx, uid):
    token_ok = "✅" if TOKENS.get(uid) else "❌"
    repo = ctx.user_data.get("repo", DEFAULT_REPO)
    port = ctx.user_data.get("port")
    port_txt = f"✅ پورت TCP: {port}" if port else "❌ پورت TCP: تنظیم نشده"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{token_ok} ثبت توکن Railway", callback_data="set_token")],
        [InlineKeyboardButton(f"📦 ریپو: {repo}", callback_data="set_repo")],
        [InlineKeyboardButton(port_txt, callback_data="set_port")],
        [InlineKeyboardButton("🚀 ساخت پنل", callback_data="create")],
    ])


# ---------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("به ربات پنل‌ساز خوش اومدی 👇",
                                    reply_markup=main_kb(ctx, uid))


async def buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()

    if q.data == "set_token":
        ctx.user_data["waiting"] = "token"
        await q.edit_message_text("توکن Railway خودت رو بفرست (Account Token):")

    elif q.data == "set_repo":
        ctx.user_data["waiting"] = "repo"
        await q.edit_message_text("لینک ریپو یا user/repo رو بفرست:")

    elif q.data == "set_port":
        ctx.user_data["waiting"] = "port"
        await q.edit_message_text(
            "پورت پنلت رو بفرست (عدد بین 1 تا 65535).\n"
            "باید همون پورتی باشه که پنل داخل کانتینر روش گوش می‌ده.")

    elif q.data == "create":
        token = TOKENS.get(uid)
        if not token:
            await q.edit_message_text("اول توکن رو ثبت کن.",
                                      reply_markup=main_kb(ctx, uid))
            return

        port = ctx.user_data.get("port")
        if not port:
            await q.edit_message_text("اول پورت TCP رو دستی ثبت کن.",
                                      reply_markup=main_kb(ctx, uid))
            return

        repo = ctx.user_data.get("repo", DEFAULT_REPO)

        await q.edit_message_text("⏳ در حال ساخت پنل روی اکانت تو...")
        try:
            res = await asyncio.to_thread(
                deploy_panel, token, repo, port, {"PORT": str(port)})
            await q.edit_message_text(
                f"✅ پنل ساخته شد:\n\n"
                f"🌐 HTTP: {res['http']}\n"
                f"🔌 TCP: {res['tcp']}\n"
                f"📍 ریجن: هلند (آمستردام)\n\n"
                f"چند دقیقه صبر کن تا بیلد تموم شه.\n"
                f"⚠️ بعد از کار، توکن رو از Railway حذف کن.",
                reply_markup=main_kb(ctx, uid))
        except Exception as e:
            await q.edit_message_text(f"❌ خطا: {e}", reply_markup=main_kb(ctx, uid))
        finally:
            # توکن بعد از استفاده پاک می‌شه
            TOKENS.pop(uid, None)


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    waiting = ctx.user_data.get("waiting")
    text = update.message.text.strip()

    if waiting == "token":
        TOKENS[uid] = text
        ctx.user_data["waiting"] = None
        try:
            await update.message.delete()  # پاک کردن پیام حاوی توکن
        except Exception:
            pass
        await update.effective_chat.send_message(
            "✅ توکن ثبت شد.", reply_markup=main_kb(ctx, uid))

    elif waiting == "repo":
        repo = valid_repo(text)
        if not repo:
            await update.message.reply_text("❌ فرمت اشتباهه. مثال: user/repo")
            return
        ctx.user_data["repo"] = repo
        ctx.user_data["waiting"] = None
        await update.effective_chat.send_message(
            "✅ ریپو ثبت شد.", reply_markup=main_kb(ctx, uid))

    elif waiting == "port":
        port = valid_port(text)
        if not port:
            await update.message.reply_text(
                "❌ پورت نامعتبره. یه عدد بین 1 تا 65535 بفرست.")
            return
        ctx.user_data["port"] = port
        ctx.user_data["waiting"] = None
        await update.effective_chat.send_message(
            f"✅ پورت {port} ثبت شد.", reply_markup=main_kb(ctx, uid))


def main():
    persistence = PicklePersistence(filepath=DATA_FILE, update_interval=5)
    app = (ApplicationBuilder()
           .token(BOT_TOKEN)
           .persistence(persistence)
           .build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()


if __name__ == "__main__":
    main()
