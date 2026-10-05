import os
import re
import time
import asyncio
import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (ApplicationBuilder, CommandHandler, PicklePersistence,
                          CallbackQueryHandler, MessageHandler,
                          ContextTypes, filters)

BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN تنظیم نشده. تو Railway > Service > Variables اضافه‌اش کن.")

RAILWAY_API = "https://backboard.railway.com/graphql/v2"
REGION = "europe-west4-drams3a"  # هلند (آمستردام)

# محل ذخیره اطلاعات ربات. روی Railway باید یه Volume به /data وصل کنی
DATA_DIR = os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
DATA_FILE = os.path.join(DATA_DIR, "bot_data.pickle")

# توکن Railway کاربرها فقط تو حافظه می‌مونه (ذخیره روی دیسک نمی‌شه) و بعد از ۳۰ دقیقه بی‌کاری پاک می‌شه
TOKEN_TTL = 30 * 60
TOKENS = {}  # uid -> (token, expire_time)


def set_token(uid, token):
    TOKENS[uid] = (token, time.time() + TOKEN_TTL)


def get_token(uid):
    v = TOKENS.get(uid)
    if not v:
        return None
    if v[1] < time.time():
        TOKENS.pop(uid, None)
        return None
    TOKENS[uid] = (v[0], time.time() + TOKEN_TTL)  # تمدید
    return v[0]


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


def deploy_panel(token, repo):
    """ریپو رو روی اکانت کاربر دیپلوی می‌کنه (بدون TCP)."""
    name = repo.split("/")[-1]

    # 1) پروژه
    p = gql(token, """
        mutation($n:String!){ projectCreate(input:{name:$n}){
            id environments{ edges{ node{ id } } } } }
    """, {"n": name})["projectCreate"]
    project_id = p["id"]
    env_id = p["environments"]["edges"][0]["node"]["id"]

    # 2) سرویس از ریپوی گیت‌هاب
    s = gql(token, """
        mutation($pid:String!, $repo:String!){
          serviceCreate(input:{projectId:$pid, source:{repo:$repo}}){ id } }
    """, {"pid": project_id, "repo": repo})["serviceCreate"]
    service_id = s["id"]

    # 3) ریجن هلند (اگه فیلد region قبول نشد، multiRegionConfig رو امتحان می‌کنه)
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

    # 4) دامنه عمومی HTTP
    d = gql(token, """
        mutation($s:String!, $e:String!){
          serviceDomainCreate(input:{serviceId:$s, environmentId:$e}){ domain } }
    """, {"s": service_id, "e": env_id})["serviceDomainCreate"]

    return {
        "name": name,
        "http": f"https://{d['domain']}",
        "tcp": None,
        "project_id": project_id,
        "service_id": service_id,
        "env_id": env_id,
    }


def create_tcp(token, service_id, env_id, port):
    t = gql(token, """
        mutation($s:String!, $e:String!, $port:Int!){
          tcpProxyCreate(input:{serviceId:$s, environmentId:$e,
                                applicationPort:$port}){
            id domain proxyPort } }
    """, {"s": service_id, "e": env_id, "port": port})["tcpProxyCreate"]
    return f"{t['domain']}:{t['proxyPort']}"


def delete_tcp(token, service_id, env_id):
    """همه TCP Proxy های سرویس رو حذف می‌کنه و تعدادشون رو برمی‌گردونه."""
    proxies = gql(token, """
        query($s:String!, $e:String!){
          tcpProxies(serviceId:$s, environmentId:$e){
            id domain proxyPort applicationPort } }
    """, {"s": service_id, "e": env_id})["tcpProxies"]
    for p in proxies:
        gql(token, """
            mutation($id:String!){ tcpProxyDelete(id:$id) }
        """, {"id": p["id"]})
    return len(proxies)


def delete_project(token, project_id):
    gql(token, """
        mutation($id:String!){ projectDelete(id:$id) }
    """, {"id": project_id})


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


def parse_idx(data):
    try:
        return int(data.rsplit("_", 1)[1])
    except Exception:
        return -1


# ---------------------------------------------------------------
# کیبوردهای شیشه‌ای
# ---------------------------------------------------------------
def B(text, data):
    return InlineKeyboardButton(text, callback_data=data)


def main_kb(uid):
    if not get_token(uid):
        return InlineKeyboardMarkup([[B("🔑 ثبت توکن Railway", "set_token")]])
    return InlineKeyboardMarkup([
        [B("🚀 ساخت پنل جدید", "new")],
        [B("📋 مدیریت پنل‌ها", "panels")],
        [B("🚪 خروج (پاک کردن توکن)", "logout")],
    ])


