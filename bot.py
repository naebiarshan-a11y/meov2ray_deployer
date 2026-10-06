import os
import re
import time
import asyncio
import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    PicklePersistence,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

RAILWAY_API = "https://backboard.railway.com/graphql/v2"

REGION = "europe-west4-drams3a"

DATA_DIR = os.environ.get(
    "DATA_DIR",
    "/data" if os.path.isdir("/data") else "."
)

DATA_FILE = os.path.join(DATA_DIR, "bot_data.pickle")

MAX_TCPS = 3


# =========================================================
# RAILWAY GRAPHQL
# =========================================================

def railway_request(token, query, variables=None):
    try:
        r = requests.post(
            RAILWAY_API,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={
                "query": query,
                "variables": variables or {},
            },
            timeout=30,
        )

        data = r.json()

        if data.get("errors"):
            return None, data["errors"]

        return data.get("data"), None

    except Exception as e:
        return None, [{"message": str(e)}]


# =========================================================
# TOKEN MANAGEMENT
# =========================================================

def get_tokens(user_data):
    return user_data.setdefault("railway_tokens", {})


def get_active_token_id(user_data):
    return user_data.get("active_token_id")


def get_active_token(user_data):
    token_id = get_active_token_id(user_data)

    if not token_id:
        return None

    item = get_tokens(user_data).get(token_id)

    if not item:
        return None

    return item.get("token")


def add_token(user_data, token):
    tokens = get_tokens(user_data)

    number = len(tokens) + 1

    token_id = str(int(time.time() * 1000000))

    tokens[token_id] = {
        "name": f"توکن {number}",
        "token": token,
    }

    user_data["active_token_id"] = token_id

    return token_id


def delete_token(user_data, token_id):
    tokens = get_tokens(user_data)

    if token_id not in tokens:
        return False

    del tokens[token_id]

    if user_data.get("active_token_id") == token_id:
        if tokens:
            user_data["active_token_id"] = next(iter(tokens))
        else:
            user_data.pop("active_token_id", None)

    return True


def get_token_for_panel(user_data, panel):
    token_id = panel.get("token_id")

    if not token_id:
        return None

    item = get_tokens(user_data).get(token_id)

    if not item:
        return None

    return item.get("token")


# =========================================================
# PANEL STORAGE
# =========================================================

def get_panels(user_data):
    panels = user_data.setdefault("panels", [])

    for panel in panels:
        normalize_panel(panel)

    return panels


def normalize_panel(panel):
    """
    تبدیل ساختار قدیمی TCP به ساختار جدید.
    """

    if "tcps" not in panel:
        panel["tcps"] = []

        old_tcp = panel.get("tcp")

        if old_tcp:
            panel["tcps"].append({
                "id": None,
                "domain": None,
                "proxy_port": None,
                "application_port": panel.get("port"),
                "address": old_tcp,
            })

    panel.pop("tcp", None)


# =========================================================
# GITHUB
# =========================================================

def default_branch(repo):
    try:
        r = requests.get(
            f"https://api.github.com/repos/{repo}",
            timeout=20,
        )

        if r.status_code != 200:
            return "main"

        return r.json().get("default_branch", "main")

    except Exception:
        return "main"


# =========================================================
# RAILWAY WORKSPACE
# =========================================================

def get_workspace_id(token):
    query = """
    query {
        me {
            workspaces {
                id
                name
            }
        }
    }
    """

    data, error = railway_request(token, query)

    if error:
        return None

    try:
        workspaces = data["me"]["workspaces"]

        if not workspaces:
            return None

        return workspaces[0]["id"]

    except Exception:
        return None


# =========================================================
# DEPLOY PANEL
# =========================================================

def deploy_panel(token, repo, port):
    workspace_id = get_workspace_id(token)

    if not workspace_id:
        raise Exception(
            "توکن Railway معتبر نیست یا Workspace پیدا نشد."
        )

    branch = default_branch(repo)

    # -----------------------------------------
    # CREATE PROJECT
    # -----------------------------------------

    project_mutation = """
    mutation ProjectCreate($input: ProjectCreateInput!) {
        projectCreate(input: $input) {
            project {
                id
                name
            }
        }
    }
    """

    project_name = (
        repo.split("/")[-1]
        .replace("_", "-")
        .replace(".", "-")
    )

    data, error = railway_request(
        token,
        project_mutation,
        {
            "input": {
                "name": project_name,
                "workspaceId": workspace_id,
            }
        },
    )

    if error:
        raise Exception(error[0].get("message", "خطا در ساخت Project"))

    project = data["projectCreate"]["project"]

    project_id = project["id"]

    # -----------------------------------------
    # CREATE SERVICE
    # -----------------------------------------

    service_mutation = """
    mutation ServiceCreate($input: ServiceCreateInput!) {
        serviceCreate(input: $input) {
            service {
                id
                name
            }
        }
    }
    """

    service_input = {
        "projectId": project_id,
        "name": "app",
        "source": {
            "repo": repo,
            "branch": branch,
        },
    }

    data, error = railway_request(
        token,
        service_mutation,
        {
            "input": service_input,
        },
    )

    if error:
        raise Exception(
            error[0].get("message", "خطا در ساخت Service")
        )

    service = data["serviceCreate"]["service"]

    service_id = service["id"]

    # -----------------------------------------
    # GET ENVIRONMENT
    # -----------------------------------------

    env_query = """
    query Project($id: String!) {
        project(id: $id) {
            environments {
                edges {
                    node {
                        id
                        name
                    }
                }
            }
        }
    }
    """

    data, error = railway_request(
        token,
        env_query,
        {
            "id": project_id,
        },
    )

    if error:
        raise Exception(
            error[0].get("message", "خطا در دریافت Environment")
        )

    env_id = (
        data["project"]["environments"]["edges"][0]["node"]["id"]
    )

    # -----------------------------------------
    # SET PORT
    # -----------------------------------------

    try:
        variable_mutation = """
        mutation VariableUpsert(
            $input: VariableUpsertInput!
        ) {
            variableUpsert(input: $input)
        }
        """

        railway_request(
            token,
            variable_mutation,
            {
                "input": {
                    "environmentId": env_id,
                    "serviceId": service_id,
                    "name": "PORT",
                    "value": str(port),
                }
            },
        )

    except Exception:
        pass

    # -----------------------------------------
    # REGION
    # -----------------------------------------

    region_ok = False

    try:
        region_mutation = """
        mutation ServiceInstanceUpdate(
            $input: ServiceInstanceUpdateInput!
        ) {
            serviceInstanceUpdate(input: $input)
        }
        """

        _, err = railway_request(
            token,
            region_mutation,
            {
                "input": {
                    "serviceId": service_id,
                    "environmentId": env_id,
                    "multiRegionConfig": {
                        REGION: 1
                    },
                }
            },
        )

        if not err:
            region_ok = True

    except Exception:
        pass

    # -----------------------------------------
    # DOMAIN
    # -----------------------------------------

    domain_mutation = """
    mutation ServiceDomainCreate(
        $input: ServiceDomainCreateInput!
    ) {
        serviceDomainCreate(input: $input) {
            domain {
                id
                domain
            }
        }
    }
    """

    data, error = railway_request(
        token,
        domain_mutation,
        {
            "input": {
                "serviceId": service_id,
                "environmentId": env_id,
            }
        },
    )

    http_domain = None

    if not error:
        try:
            http_domain = data[
                "serviceDomainCreate"
            ]["domain"]["domain"]
        except Exception:
            pass

    # -----------------------------------------
    # DEPLOY
    # -----------------------------------------

    try:
        deploy_mutation = """
        mutation ServiceInstanceDeploy(
            $serviceId: String!,
            $environmentId: String!
        ) {
            serviceInstanceDeploy(
                serviceId: $serviceId,
                environmentId: $environmentId
            )
        }
        """

        railway_request(
            token,
            deploy_mutation,
            {
                "serviceId": service_id,
                "environmentId": env_id,
            },
        )

    except Exception:
        pass

    if http_domain:
        http_url = (
            http_domain
            if http_domain.startswith("http")
            else f"https://{http_domain}"
        )
    else:
        http_url = None

    return {
        "name": project_name,
        "repo": repo,
        "http": http_url,
        "port": port,
        "region": REGION,
        "region_ok": region_ok,
        "project_id": project_id,
        "service_id": service_id,
        "env_id": env_id,
        "tcps": [],
        "created_at": int(time.time()),
    }


# =========================================================
# TCP
# =========================================================