def panels_kb(panels):
    rows = [[B(f"📦 {p.get('name', 'پنل')}", f"p_{i}")] for i, p in enumerate(panels)]
    rows.append([B("🔙 بازگشت", "home")])
    return InlineKeyboardMarkup(rows)


def panel_kb(i, p):
    if p.get("tcp"):
        tcp_btn = B("🗑 حذف TCP", f"tcpdel_{i}")
    else:
        tcp_btn = B("➕ ساخت TCP", f"tcpadd_{i}")
    return InlineKeyboardMarkup([
        [tcp_btn],
        [B("🗑 حذف پنل", f"pdel_{i}")],
        [B("🔙 لیست پنل‌ها", "panels")],
    ])


def confirm_kb(ok_data, back_data):
    return InlineKeyboardMarkup([[B("✅ بله", ok_data), B("❌ لغو", back_data)]])


def panel_text(p):
    return (f"📦 {p.get('name', 'پنل')}\n\n"
            f"🌐 HTTP: {p.get('http', '-')}\n"
            f"🔌 TCP: {p.get('tcp') or 'ندارد'}\n"
            f"📍 ریجن: هلند (آمستردام)")


# ---------------------------------------------------------------
# هندلرها
# ---------------------------------------------------------------
async def ask_token(target, ctx, after):
    """از کاربر توکن می‌خواد. after = 'repo' یا 'menu' (بعد از ثبت توکن چی بشه)."""
    ctx.user_data["waiting"] = "token"
    ctx.user_data["after_token"] = after
    text = ("🔑 اول توکن Railway خودت رو بفرست (Account Token):\n"
            "Railway ← Account Settings ← Tokens")
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text)
    else:
        await target.reply_text(text)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ctx.user_data["waiting"] = None
    if not get_token(uid):
        has_panels = bool(ctx.user_data.get("panels"))
        await update.message.reply_text("به ربات پنل‌ساز خوش اومدی 👋")
        await ask_token(update.message, ctx, "menu" if has_panels else "repo")
    else:
        await update.message.reply_text("منوی اصلی 👇", reply_markup=main_kb(uid))