def create_tcp(token, service_id, env_id, port):
    mutation = """
    mutation TcpProxyCreate(
        $input: TCPProxyCreateInput!
    ) {
        tcpProxyCreate(input: $input) {
            tcpProxy {
                id
                domain
                proxyPort
                applicationPort
            }
        }
    }
    """

    data, error = railway_request(
        token,
        mutation,
        {
            "input": {
                "serviceId": service_id,
                "environmentId": env_id,
                "applicationPort": int(port),
            }
        },
    )

    if error:
        raise Exception(
            error[0].get(
                "message",
                "خطا در ساخت TCP"
            )
        )

    tcp = data["tcpProxyCreate"]["tcpProxy"]

    return {
        "id": tcp.get("id"),
        "domain": tcp.get("domain"),
        "proxy_port": tcp.get("proxyPort"),
        "application_port": tcp.get("applicationPort"),
        "address": (
            f'{tcp.get("domain")}:{tcp.get("proxyPort")}'
        ),
    }


def get_tcps(token, service_id, env_id):
    query = """
    query TcpProxies(
        $serviceId: String!,
        $environmentId: String!
    ) {
        tcpProxies(
            serviceId: $serviceId,
            environmentId: $environmentId
        ) {
            id
            domain
            proxyPort
            applicationPort
        }
    }
    """

    data, error = railway_request(
        token,
        query,
        {
            "serviceId": service_id,
            "environmentId": env_id,
        },
    )

    if error:
        return []

    result = []

    for tcp in data.get("tcpProxies", []):
        result.append({
            "id": tcp.get("id"),
            "domain": tcp.get("domain"),
            "proxy_port": tcp.get("proxyPort"),
            "application_port": tcp.get("applicationPort"),
            "address": (
                f'{tcp.get("domain")}:{tcp.get("proxyPort")}'
            ),
        })

    return result


def delete_tcp_by_id(token, tcp_id):
    mutation = """
    mutation TcpProxyDelete($id: String!) {
        tcpProxyDelete(id: $id)
    }
    """

    data, error = railway_request(
        token,
        mutation,
        {
            "id": tcp_id,
        },
    )

    if error:
        raise Exception(
            error[0].get(
                "message",
                "خطا در حذف TCP"
            )
        )

    return True


# =========================================================
# DELETE PROJECT
# =========================================================

def delete_project(token, project_id):
    mutation = """
    mutation ProjectDelete($id: String!) {
        projectDelete(id: $id)
    }
    """

    data, error = railway_request(
        token,
        mutation,
        {
            "id": project_id,
        },
    )

    if error:
        raise Exception(
            error[0].get(
                "message",
                "خطا در حذف پنل"
            )
        )

    return True


# =========================================================
# UI
# =========================================================

def main_menu(user_data):
    buttons = [
        [
            InlineKeyboardButton(
                "🚀 ساخت پنل جدید",
                callback_data="new",
            )
        ],
        [
            InlineKeyboardButton(
                "📋 مدیریت پنل‌ها",
                callback_data="panels",
            )
        ],
        [
            InlineKeyboardButton(
                "🔑 مدیریت توکن‌ها",
                callback_data="tokens",
            )
        ],
    ]

    return InlineKeyboardMarkup(buttons)


def tokens_keyboard(user_data):
    tokens = get_tokens(user_data)

    buttons = []

    for token_id, item in tokens.items():
        active = (
            "🟢 "
            if token_id == user_data.get("active_token_id")
            else ""
        )

        buttons.append([
            InlineKeyboardButton(
                f"{active}{item.get('name', 'توکن')}",
                callback_data=f"token_select_{token_id}",
            ),
            InlineKeyboardButton(
                "🗑",
                callback_data=f"token_delete_{token_id}",
            ),
        ])

    buttons.append([
        InlineKeyboardButton(
            "➕ ثبت توکن جدید",
            callback_data="token_add",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 بازگشت",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def panels_keyboard(user_data):
    panels = get_panels(user_data)

    buttons = []

    if not panels:
        buttons.append([
            InlineKeyboardButton(
                "📭 هیچ پنلی ثبت نشده",
                callback_data="noop",
            )
        ])

    for i, panel in enumerate(panels):
        normalize_panel(panel)

        tcps = panel.get("tcps", [])

        buttons.append([
            InlineKeyboardButton(
                f"📦 {panel.get('name', f'Panel {i+1}')}",
                callback_data=f"p_{i}",
            )
        ])

        # TCP buttons directly inside panel management
        tcp_buttons = [
            InlineKeyboardButton(
                f"➕ TCP ({len(tcps)}/{MAX_TCPS})",
                callback_data=f"tcpadd_{i}",
            ),
            InlineKeyboardButton(
                "🛠 مدیریت TCP",
                callback_data=f"tcpmanage_{i}",
            ),
        ]

        if len(tcps) < MAX_TCPS:
            buttons.append(tcp_buttons)
        else:
            buttons.append([
                InlineKeyboardButton(
                    f"🔌 TCP کامل شد ({len(tcps)}/{MAX_TCPS})",
                    callback_data=f"tcpmanage_{i}",
                )
            ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 بازگشت",
            callback_data="home",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def panel_text(panel):
    normalize_panel(panel)

    tcps = panel.get("tcps", [])

    text = (
        f"📦 <b>{panel.get('name', 'Panel')}</b>\n\n"
        f"🌐 HTTP: "
        f"{panel.get('http') or 'ندارد'}\n\n"
        f"📁 Repo: "
        f"{panel.get('repo', '-')}\n\n"
        f"⚙️ Port: "
        f"{panel.get('port', '-')}\n\n"
        f"🌍 Region: "
        f"{panel.get('region', '-')}\n\n"
        f"🔌 TCP: {len(tcps)}/{MAX_TCPS}\n"
    )

    for i, tcp in enumerate(tcps, 1):
        text += (
            f"\n🔹 TCP {i}: "
            f"<code>{tcp.get('address', '-')}</code>"
        )

    return text


def panel_keyboard(index, panel):
    normalize_panel(panel)

    tcps = panel.get("tcps", [])

    buttons = []

    if len(tcps) < MAX_TCPS:
        buttons.append([
            InlineKeyboardButton(
                f"➕ ساخت TCP ({len(tcps)}/{MAX_TCPS})",
                callback_data=f"tcpadd_{index}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🛠 مدیریت TCP",
            callback_data=f"tcpmanage_{index}",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🗑 حذف پنل",
            callback_data=f"pdel_{index}",
        )
    ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 همه پنل‌ها",
            callback_data="panels",
        )
    ])

    return InlineKeyboardMarkup(buttons)


def tcp_manage_keyboard(panel_index, panel):
    normalize_panel(panel)

    tcps = panel.get("tcps", [])

    buttons = []

    for i, tcp in enumerate(tcps):
        buttons.append([
            InlineKeyboardButton(
                f"🔌 TCP {i+1}",
                callback_data=f"tcpinfo_{panel_index}_{i}",
            ),
            InlineKeyboardButton(
                "🗑 حذف",
                callback_data=f"tcpdel_{panel_index}_{i}",
            ),
        ])

    if len(tcps) < MAX_TCPS:
        buttons.append([
            InlineKeyboardButton(
                f"➕ ساخت TCP ({len(tcps)}/{MAX_TCPS})",
                callback_data=f"tcpadd_{panel_index}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "🔙 پنل",
            callback_data=f"p_{panel_index}",
        )
    ])

    return InlineKeyboardMarkup(buttons)


# =========================================================
# START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_data = context.user_data

    if not get_active_token(user_data):
        text = (
            "👋 سلام!\n\n"
            "برای شروع ابتدا یک توکن Railway ثبت کن."
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔑 ثبت توکن Railway",
                    callback_data="token_add",
                )
            ],
            [
                InlineKeyboardButton(
                    "📋 مدیریت پنل‌ها",
                    callback_data="panels",
                )
            ],
        ])

        await update.message.reply_text(
            text,
            reply_markup=keyboard,
        )

        return

    await update.message.reply_text(
        "🏠 منوی اصلی",
        reply_markup=main_menu(user_data),
    )


# =========================================================
# CALLBACKS
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user_data = context.user_data

    data = query.data

    # -----------------------------------------
    # NOOP
    # -----------------------------------------

    if data == "noop":
        return

    # -----------------------------------------
    # HOME
    # -----------------------------------------

    if data == "home":
        await query.edit_message_text(
            "🏠 منوی اصلی",
            reply_markup=main_menu(user_data),
        )
        return

    # -----------------------------------------
    # TOKENS
    # -----------------------------------------

    if data == "tokens":
        tokens = get_tokens(user_data)

        text = (
            "🔑 <b>مدیریت توکن‌های Railway</b>\n\n"
            f"تعداد توکن‌ها: {len(tokens)}\n\n"
            "🟢 توکن فعال برای ساخت پنل جدید استفاده می‌شود."
        )

        await query.edit_message_text(
            text,
            reply_markup=tokens_keyboard(user_data),
            parse_mode="HTML",
        )

        return

    if data == "token_add":
        user_data["waiting"] = "token"

        await query.edit_message_text(
            "🔑 توکن Railway را ارسال کن:\n\n"
            "توکن به صورت دائمی در اطلاعات ربات ذخیره می‌شود."
        )

        return

    if data.startswith("token_select_"):
        token_id = data.replace("token_select_", "", 1)

        if token_id not in get_tokens(user_data):
            await query.answer(
                "این توکن پیدا نشد.",
                show_alert=True,
            )
            return

        user_data["active_token_id"] = token_id

        await query.edit_message_text(
            "✅ توکن فعال تغییر کرد.",
            reply_markup=tokens_keyboard(user_data),
        )

        return

    if data.startswith("token_delete_"):
        token_id = data.replace("token_delete_", "", 1)

        if token_id not in get_tokens(user_data):
            return

        user_data["delete_token_id"] = token_id

        await query.edit_message_text(
            "⚠️ مطمئنی می‌خواهی این توکن حذف شود؟\n\n"
            "پنل‌هایی که قبلاً با این توکن ساخته شده‌اند "
            "در اطلاعات ربات باقی می‌مانند، ولی برای مدیریت "
            "آن‌ها دوباره باید همین توکن را اضافه کنی.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ بله، حذف کن",
                        callback_data="token_delete_confirm",
                    ),
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data="tokens",
                    ),
                ]
            ]),
        )

        return

    if data == "token_delete_confirm":
        token_id = user_data.pop(
            "delete_token_id",
            None,
        )

        if token_id:
            delete_token(
                user_data,
                token_id,
            )

        await query.edit_message_text(
            "✅ توکن حذف شد.",
            reply_markup=tokens_keyboard(user_data),
        )

        return

    # -----------------------------------------
    # NEW PANEL
    # -----------------------------------------

    if data == "new":
        if not get_active_token(user_data):
            await query.edit_message_text(
                "❌ ابتدا یک توکن Railway ثبت کن.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔑 ثبت توکن",
                            callback_data="token_add",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data="home",
                        )
                    ],
                ]),
            )
            return

        user_data["waiting"] = "repo"

        await query.edit_message_text(
            "🚀 آدرس GitHub Repository را ارسال کن.\n\n"
            "مثال:\n"
            "<code>username/repository</code>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # PANELS
    # -----------------------------------------

    if data == "panels":
        panels = get_panels(user_data)

        if not panels:
            await query.edit_message_text(
                "📭 هنوز هیچ پنلی ساخته نشده.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🚀 ساخت پنل جدید",
                            callback_data="new",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 بازگشت",
                            callback_data="home",
                        )
                    ],
                ]),
            )
            return

        await query.edit_message_text(
            f"📋 <b>مدیریت پنل‌ها</b>\n\n"
            f"تعداد پنل‌ها: {len(panels)}\n\n"
            f"از همین‌جا برای هر پنل می‌توانی TCP بسازی.",
            reply_markup=panels_keyboard(user_data),
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # PANEL
    # -----------------------------------------

    if data.startswith("p_"):
        try:
            index = int(data.split("_")[1])
        except Exception:
            return

        panels = get_panels(user_data)

        if index >= len(panels):
            return

        panel = panels[index]

        await query.edit_message_text(
            panel_text(panel),
            reply_markup=panel_keyboard(
                index,
                panel,
            ),
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # TCP ADD
    # -----------------------------------------

    if data.startswith("tcpadd_"):
        try:
            index = int(data.split("_")[1])
        except Exception:
            return

        panels = get_panels(user_data)

        if index >= len(panels):
            return

        panel = panels[index]

        normalize_panel(panel)

        tcps = panel["tcps"]

        if len(tcps) >= MAX_TCPS:
            await query.answer(
                "❌ این پنل قبلاً ۳ TCP دارد.",
                show_alert=True,
            )
            return

        token = get_token_for_panel(
            user_data,
            panel,
        )

        if not token:
            await query.edit_message_text(
                "❌ توکن Railway مربوط به این پنل در ربات وجود ندارد.\n\n"
                "لطفاً همان توکن را دوباره ثبت کن.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔑 ثبت توکن",
                            callback_data="token_add",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 مدیریت پنل‌ها",
                            callback_data="panels",
                        )
                    ],
                ]),
            )
            return

        user_data["waiting"] = "tcp_port"
        user_data["tcp_panel_index"] = index

        await query.edit_message_text(
            f"🔌 ساخت TCP جدید\n\n"
            f"TCP فعلی: {len(tcps)}/{MAX_TCPS}\n\n"
            f"پورت Application را ارسال کن.\n\n"
            f"مثال: <code>443</code>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # TCP MANAGE
    # -----------------------------------------

    if data.startswith("tcpmanage_"):
        try:
            index = int(data.split("_")[1])
        except Exception:
            return

        panels = get_panels(user_data)

        if index >= len(panels):
            return

        panel = panels[index]

        normalize_panel(panel)

        await query.edit_message_text(
            f"🛠 <b>مدیریت TCP</b>\n\n"
            f"پنل: <b>{panel.get('name', 'Panel')}</b>\n"
            f"تعداد TCP: {len(panel['tcps'])}/{MAX_TCPS}",
            reply_markup=tcp_manage_keyboard(
                index,
                panel,
            ),
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # TCP INFO
    # -----------------------------------------

    if data.startswith("tcpinfo_"):
        parts = data.split("_")

        try:
            panel_index = int(parts[1])
            tcp_index = int(parts[2])
        except Exception:
            return

        panels = get_panels(user_data)

        if panel_index >= len(panels):
            return

        panel = panels[panel_index]

        normalize_panel(panel)

        tcps = panel["tcps"]

        if tcp_index >= len(tcps):
            return

        tcp = tcps[tcp_index]

        text = (
            f"🔌 <b>TCP {tcp_index + 1}</b>\n\n"
            f"🌐 Address:\n"
            f"<code>{tcp.get('address', '-')}</code>\n\n"
            f"📥 Application Port: "
            f"{tcp.get('application_port', '-')}\n\n"
            f"📤 Proxy Port: "
            f"{tcp.get('proxy_port', '-')}"
        )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🗑 حذف TCP",
                        callback_data=(
                            f"tcpdel_{panel_index}_{tcp_index}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "🔙 مدیریت TCP",
                        callback_data=f"tcpmanage_{panel_index}",
                    )
                ],
            ]),
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # TCP DELETE
    # -----------------------------------------

    if data.startswith("tcpdel_"):
        parts = data.split("_")

        try:
            panel_index = int(parts[1])
            tcp_index = int(parts[2])
        except Exception:
            return

        panels = get_panels(user_data)

        if panel_index >= len(panels):
            return

        panel = panels[panel_index]

        normalize_panel(panel)

        tcps = panel["tcps"]

        if tcp_index >= len(tcps):
            return

        user_data["delete_tcp_panel"] = panel_index
        user_data["delete_tcp_index"] = tcp_index

        await query.edit_message_text(
            f"⚠️ حذف TCP {tcp_index + 1}؟\n\n"
            f"<code>{tcps[tcp_index].get('address', '-')}</code>",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ حذف",
                        callback_data="tcpdel_confirm",
                    ),
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=(
                            f"tcpmanage_{panel_index}"
                        ),
                    ),
                ]
            ]),
            parse_mode="HTML",
        )

        return

    if data == "tcpdel_confirm":
        panel_index = user_data.pop(
            "delete_tcp_panel",
            None,
        )

        tcp_index = user_data.pop(
            "delete_tcp_index",
            None,
        )

        if panel_index is None or tcp_index is None:
            return

        panels = get_panels(user_data)

        if panel_index >= len(panels):
            return

        panel = panels[panel_index]

        normalize_panel(panel)

        tcps = panel["tcps"]

        if tcp_index >= len(tcps):
            return

        tcp = tcps[tcp_index]

        token = get_token_for_panel(
            user_data,
            panel,
        )

        if not token:
            await query.edit_message_text(
                "❌ توکن مربوط به این پنل پیدا نشد.\n\n"
                "ابتدا توکن مربوط به پنل را دوباره ثبت کن.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔑 ثبت توکن",
                            callback_data="token_add",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🔙 مدیریت پنل‌ها",
                            callback_data="panels",
                        )
                    ],
                ]),
            )
            return

        try:
            if tcp.get("id"):
                delete_tcp_by_id(
                    token,
                    tcp["id"],
                )

        except Exception as e:
            await query.edit_message_text(
                f"❌ خطا در حذف TCP:\n\n{e}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔙 مدیریت TCP",
                            callback_data=(
                                f"tcpmanage_{panel_index}"
                            ),
                        )
                    ]
                ]),
            )
            return

        tcps.pop(tcp_index)

        await query.edit_message_text(
            "✅ TCP حذف شد.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🛠 مدیریت TCP",
                        callback_data=(
                            f"tcpmanage_{panel_index}"
                        ),
                    )
                ],
                [
                    InlineKeyboardButton(
                        "📋 همه پنل‌ها",
                        callback_data="panels",
                    )
                ],
            ]),
        )

        return

    # -----------------------------------------
    # PANEL DELETE
    # -----------------------------------------

    if data.startswith("pdel_"):
        try:
            index = int(data.split("_")[1])
        except Exception:
            return

        panels = get_panels(user_data)

        if index >= len(panels):
            return

        user_data["delete_panel_index"] = index

        await query.edit_message_text(
            "⚠️ مطمئنی می‌خواهی این پنل را حذف کنی؟",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ بله، حذف کن",
                        callback_data="pdel_confirm",
                    ),
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=f"p_{index}",
                    ),
                ]
            ]),
        )

        return

    if data == "pdel_confirm":
        index = user_data.pop(
            "delete_panel_index",
            None,
        )

        if index is None:
            return

        panels = get_panels(user_data)

        if index >= len(panels):
            return

        panel = panels[index]

        token = get_token_for_panel(
            user_data,
            panel,
        )

        if token:
            try:
                delete_project(
                    token,
                    panel["project_id"],
                )
            except Exception:
                pass

        panels.pop(index)

        await query.edit_message_text(
            "✅ پنل حذف شد.",
            reply_markup=panels_keyboard(user_data),
        )

        return


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_data = context.user_data

    waiting = user_data.get("waiting")

    text = update.message.text.strip()

    # -----------------------------------------
    # TOKEN
    # -----------------------------------------

    if waiting == "token":
        token = text

        if len(token) < 10:
            await update.message.reply_text(
                "❌ توکن واردشده خیلی کوتاه است."
            )
            return

        # Validate token
        workspace_id = get_workspace_id(token)

        if not workspace_id:
            await update.message.reply_text(
                "❌ توکن Railway معتبر نیست.\n"
                "یک توکن صحیح ارسال کن."
            )
            return

        token_id = add_token(
            user_data,
            token,
        )

        user_data.pop("waiting", None)

        await update.message.reply_text(
            "✅ توکن Railway با موفقیت ثبت شد.\n\n"
            "این توکن به عنوان توکن فعال انتخاب شد.",
            reply_markup=main_menu(user_data),
        )

        return

    # -----------------------------------------
    # REPO
    # -----------------------------------------

    if waiting == "repo":
        repo = text

        repo = repo.replace(
            "https://github.com/",
            "",
        ).strip("/")

        if not re.match(
            r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$",
            repo,
        ):
            await update.message.reply_text(
                "❌ فرمت Repository صحیح نیست.\n\n"
                "مثال:\n"
                "username/repository"
            )
            return

        if not get_active_token(user_data):
            await update.message.reply_text(
                "❌ ابتدا یک توکن Railway ثبت کن."
            )
            return

        user_data["pending_repo"] = repo
        user_data["waiting"] = "deploy_port"

        await update.message.reply_text(
            "📡 پورت برنامه را ارسال کن.\n\n"
            "مثال:\n"
            "<code>8080</code>",
            parse_mode="HTML",
        )

        return

    # -----------------------------------------
    # DEPLOY PORT
    # -----------------------------------------

    if waiting == "deploy_port":
        try:
            port = int(text)

            if port < 1 or port > 65535:
                raise ValueError

        except Exception:
            await update.message.reply_text(
                "❌ پورت باید عددی بین 1 تا 65535 باشد."
            )
            return

        repo = user_data.pop(
            "pending_repo",
            None,
        )

        token = get_active_token(user_data)

        token_id = get_active_token_id(user_data)

        if not repo or not token or not token_id:
            await update.message.reply_text(
                "❌ اطلاعات ساخت پنل ناقص است."
            )
            user_data.pop("waiting", None)
            return

        await update.message.reply_text(
            "⏳ در حال ساخت پنل روی Railway..."
        )

        try:
            panel = await asyncio.to_thread(
                deploy_panel,
                token,
                repo,
                port,
            )

            panel["token_id"] = token_id

            panels = get_panels(user_data)

            panels.append(panel)

            user_data.pop("waiting", None)

            index = len(panels) - 1

            await update.message.reply_text(
                "✅ پنل با موفقیت ساخته شد!\n\n"
                + panel_text(panel),
                reply_markup=panel_keyboard(
                    index,
                    panel,
                ),
                parse_mode="HTML",
            )

        except Exception as e:
            user_data.pop("waiting", None)

            await update.message.reply_text(
                f"❌ خطا در ساخت پنل:\n\n"
                f"<code>{str(e)}</code>",
                parse_mode="HTML",
                reply_markup=main_menu(user_data),
            )

        return

    # -----------------------------------------
    # TCP PORT
    # -----------------------------------------

    if waiting == "tcp_port":
        try:
            port = int(text)

            if port < 1 or port > 65535:
                raise ValueError

        except Exception:
            await update.message.reply_text(
                "❌ پورت باید عددی بین 1 تا 65535 باشد."
            )
            return

        panel_index = user_data.pop(
            "tcp_panel_index",
            None,
        )

        user_data.pop("waiting", None)

        if panel_index is None:
            await update.message.reply_text(
                "❌ پنل پیدا نشد."
            )
            return

        panels = get_panels(user_data)

        if panel_index >= len(panels):
            await update.message.reply_text(
                "❌ پنل پیدا نشد."
            )
            return

        panel = panels[panel_index]

        normalize_panel(panel)

        if len(panel["tcps"]) >= MAX_TCPS:
            await update.message.reply_text(
                "❌ این پنل قبلاً ۳ TCP دارد."
            )
            return

        token = get_token_for_panel(
            user_data,
            panel,
        )

        if not token:
            await update.message.reply_text(
                "❌ توکن Railway مربوط به این پنل پیدا نشد."
            )
            return

        await update.message.reply_text(
            "⏳ در حال ساخت TCP..."
        )

        try:
            tcp = await asyncio.to_thread(
                create_tcp,
                token,
                panel["service_id"],
                panel["env_id"],
                port,
            )

            panel["tcps"].append(tcp)

            await update.message.reply_text(
                f"✅ TCP با موفقیت ساخته شد!\n\n"
                f"🔌 TCP "
                f"{len(panel['tcps'])}/{MAX_TCPS}\n\n"
                f"🌐 Address:\n"
                f"<code>{tcp.get('address', '-')}</code>\n\n"
                f"📥 Application Port: "
                f"{tcp.get('application_port', '-')}\n\n"
                f"📤 Proxy Port: "
                f"{tcp.get('proxy_port', '-')}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "➕ TCP دیگر",
                            callback_data=(
                                f"tcpadd_{panel_index}"
                            ),
                        )
                    ]
                    if len(panel["tcps"]) < MAX_TCPS
                    else [
                        InlineKeyboardButton(
                            "🛠 مدیریت TCP",
                            callback_data=(
                                f"tcpmanage_{panel_index}"
                            ),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "📋 همه پنل‌ها",
                            callback_data="panels",
                        )
                    ],
                ]),
                parse_mode="HTML",
            )

        except Exception as e:
            await update.message.reply_text(
                f"❌ خطا در ساخت TCP:\n\n"
                f"<code>{str(e)}</code>",
                parse_mode="HTML",
            )

        return

    # -----------------------------------------
    # DEFAULT
    # -----------------------------------------

    await update.message.reply_text(
        "از منوی ربات استفاده کن.",
        reply_markup=main_menu(user_data),
    )


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    print("BOT ERROR:", context.error)


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    os.makedirs(
        DATA_DIR,
        exist_ok=True,
    )

    persistence = PicklePersistence(
        filepath=DATA_FILE,
        update_interval=2,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            button_handler,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(
        error_handler
    )

    print("Bot started...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