async def buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    await q.answer()
    d = q.data
    panels = ctx.user_data.setdefault("panels", [])

    async def need_token():
        """اگه توکن نیست از کاربر می‌خواد و None برمی‌گردونه."""
        t = get_token(uid)
        if not t:
            await ask_token(q, ctx, "menu")
        return t

    if d == "set_token":
        await ask_token(q, ctx, "repo")

    elif d == "home":
        ctx.user_data["waiting"] = None
        await q.edit_message_text("منوی اصلی 👇", reply_markup=main_kb(uid))

    elif d == "logout":
        TOKENS.pop(uid, None)
        await q.edit_message_text("🚪 توکن از حافظه پاک شد.\n"
                                  "یادت نره توکن رو از Railway هم حذف کنی.",
                                  reply_markup=main_kb(uid))

    elif d == "new":
        if not await need_token():
            return
        ctx.user_data["waiting"] = "repo"
        await q.edit_message_text("📦 لینک ریپوی گیت‌هاب رو بفرست (یا user/repo):")

    elif d == "panels":
        if not panels:
            await q.edit_message_text("هنوز پنلی نساختی.", reply_markup=main_kb(uid))
        else:
            await q.edit_message_text("📋 پنل‌های تو:", reply_markup=panels_kb(panels))

    elif d.startswith("p_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            await q.edit_message_text("❌ پیدا نشد.", reply_markup=main_kb(uid))
            return
        await q.edit_message_text(panel_text(panels[i]),
                                  reply_markup=panel_kb(i, panels[i]))

    elif d.startswith("tcpadd_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            return
        if not await need_token():
            return
        ctx.user_data["waiting"] = "port"
        ctx.user_data["port_for"] = i
        await q.edit_message_text(
            "🔌 پورت TCP رو بفرست (عدد بین 1 تا 65535).\n"
            "باید همون پورتی باشه که پنل داخل کانتینر روش گوش می‌ده.")

    elif d.startswith("tcpdel_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            return
        await q.edit_message_text(
            f"مطمئنی TCP حذف بشه؟\n\n🔌 {panels[i].get('tcp')}",
            reply_markup=confirm_kb(f"oktcpdel_{i}", f"p_{i}"))

    elif d.startswith("oktcpdel_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            return
        token = await need_token()
        if not token:
            return
        p = panels[i]
        await q.edit_message_text("⏳ در حال حذف TCP...")
        try:
            n = await asyncio.to_thread(delete_tcp, token, p["service_id"], p["env_id"])
            p["tcp"] = None
            await q.edit_message_text(f"✅ {n} تا TCP حذف شد.\n\n" + panel_text(p),
                                      reply_markup=panel_kb(i, p))
        except Exception as e:
            await q.edit_message_text(f"❌ خطا: {e}", reply_markup=panel_kb(i, p))

    elif d.startswith("pdel_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            return
        await q.edit_message_text(
            f"⚠️ کل پروژه «{panels[i].get('name', 'پنل')}» از Railway حذف می‌شه "
            f"(سرویس، دامنه و TCP). مطمئنی؟",
            reply_markup=confirm_kb(f"okpdel_{i}", f"p_{i}"))

    elif d.startswith("okpdel_"):
        i = parse_idx(d)
        if not 0 <= i < len(panels):
            return
        token = await need_token()
        if not token:
            return
        p = panels[i]
        if not p.get("project_id"):
            await q.edit_message_text("❌ شناسه پروژه ذخیره نشده. از داشبورد Railway حذفش کن.",
                                      reply_markup=main_kb(uid))
            return
        await q.edit_message_text("⏳ در حال حذف پنل...")
        try:
            await asyncio.to_thread(delete_project, token, p["project_id"])
            panels.pop(i)
            await q.edit_message_text("✅ پنل حذف شد.", reply_markup=main_kb(uid))
        except Exception as e:
            await q.edit_message_text(f"❌ خطا: {e}", reply_markup=panel_kb(i, p))


async def text_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    waiting = ctx.user_data.get("waiting")
    text = update.message.text.strip()
    panels = ctx.user_data.setdefault("panels", [])

    if waiting == "token":
        set_token(uid, text)
        ctx.user_data["waiting"] = None
        try:
            await update.message.delete()  # پاک کردن پیام حاوی توکن
        except Exception:
            pass
        if ctx.user_data.get("after_token") == "repo":
            ctx.user_data["waiting"] = "repo"
            await update.effective_chat.send_message(
                "✅ توکن ثبت شد.\n\n📦 حالا لینک ریپوی گیت‌هاب رو بفرست (یا user/repo):")
        else:
            await update.effective_chat.send_message(
                "✅ توکن ثبت شد.", reply_markup=main_kb(uid))

    elif waiting == "repo":
        repo = valid_repo(text)
        if not repo:
            await update.message.reply_text("❌ فرمت اشتباهه. مثال: user/repo")
            return
        token = get_token(uid)
        if not token:
            await ask_token(update.message, ctx, "repo")
            return
        ctx.user_data["waiting"] = None
        msg = await update.effective_chat.send_message("⏳ در حال دیپلوی روی اکانت تو...")
        try:
            p = await asyncio.to_thread(deploy_panel, token, repo)
            panels.append(p)
            i = len(panels) - 1
            await msg.edit_text(
                "✅ پنل ساخته شد. چند دقیقه صبر کن تا بیلد تموم شه.\n\n" + panel_text(p),
                reply_markup=panel_kb(i, p))
        except Exception as e:
            await msg.edit_text(f"❌ خطا: {e}", reply_markup=main_kb(uid))

    elif waiting == "port":
        port = valid_port(text)
        if not port:
            await update.message.reply_text("❌ پورت نامعتبره. یه عدد بین 1 تا 65535 بفرست.")
            return
        i = ctx.user_data.get("port_for", -1)
        token = get_token(uid)
        if not token:
            await ask_token(update.message, ctx, "menu")
            return
        if not 0 <= i < len(panels):
            ctx.user_data["waiting"] = None
            await update.message.reply_text("❌ پنل پیدا نشد.", reply_markup=main_kb(uid))
            return
        ctx.user_data["waiting"] = None
        p = panels[i]
        msg = await update.effective_chat.send_message("⏳ در حال ساخت TCP...")
        try:
            p["tcp"] = await asyncio.to_thread(
                create_tcp, token, p["service_id"], p["env_id"], port)
            await msg.edit_text("✅ TCP ساخته شد.\n\n" + panel_text(p),
                                reply_markup=panel_kb(i, p))
        except Exception as e:
            await msg.edit_text(f"❌ خطا: {e}", reply_markup=panel_kb(i, p))


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
