import datetime
import javaproperties
import secrets
import aiohttp
import asyncio
import requests
import os
import json
import lzma
import time
import io
import importlib
import uuid
import logging
import inspect
import subprocess
import sys
from typing import Dict, List, Any, Optional, Union, Tuple
from packaging.version import parse as parse_version

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request, status, HTTPException, Body
from fastapi.responses import (
    HTMLResponse,
    RedirectResponse,
    JSONResponse,
    PlainTextResponse,
)
from fastapi.templating import Jinja2Templates
from ruamel.yaml.comments import CommentedSeq
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from .utils.log_watcher import LogWatcher
from .utils.PIM import PluginInstaller, create_installer, initialize_pim  # 修改导入，添加 initialize_pim

from .utils.constant import *
from .utils.server_util import *
from .utils.table import yaml
from .utils.utils import *

import mcdreforged.api.all as MCDR

from .utils.utils import __copyFile

app = FastAPI()

# template engine -> jinja2
templates = Jinja2Templates(directory=f"{STATIC_PATH}/templates")

# 全局LogWatcher实例
log_watcher = LogWatcher()

# 用于保存pip任务状态的字典
pip_tasks = {}

# 尝试迁移旧配置
migrate_old_config()

# 初始化函数，在应用程序启动时调用
def init_app(server_instance):
    """初始化应用程序，注册事件监听器"""
    global log_watcher
    
     # 加载HTTPS总开关（来自config.json）
    main_config = server_instance.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
    https_enabled = main_config.get("https_enabled", False)

    # 加载TLS具体配置（来自config/guguwebui/tls.json）
    tls_config_path = os.path.join(server_instance.get_data_folder(), "config", "guguwebui", "tls.json")
    try:
        with open(tls_config_path, "r", encoding="utf-8") as f:
            tls_config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        tls_config = {}
        server_instance.logger.warning(f"TLS配置文件不存在或格式错误：{tls_config_path}")

    # 构造证书路径（固定存储在config/guguwebui/cert/目录）
    cert_dir = os.path.join(server_instance.get_data_folder(), "config", "guguwebui", "cert")
    ssl_cert_path = os.path.join(cert_dir, tls_config.get("ssl_cert_file", "server.crt"))
    ssl_key_path = os.path.join(cert_dir, tls_config.get("ssl_key_file", "server.key"))

    # 配置HTTPS参数
    ssl_args = {}
    if https_enabled:
        if os.path.exists(ssl_cert_path) and os.path.exists(ssl_key_path):
            ssl_args = {
                "ssl_keyfile": ssl_key_path,
                "ssl_certfile": ssl_cert_path
            }
            server_instance.logger.info(f"HTTPS已启用，使用证书：{ssl_cert_path}")
            server_instance.logger.info(f"HTTPS已启用，使用密钥：{ssl_key_path}")


        else:
            server_instance.logger.warning(f"HTTPS启用但证书文件不存在（证书路径：{ssl_cert_path}，密钥路径：{ssl_key_path}），将使用HTTP")
    else:
        server_instance.logger.info("HTTPS未启用，使用HTTP")

    # 启动服务器时应用SSL配置（假设原启动代码在此处，需根据实际启动方式调整）
    # 示例：uvicorn.run(app, host=host, port=port, **ssl_args)
    
    # 存储服务器接口
    app.state.server_interface = server_instance
    
    # 清理现有监听器，避免重复注册
    if log_watcher:
        log_watcher.stop()
    
    # 初始化LogWatcher实例，将 server_instance 传递给它
    log_watcher = LogWatcher(server_interface=server_instance)
    
    # 设置日志捕获 - 直接调用此方法确保与MCDR内部日志系统连接
    log_watcher._setup_log_capture()
    
    # 注册MCDR事件监听器，每种事件只注册一次
    # 修正：GENERAL_INFO应该映射到on_mcdr_info，处理MCDR和服务器的常规信息
    # USER_INFO应该映射到on_server_output，处理用户输入的命令
    server_instance.register_event_listener(MCDR.MCDRPluginEvents.GENERAL_INFO, on_mcdr_info)
    server_instance.register_event_listener(MCDR.MCDRPluginEvents.USER_INFO, on_server_output)
    
    # 初始化PIM模块
    try:
        server_instance.logger.debug("正在初始化内置PIM模块...")
        pim_helper, plugin_installer = initialize_pim(server_instance)
        # 将初始化后的PIM实例存储到app.state中，供API调用
        app.state.pim_helper = pim_helper
        app.state.plugin_installer = plugin_installer
        if pim_helper and plugin_installer:
            server_instance.logger.info("内置PIM模块初始化成功")
        else:
            server_instance.logger.warning("内置PIM模块初始化部分失败，某些功能可能不可用")
            
        # 在启动时检查插件仓库缓存
        check_repository_cache(server_instance)
    except Exception as e:
        server_instance.logger.error(f"内置PIM模块初始化失败: {e}")
    
    server_instance.logger.debug("WebUI日志捕获器已初始化，将直接从MCDR捕获日志")

# 检查仓库缓存
def check_repository_cache(server):
    """检查插件仓库缓存，如果不存在则尝试下载"""
    try:
        # 获取PIM缓存目录
        pim_helper = app.state.pim_helper
        if not pim_helper:
            server.logger.warning("无法获取PIM模块实例，跳过缓存检查")
            return
            
        cache_dir = pim_helper.get_temp_dir()
        cache_file = os.path.join(cache_dir, "everything_slim.json")
        
        # 创建一个命令源模拟对象
        class CacheCheckSource:
            def __init__(self, server):
                self.server = server
            
            def reply(self, message):
                self.server.logger.info(f"[仓库缓存] {message}")
            
            def get_server(self):
                return self.server
        
        source = CacheCheckSource(server)
        
        # 检查缓存是否存在
        if not os.path.exists(cache_file):
            server.logger.info("插件仓库缓存不存在，尝试下载")
            
            # 使用PIM获取仓库数据，会自动缓存，使用ignore_ttl=False以利用PIM的失败缓存机制
            # 无需额外的try-except，因为PIM内部已经实现了失败处理和重试
            pim_helper.get_cata_meta(source, ignore_ttl=False)
            
            # 检查下载后缓存是否存在
            if os.path.exists(cache_file):
                server.logger.info("插件仓库缓存已成功下载")
            else:
                server.logger.warning("插件仓库缓存下载可能失败，但这是正常的，请参考日志了解详情")
                server.logger.info("WebUI将使用PIM模块的下载失败缓存机制，在15分钟内不会重复尝试下载失败的仓库")
        else:
            server.logger.debug("插件仓库缓存已存在")
    except Exception as e:
        server.logger.error(f"检查仓库缓存时出错: {e}")

# 事件处理函数
def on_server_output(server, info):
    """处理服务器输出事件"""
    global log_watcher
    if log_watcher:
        log_watcher.on_server_output(server, info)

def on_mcdr_info(server, info):
    """处理MCDR信息事件"""
    global log_watcher
    if log_watcher:
        log_watcher.on_mcdr_info(server, info)

# ============================================================#

# redirect to login
@app.get("/", name="root")
def read_root(request: Request):
    return RedirectResponse(url='./login')


# login page
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # token is valid
    token = request.cookies.get("token")
    server:PluginServerInterface = app.state.server_interface
    server_config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)

    disable_other_admin = server_config.get("disable_other_admin", False)
    super_admin_account = server_config.get("super_admin_account")

    def login_admin_check(account, disable_other_admin, super_admin_account):
        if disable_other_admin and account != super_admin_account:
            return False
        return True

    if (
        token
        and user_db["token"].get(token)
        and user_db["token"][token]["expire_time"]
        > str(datetime.datetime.now(datetime.timezone.utc))
        and login_admin_check(user_db["token"][token]["user_name"], disable_other_admin, super_admin_account)
    ):
        request.session["logged_in"] = True
        request.session["token"] = token
        request.session["username"] = user_db["token"][token]["user_name"]
        return RedirectResponse(url="./index", status_code=status.HTTP_302_FOUND)

    # no token / expired token
    response = templates.TemplateResponse("login.html", {"request": request})
    if token:
        if token in user_db["token"]:
            del user_db["token"][token]
            user_db.save()
        response.delete_cookie("token")
    return response


# login request
@app.post("/login")
async def login(
    request: Request,
    account: str = Form(""),
    password: str = Form(""),
    temp_code: str = Form(""),
    remember: bool = Form(False),
):
    response = templates.TemplateResponse(
        "login.html", {"request": request, "error": "未知错误。"}
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    server:PluginServerInterface = app.state.server_interface
    server_config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)

    # check account & password
    if account and password:
        # check if super admin & only_super_admin
        disable_other_admin = server_config.get("disable_other_admin", False)
        super_admin_account = str(server_config.get("super_admin_account"))
        if disable_other_admin and account != super_admin_account:
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "只有超级管理才能登录。"}
            )

        if account in user_db["user"] and verify_password(
            password, user_db["user"][account]
        ):
            # token Generation
            token = secrets.token_hex(16)
            expiry = now + (
                    datetime.timedelta(days=365)
                    if remember
                    else datetime.timedelta(days=1)
                )
            max_age = datetime.timedelta(days=365) if remember else datetime.timedelta(days=1)
            max_age = max_age.total_seconds()

            response = RedirectResponse(url="./index", status_code=status.HTTP_302_FOUND)
            response.set_cookie("token", token, expires=expiry, path="/", httponly=True, max_age=max_age)

            # save token & username session
            request.session["logged_in"] = True
            request.session["token"] = token
            request.session["username"] = account

            user_db["token"][token] = {"user_name": account, "expire_time": str(expiry)}
            user_db.save()

        else:
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "账号或密码错误。"}
            )

    # temp password
    elif temp_code:
        # disallow temp_password check
        allow_temp_password = server_config.get('allow_temp_password', True)
        if not allow_temp_password:
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "已禁止临时登录码登录。"}
            )

        if temp_code in user_db["temp"] and user_db["temp"][temp_code] > str(now):
            # token Generation
            token = secrets.token_hex(16)
            expiry = now + datetime.timedelta(hours=2)  # 临时码有效期为2小时
            max_age = datetime.timedelta(hours=2)
            max_age = max_age.total_seconds()

            response = RedirectResponse(url="./index", status_code=status.HTTP_302_FOUND)
            response.set_cookie("token", token, expires=expiry, path="/", httponly=True, max_age=max_age)

            # save token & username in session
            request.session["logged_in"] = True
            request.session["token"] = token
            request.session["username"] = "tempuser"

            user_db["token"][token] = {"user_name": "tempuser", "expire_time": str(expiry)}
            user_db.save()

        else:
            if temp_code in user_db["temp"]:  # delete expired token
                del user_db["temp"][temp_code]
                user_db.save()
            # Inavalid temp password
            return templates.TemplateResponse(
                "login.html", {"request": request, "error": "临时登录码无效。"}
            )

    else:
        # 如果未提供完整的登录信息
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "请填写完整的登录信息。"}
        )

    return response


# logout Endpoint
@app.get("/logout", response_class=RedirectResponse)
def logout(request: Request):
    request.session["logged_in"] = False
    request.session.clear()  # clear session data
    response = RedirectResponse(url="./login", status_code=status.HTTP_302_FOUND)
    response.delete_cookie("token", path="/")  # delete token cookie
    return response

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
# ============================================================#
# Pages
@app.get("/index", response_class=HTMLResponse)
async def read_index(request: Request, token_valid: bool = Depends(verify_token)):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="./login")
    return templates.TemplateResponse(
        "index.html", {"request": request, "index_path": "./index"}
    )


@app.get("/home", response_class=HTMLResponse)
async def read_home(request: Request, token_valid: bool = Depends(verify_token)):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="./login")
    return templates.TemplateResponse(
        "home.html", {"request": request, "message": "欢迎进入后台主页！", "index_path": "./index"}
    )


async def render_template_if_logged_in(request: Request, template_name: str):
    if not request.session.get("logged_in"):
        return RedirectResponse(url="./login")
    return templates.TemplateResponse(template_name, {"request": request, "index_path": "./index"})

@app.get("/gugubot", response_class=HTMLResponse)
async def gugubot(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "gugubot.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/cq", response_class=HTMLResponse)
async def cq(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "cq.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/mc", response_class=HTMLResponse)
async def mc(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "mc.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/mcdr", response_class=HTMLResponse)
async def mcdr(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "mcdr.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/plugins", response_class=HTMLResponse)
async def plugins(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "plugins.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/online-plugins", response_class=HTMLResponse)
async def online_plugins(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "online-plugins.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/settings", response_class=HTMLResponse)
async def settings(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "settings.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)


@app.get("/about", response_class=HTMLResponse)
async def about(request: Request, token_valid: bool = Depends(verify_token)):
    try:
        return await render_template_if_logged_in(request, "about.html")
    except Exception:
        return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

# 404 page
@app.exception_handler(404)
async def custom_404_handler(request: Request, exc: StarletteHTTPException):
    return templates.TemplateResponse("404.html", {"request": request}, status_code=404)

@app.exception_handler(ConnectionResetError)
async def connection_reset_handler(request: Request, exc: ConnectionResetError):
    # 在日志中记录错误，但向客户端返回友好消息
    app.state.server_interface.logger.warning(f"[GUGUWebUI] 连接重置错误: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "连接被重置，请刷新页面重试"}
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # 记录所有未处理的异常
    error_message = f"未处理的异常: {str(exc)}"
    
    # 尝试获取服务器接口记录日志
    try:
        if hasattr(app.state, "server_interface"):
            app.state.server_interface.logger.error(f"[GUGUWebUI] {error_message}")
        else:
            print(f"[GUGUWebUI] {error_message}")
    except:
        print(f"[GUGUWebUI] {error_message}")
    
    # 返回友好的错误消息
    return JSONResponse(
        status_code=500,
        content={"status": "error", "message": "服务器内部错误，请稍后再试"}
    )

# ============================================================#

@app.get("/api/checkLogin")
async def check_login_status(request: Request):
    if request.session.get("logged_in"):
        username = request.session.get("username", "tempuser")
        return JSONResponse({"status": "success", "username": username})
    return JSONResponse({"status": "error", "message": "User not logged in"})


# Return gugu plugins' metadata
@app.get("/api/gugubot_plugins")
async def get_gugubot_plugins(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    return JSONResponse(
        content={
            "gugubot_plugins": get_gugubot_plugins_info(app.state.server_interface)
        }
    )


# Return plugins' metadata
@app.get("/api/plugins")
async def get_plugins(request: Request, detail: bool = False):
    plugins = get_plugins_info(app.state.server_interface, detail)
    if not request.session.get("logged_in"):
        # 未登录时仅返回guguwebui插件信息
        guguwebui_plugin = next((p for p in plugins if p["id"] == "guguwebui"), None)
        if guguwebui_plugin:
            return JSONResponse(
                content={"plugins": [guguwebui_plugin]}
            )
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    return JSONResponse(
        content={"plugins": plugins}
    )

# 从 everything_slim.json 获取在线插件列表，免登录
@app.get("/api/online-plugins")
async def get_online_plugins(request: Request, repo_url: str = None):
    # 获取服务器接口和PIM助手
    server = app.state.server_interface
    pim_helper = getattr(app.state, "pim_helper", None)
    
    # 如果没有PIM助手，无法处理请求
    if not pim_helper:
        server.logger.warning("未找到PIM助手实例，无法获取插件信息")
        return []
    
    # 获取配置中定义的仓库URL
    config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
    official_repo_url = config.get("mcdr_plugins_url", "https://api.mcdreforged.com/catalogue/everything_slim.json.xz")
    configured_repos = [official_repo_url]  # 始终包含官方仓库
    
    # 添加配置中的其他仓库URL
    if "repositories" in config and isinstance(config["repositories"], list):
        for repo in config["repositories"]:
            if isinstance(repo, dict) and "url" in repo:
                configured_repos.append(repo["url"])
    
    try:
        # 创建一个命令源模拟对象，用于PIM助手的API调用
        class FakeSource:
            def __init__(self, server):
                self.server = server
            
            def reply(self, message):
                if isinstance(message, str):
                    self.server.logger.debug(f"[仓库API] {message}")
            
            def get_server(self):
                return self.server
        
        source = FakeSource(server)
        
        # 如果指定了特定仓库URL，则只获取该仓库的数据
        if repo_url:
            # 检查是否是配置中的仓库，否则视为不受信任的源
            is_configured_repo = repo_url in configured_repos
            
            # 使用PIM获取元数据，使用ignore_ttl=False以利用PIM的下载失败缓存逻辑
            meta_registry = pim_helper.get_cata_meta(source, ignore_ttl=False, repo_url=repo_url)
            
            # 如果没有获取到有效的仓库数据，直接返回空列表
            if not meta_registry or not hasattr(meta_registry, 'get_plugins') or not meta_registry.get_plugins():
                server.logger.warning(f"未获取到有效的仓库数据: {repo_url}")
                return []
            
            # 获取原始仓库数据
            registry_data = {}
            try:
                # 尝试获取原始仓库数据
                if hasattr(meta_registry, 'get_registry_data'):
                    registry_data = meta_registry.get_registry_data()
            except Exception as e:
                server.logger.warning(f"获取原始仓库数据失败: {e}")
            
            # 检查数据类型 - 处理简化格式(list类型)和标准格式(dict类型)
            if isinstance(registry_data, list):
                # 简化格式 - 直接返回原始数据
                server.logger.debug(f"检测到简化格式仓库数据，直接处理: {repo_url}")
                return registry_data
            
            # 提取作者信息
            authors_data = {}
            try:
                if registry_data and 'authors' in registry_data and 'authors' in registry_data['authors']:
                    authors_data = registry_data['authors']['authors']
            except Exception as e:
                server.logger.warning(f"提取作者信息失败: {e}")
            
            # 转换为列表格式返回
            plugins_data = []
            for plugin_id, plugin_data in meta_registry.get_plugins().items():
                try:
                    # 处理作者信息为期望的格式
                    authors = []
                    # 从plugin部分获取作者信息，而不是meta部分
                    if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                        plugin_info = registry_data['plugins'][plugin_id].get('plugin', {})
                        author_names = plugin_info.get('authors', [])
                        
                        for author_name in author_names:
                            if isinstance(author_name, str) and author_name in authors_data:
                                # 从原始数据中获取作者详细信息
                                author_info = authors_data.get(author_name, {})
                                authors.append({
                                    'name': author_info.get('name', author_name),
                                    'link': author_info.get('link', '')
                                })
                            else:
                                # 直接使用作者名称
                                authors.append({
                                    'name': author_name,
                                    'link': ''
                                })
                    # 如果plugin部分没有作者信息，则尝试从plugin_data.author获取
                    elif hasattr(plugin_data, 'author'):
                        for author_item in plugin_data.author:
                            if isinstance(author_item, str):
                                # 原始格式：作者名称是字符串
                                if author_item in authors_data:
                                    # 从原始数据中获取作者详细信息
                                    author_info = authors_data.get(author_item, {})
                                    authors.append({
                                        'name': author_info.get('name', author_item),
                                        'link': author_info.get('link', '')
                                    })
                                else:
                                    # 直接使用作者名称
                                    authors.append({
                                        'name': author_item,
                                        'link': ''
                                    })
                            elif isinstance(author_item, dict):
                                # 简化格式：作者信息已经是字典
                                authors.append(author_item)
                    
                    # 获取最新版本信息
                    latest_release = plugin_data.get_latest_release()
                    
                    # 处理标签信息 (labels)
                    labels = []
                    
                    # 从原始数据中获取plugin信息
                    plugin_info = {}
                    if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                        plugin_info = registry_data['plugins'][plugin_id].get('plugin', {})
                        if 'labels' in plugin_info:
                            labels = plugin_info.get('labels', [])
                    
                    # 处理License信息
                    license_key = "未知"
                    license_url = ""
                    
                    # 从原始数据中获取repository信息
                    repo_info = {}
                    if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                        repo_info = registry_data['plugins'][plugin_id].get('repository', {})
                        if 'license' in repo_info and repo_info['license']:
                            license_info = repo_info['license']
                            license_key = license_info.get('key', '未知')
                            license_url = license_info.get('url', '')
                    
                    # 处理Readme URL
                    readme_url = ""
                    if 'readme_url' in repo_info:
                        readme_url = repo_info.get('readme_url', '')
                    
                    # 计算所有版本的下载总数
                    total_downloads = 0
                    if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                        release_info = registry_data['plugins'][plugin_id].get('release', {})
                        releases = release_info.get('releases', [])
                        for rel in releases:
                            if 'asset' in rel and 'download_count' in rel['asset']:
                                total_downloads += rel['asset']['download_count']
                    
                    # 如果没有找到任何下载数据，但最新版本有下载数，则使用它
                    if total_downloads == 0 and latest_release and hasattr(latest_release, 'download_count'):
                        total_downloads = latest_release.download_count
                    
                    # 创建插件条目
                    plugin_entry = {
                        "id": plugin_data.id,
                        "name": plugin_data.name,
                        "version": plugin_data.version,
                        "description": plugin_data.description,
                        "authors": authors,
                        "dependencies": {k: str(v) for k, v in plugin_data.dependencies.items()},
                        "labels": labels,
                        "repository_url": plugin_data.link,
                        "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "latest_version": plugin_data.latest_version,
                        "license": license_key,
                        "license_url": license_url,
                        "downloads": total_downloads,
                        "readme_url": readme_url,
                    }
                    
                    # 添加最后更新时间
                    if latest_release and hasattr(latest_release, 'created_at'):
                        try:
                            # 将ISO格式时间转换为更友好的格式
                            dt = datetime.datetime.fromisoformat(latest_release.created_at.replace('Z', '+00:00'))
                            plugin_entry["last_update_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except Exception as time_error:
                            server.logger.error(f"处理插件 {plugin_id} 的时间信息时出错: {time_error}")
                            plugin_entry["last_update_time"] = latest_release.created_at if hasattr(latest_release, 'created_at') else ''
                    
                    plugins_data.append(plugin_entry)
                except Exception as plugin_error:
                    server.logger.error(f"处理插件 {plugin_id} 时出错: {plugin_error}")
                    # 继续处理下一个插件
            
            return plugins_data
        
        # 没有指定仓库URL，使用官方仓库数据
        meta_registry = pim_helper.get_cata_meta(source, ignore_ttl=False)
        
        # 如果没有获取到有效的仓库数据，直接返回空列表
        if not meta_registry or not hasattr(meta_registry, 'get_plugins') or not meta_registry.get_plugins():
            server.logger.warning("未获取到有效的官方仓库数据")
            return []
        
        # 获取原始仓库数据
        registry_data = {}
        try:
            # 尝试获取原始仓库数据
            if hasattr(meta_registry, 'get_registry_data'):
                registry_data = meta_registry.get_registry_data()
        except Exception as e:
            server.logger.warning(f"获取原始仓库数据失败: {e}")
        
        # 检查数据类型 - 处理简化格式(list类型)和标准格式(dict类型)
        if isinstance(registry_data, list):
            # 简化格式 - 直接返回原始数据
            server.logger.info("检测到简化格式仓库数据，直接处理")
            return registry_data
        
        # 提取作者信息
        authors_data = {}
        try:
            if registry_data and 'authors' in registry_data and 'authors' in registry_data['authors']:
                authors_data = registry_data['authors']['authors']
        except Exception as e:
            server.logger.warning(f"提取作者信息失败: {e}")
        
        # 转换为列表格式返回
        plugins_data = []
        for plugin_id, plugin_data in meta_registry.get_plugins().items():
            try:
                # 处理作者信息为期望的格式
                authors = []
                # 从plugin部分获取作者信息，而不是meta部分
                if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                    plugin_info = registry_data['plugins'][plugin_id].get('plugin', {})
                    author_names = plugin_info.get('authors', [])
                    
                    for author_name in author_names:
                        if isinstance(author_name, str) and author_name in authors_data:
                            # 从原始数据中获取作者详细信息
                            author_info = authors_data.get(author_name, {})
                            authors.append({
                                'name': author_info.get('name', author_name),
                                'link': author_info.get('link', '')
                            })
                        else:
                            # 直接使用作者名称
                            authors.append({
                                'name': author_name,
                                'link': ''
                            })
                # 如果plugin部分没有作者信息，则尝试从plugin_data.author获取
                elif hasattr(plugin_data, 'author'):
                    for author_item in plugin_data.author:
                        if isinstance(author_item, str):
                            # 原始格式：作者名称是字符串
                            if author_item in authors_data:
                                # 从原始数据中获取作者详细信息
                                author_info = authors_data.get(author_item, {})
                                authors.append({
                                    'name': author_info.get('name', author_item),
                                    'link': author_info.get('link', '')
                                })
                            else:
                                # 直接使用作者名称
                                authors.append({
                                    'name': author_item,
                                    'link': ''
                                })
                        elif isinstance(author_item, dict):
                            # 简化格式：作者信息已经是字典
                            authors.append(author_item)
                
                # 获取最新版本信息
                latest_release = plugin_data.get_latest_release()
                
                # 处理标签信息 (labels)
                labels = []
                
                # 从原始数据中获取plugin信息
                plugin_info = {}
                if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                    plugin_info = registry_data['plugins'][plugin_id].get('plugin', {})
                    if 'labels' in plugin_info:
                        labels = plugin_info.get('labels', [])
                
                # 处理License信息
                license_key = "未知"
                license_url = ""
                
                # 从原始数据中获取repository信息
                repo_info = {}
                if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                    repo_info = registry_data['plugins'][plugin_id].get('repository', {})
                    if 'license' in repo_info and repo_info['license']:
                        license_info = repo_info['license']
                        license_key = license_info.get('key', '未知')
                        license_url = license_info.get('url', '')
                
                # 处理Readme URL
                readme_url = ""
                if 'readme_url' in repo_info:
                    readme_url = repo_info.get('readme_url', '')
                
                # 计算所有版本的下载总数
                total_downloads = 0
                if registry_data and 'plugins' in registry_data and plugin_id in registry_data['plugins']:
                    release_info = registry_data['plugins'][plugin_id].get('release', {})
                    releases = release_info.get('releases', [])
                    for rel in releases:
                        if 'asset' in rel and 'download_count' in rel['asset']:
                            total_downloads += rel['asset']['download_count']
                
                # 如果没有找到任何下载数据，但最新版本有下载数，则使用它
                if total_downloads == 0 and latest_release and hasattr(latest_release, 'download_count'):
                    total_downloads = latest_release.download_count
                
                # 创建插件条目
                plugin_entry = {
                    "id": plugin_data.id,
                    "name": plugin_data.name,
                    "version": plugin_data.version,
                    "description": plugin_data.description,
                    "authors": authors,
                    "dependencies": {k: str(v) for k, v in plugin_data.dependencies.items()},
                    "labels": labels,
                    "repository_url": plugin_data.link,
                    "update_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "latest_version": plugin_data.latest_version,
                    "license": license_key,
                    "license_url": license_url,
                    "downloads": total_downloads,
                    "readme_url": readme_url,
                }
                
                # 添加最后更新时间
                if latest_release and hasattr(latest_release, 'created_at'):
                    try:
                        # 将ISO格式时间转换为更友好的格式
                        dt = datetime.datetime.fromisoformat(latest_release.created_at.replace('Z', '+00:00'))
                        plugin_entry["last_update_time"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except Exception as time_error:
                        server.logger.error(f"处理插件 {plugin_id} 的时间信息时出错: {time_error}")
                        plugin_entry["last_update_time"] = latest_release.created_at if hasattr(latest_release, 'created_at') else ''
                
                plugins_data.append(plugin_entry)
            except Exception as plugin_error:
                server.logger.error(f"处理插件 {plugin_id} 时出错: {plugin_error}")
                # 继续处理下一个插件
        
        return plugins_data
        
    except Exception as e:
        # 下载或解析出错，记录详细错误信息
        import traceback
        error_msg = f"获取在线插件列表失败: {str(e)}\n{traceback.format_exc()}"
        if server:
            server.logger.error(error_msg)
        else:
            print(error_msg)
        return []

# Loading/Unloading pluging
@app.post("/api/toggle_plugin")
async def toggle_plugin(request: Request, request_body:toggleconfig):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    server:PluginServerInterface = app.state.server_interface
    plugin_id = request_body.plugin_id
    target_status = request_body.status

    # reload only for guguwebui
    if plugin_id == "guguwebui":
        server.reload_plugin(plugin_id)
    # loading
    elif target_status == True:
        _, unloaded_plugin_metadata, unloaded_plugin, disabled_plugin = (
            load_plugin_info(server)
        )
        plugin_path = unloaded_plugin_metadata.get(plugin_id, {}).get("path")
        # plugin not found
        if not plugin_path:
            return JSONResponse(
                {"status": "error", "message": "Plugin not found"}, status_code=404
            )
        # enable the plugin before load it
        if plugin_path in disabled_plugin:
            server.enable_plugin(plugin_path)
        server.load_plugin(plugin_path)
    # unload
    elif target_status == False:
        server.unload_plugin(plugin_id)
    return JSONResponse({"status": "success"})


# Reload Plugin
@app.post("/api/reload_plugin")
async def reload_plugin(request: Request, plugin_info:plugin_info):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    plugin_id = plugin_info.plugin_id
    if plugin_id == "guguwebui":
        return JSONResponse({"status": "error", "message": "无法处理自身"})
    server:PluginServerInterface = app.state.server_interface

    server_response = server.reload_plugin(plugin_id)

    if server_response: # sucess
        return JSONResponse({"status": "success"})

    return JSONResponse({"status": "error", "message": f"Reload {plugin_id} failed"}, status_code=500)   

# List all config files for a plugin
@app.get("/api/list_config_files")
async def list_config_files(request: Request, plugin_id:str):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    config_path_list:list[str] = find_plugin_config_paths(plugin_id)
    return JSONResponse({"files": config_path_list})


@app.get("/api/get_web_config")
async def get_web_config(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    server = app.state.server_interface
    config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
    return JSONResponse(
        {   
            "host": config["host"],
            "port": config["port"],
            "super_admin_account": config["super_admin_account"],
            "disable_admin_login_web": config["disable_other_admin"],
            "enable_temp_login_password": config["allow_temp_password"],
            "ai_api_key": "",
            "ai_model": config.get("ai_model", "deepseek-chat"),
            "ai_api_url": config.get("ai_api_url", "https://api.deepseek.com/chat/completions"),
            "mcdr_plugins_url": config.get("mcdr_plugins_url", "https://api.mcdreforged.com/catalogue/everything_slim.json.xz"),
            "repositories": config.get("repositories", []),
            "ssl_enabled": config.get("ssl_enabled", False),
            "ssl_certfile": config.get("ssl_certfile", ""),
            "ssl_keyfile": config.get("ssl_keyfile", ""),
            "ssl_keyfile_password": config.get("ssl_keyfile_password", ""),
        }
    )


@app.post("/api/save_web_config")
async def save_web_config(request: Request, config: saveconfig):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    server = app.state.server_interface
    web_config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
    
    # change port & account
    if config.action == "config":
        if config.host:
            web_config["host"] = config.host
        if config.port:
            web_config["port"] = int(config.port)
        if config.superaccount:
            web_config["super_admin_account"] = int(config.superaccount)
        # 更新AI配置 - 处理None值，避免将None保存到配置中
        if config.ai_api_key is not None:
            # JavaScript端undefined会被转为null，处理这种情况
            if isinstance(config.ai_api_key, str):
                web_config["ai_api_key"] = config.ai_api_key
        if config.ai_model is not None:
            if isinstance(config.ai_model, str):
                web_config["ai_model"] = config.ai_model
        if config.ai_api_url is not None:
            if isinstance(config.ai_api_url, str):
                web_config["ai_api_url"] = config.ai_api_url
        # 更新MCDR插件目录URL
        if config.mcdr_plugins_url is not None:
            if isinstance(config.mcdr_plugins_url, str):
                web_config["mcdr_plugins_url"] = config.mcdr_plugins_url
        # 更新仓库列表
        if config.repositories is not None:
            web_config["repositories"] = config.repositories
        # 更新SSL配置
        if config.ssl_enabled is not None:
            web_config["ssl_enabled"] = config.ssl_enabled
        if config.ssl_certfile is not None:
            if isinstance(config.ssl_certfile, str):
                web_config["ssl_certfile"] = config.ssl_certfile
        if config.ssl_keyfile is not None:
            if isinstance(config.ssl_keyfile, str):
                web_config["ssl_keyfile"] = config.ssl_keyfile
        if config.ssl_keyfile_password is not None:
            if isinstance(config.ssl_keyfile_password, str):
                web_config["ssl_keyfile_password"] = config.ssl_keyfile_password
        
        response = {"status": "success", "message": "配置已保存，重启插件后生效"}
    # disable_admin_login_web & enable_temp_login_password
    elif config.action in ["disable_admin_login_web", "enable_temp_login_password"]:
        config_map = {
            "disable_admin_login_web": "disable_other_admin",
            "enable_temp_login_password": "allow_temp_password",
        }
        web_config[config_map[config.action]] = not web_config[
            config_map[config.action]
        ]
        response = {
            "status": "success",
            "message": web_config[config_map[config.action]],
        }
    elif config.action == "toggle_ssl":
        web_config["ssl_enabled"] = not web_config.get("ssl_enabled", False)
        response = {
            "status": "success",
            "message": web_config["ssl_enabled"],
        }
    else:
        response = {"status": "error", "message": "Invalid action"}
    
    try:
        # 检查MCDR服务器接口的save_config_simple方法签名
        # 打印出调试信息
        server.logger.debug(f"保存配置: file_name='config.json'")
        
        # 直接使用模块函数而不是server.save_config_simple
        from pathlib import Path
        import json
        
        # 确保配置目录存在
        config_dir = server.get_data_folder()
        Path(config_dir).mkdir(parents=True, exist_ok=True)
        
        # 构建配置文件路径
        config_path = Path(config_dir) / "config.json"
        
        # 直接保存JSON文件
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(web_config, f, ensure_ascii=False, indent=4)
            
        server.logger.debug(f"配置已保存到 {config_path}")
        return JSONResponse(response)
    except Exception as e:
        import traceback
        error_stack = traceback.format_exc()
        server.logger.error(f"保存配置文件时出错: {str(e)}\n{error_stack}")
        return JSONResponse({"status": "error", "message": f"保存配置文件失败: {str(e)}"}, status_code=500)


# Load config data & Load config translation
@app.get("/api/load_config")
async def load_config(request: Request, path:str, translation:bool = False, type:str = "auto"):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    path:Path = Path(path)
    server:PluginServerInterface = app.state.server_interface
    MCDR_language:str = server.get_mcdr_language()

    # 提取 config/chat_with_deepseek 目录
    config_dir = path.parent
    main_json_path = config_dir / "main.json"

    if type == "auto":
        # 读取 main.json
        main_config = {}
        if main_json_path.exists():
            try:
                with open(main_json_path, "r", encoding="UTF-8") as f:
                    main_config = json.load(f)
            except Exception:
                pass  # 解析失败则保持 main_config 为空字典

        # 获取 config.json 的值（可能指向 HTML 文件）
        config_value = main_config.get(path.name)  # 这里 path.name 应该是 "config.json"
        if config_value:
            html_path = config_dir / config_value  # 构造 HTML 文件路径
            if html_path.exists() and html_path.suffix == ".html":
                try:
                    with open(html_path, "r", encoding="UTF-8") as f:
                        return JSONResponse({"status": "success", "type": "html", "content": f.read()})
                except Exception:
                    return JSONResponse(
                        {"status": "error", "message": "Failed to read HTML file"},
                        status_code=500,
                    )

    # Translation for xxx.json -> xxx_lang.json
    if translation:
        if path.suffix in [".json", ".properties"]:
            path = path.with_stem(f"{path.stem}_lang")
        if path.suffix == ".properties":
            path = path.with_suffix(f".json")
        
    if not path.exists(): # file not exists
        return JSONResponse({}, status_code=200)  

    try:
        with open(path, "r", encoding="UTF-8") as f:
            if path.suffix == ".json":
                config = json.load(f)
            elif path.suffix in [".yml", ".yaml"]:
                config = yaml.load(f)
            elif path.suffix == ".properties":
                config = javaproperties.load(f)
                # convert string "true" "false" to True False
                config = {k:v if v not in ["true", "false"] else 
                          True if v == "true" else False 
                          for k,v in config.items()}
    except json.JSONDecodeError:
        if path.suffix == ".json":
            config = {}
    except UnicodeDecodeError:
        # Handle encoding errors
        with open(path, "r", encoding="UTF-8", errors="replace") as f:
            if path.suffix == ".json":
                config = json.load(f)

    if translation:
        # Get corresponding language
        if path.suffix in [".json", ".properties"]:
            config = config.get(MCDR_language) or config.get("en_us") or {}
        # Translation for yaml -> comment in yaml file
        elif translation and path.suffix in [".yml", ".yaml"]:
            config = get_comment(config)

    return JSONResponse(config)


# Helper function for save_config
# ensure consistent data type
def consistent_type_update(original, updates, remove_missing=False):
    """
    更新配置数据，保持类型一致性
    
    Args:
        original: 原始配置数据
        updates: 新的配置数据
        remove_missing: 是否删除原始数据中存在但新数据中不存在的键
    """
    # 如果启用删除功能，先找出需要删除的键
    if remove_missing and isinstance(original, dict) and isinstance(updates, dict):
        keys_to_remove = [key for key in original if key not in updates]
        for key in keys_to_remove:
            del original[key]
    
    # 更新现有键或添加新键
    for key, value in updates.items():
        # setting to None
        if key in original and original[key] is None and \
            (not value or (isinstance(value,list) and not any(value))):
            continue
        # dict -> recurssive update
        elif isinstance(value, dict) and key in original and isinstance(original[key], dict):
            consistent_type_update(original[key], value, remove_missing)
        # get previous type 
        elif isinstance(value, list) and key in original:
            # 如果原值是字典而新值是列表，直接替换
            if isinstance(original[key], dict):
                original[key] = value
                continue
                
            # save old comment
            original_ca = original[key].ca.items if isinstance(original[key], CommentedSeq) else None

            targe_type = list( # search the first type in the original list
                {type(item) for item in original[key] if item}
            ) if original[key] else None

            temp_list = [
                (targe_type[0](item) if targe_type else item) if item else None
                for item in value
            ]

            if original_ca: # save comment to last attribute
                original[key] = CommentedSeq(temp_list)
                original[key].ca.items[len(original[key])-1] = original_ca[max(original_ca)]
            else:
                original[key] = temp_list

        # Force type convertion
        elif key in original and original[key]:
            original_type = type(original[key])
            original[key] = original_type(value)  
        # new attributes
        else:
            original[key] = value


# /api/save_config {plugin_id, file_name, config_data}
@app.post("/api/save_config")
async def save_config(request: Request, config_data: config_data):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    
    config_path = Path(config_data.file_path)
    if config_path == Path("config\\guguwebui\\config.json"):
        return JSONResponse({"status": "error", "message": "无法在此处修改guguwebui配置文件"})

    plugin_config = config_data.config_data

    if not config_path.exists():
        return JSONResponse({"status": "fail", "message": "plugin config not found"})

    try:
        # load original config data
        with open(config_path, "r", encoding="UTF-8") as f:
            if config_path.suffix == ".json":
                data = json.load(f)
            elif config_path.suffix in [".yml", ".yaml"]:
                data = yaml.load(f)
            elif config_path.suffix == ".properties":
                data = javaproperties.load(f)
                # convert back the True False to "true" "false"
                plugin_config = {k: v if not isinstance(v, bool) else 
                                 "true" if v else "false" 
                                 for k, v in plugin_config.items()}
    except Exception as e:
        print(f"Error loading config file: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    # ensure type will not change
    try:
        # 对于JSON文件，允许删除不存在的键
        if config_path.suffix == ".json":
            # 特殊处理help_msg.json配置
            if config_path.name == "help_msg.json" and isinstance(plugin_config, dict):
                # 对于help_msg.json，只更新admin_help_msg和group_help_msg字段
                allowed_fields = ['admin_help_msg', 'group_help_msg']
                # 仅更新允许的字段
                for field in allowed_fields:
                    if field in plugin_config:
                        data[field] = plugin_config[field]
            # 如果提交的配置是空对象，则需确认用户是否真的想清空
            elif isinstance(plugin_config, dict) and len(plugin_config) == 0 and len(data) > 0:
                # 执行删除所有键的操作
                data.clear()
            else:
                # 正常更新操作，同时删除缺失的键
                consistent_type_update(data, plugin_config, remove_missing=True)
        else:
            # 对于YAML和properties文件，保持原有行为，不删除键
            consistent_type_update(data, plugin_config, remove_missing=False)
    except Exception as e:
        print(f"Error updating config data: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)

    try:
        # save config data
        with open(config_path, "w", encoding="UTF-8") as f:
            if config_path.suffix == ".json":
                json.dump(data, f, ensure_ascii=False, indent=4)
            elif config_path.suffix in [".yml", ".yaml"]:
                yaml.dump(data, f)
            elif config_path.suffix == ".properties":
                javaproperties.dump(data, f)
        return JSONResponse({"status": "success", "message": "配置文件保存成功"})
    except Exception as e:
        print(f"Error saving config file: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=500)


# load overall.js / overall.css
@app.get("/api/load_file", response_class=PlainTextResponse)
async def load_file(request: Request, file: str):
    if not request.session.get("logged_in"):
        return JSONResponse({"status": "error", "message": "User not logged in"}, status_code=401)
    file_path = CSS_FILE if file == "css" else JS_FILE
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"{file} file not found")


# save overall.js / overall.css
@app.post("/api/save_file")
async def save_css(request: Request, data: SaveContent):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    file_path = CSS_FILE if data.action == "css" else JS_FILE
    with open(file_path, "w", encoding="utf-8") as file:
        file.write(data.content)
    return {"status": "success", "message": f"{data.action} saved successfully"}


# load config file
@app.get("/api/load_config_file", response_class=PlainTextResponse)
async def load_config_file(request: Request, path: str):
    if not request.session.get("logged_in"):
        return JSONResponse({"status": "error", "message": "User not logged in"}, status_code=401)
    try:
        with open(path, "r", encoding="utf-8") as file:
            return file.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"{file} file not found")

# save config file
@app.post("/api/save_config_file")
async def save_config_file(request: Request, data: SaveContent):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    path = data.action
    if path == "config\\guguwebui\\config.json":
        return JSONResponse({"status": "error", "message": "无法在此处修改guguwebui配置文件"})
    with open(path, "w", encoding="utf-8") as file:
        file.write(data.content)
    return {"status": "success", "message": f"{data.action} saved successfully"}

# read MC server status
@app.get("/api/get_server_status")
async def get_server_status(request: Request):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    server:PluginServerInterface = app.state.server_interface

    server_status = "online" if server.is_server_running() or server.is_server_startup() else "offline"
    server_message = get_java_server_info()

    server_version = server_message.get("server_version", "")
    version_string = f"Version: {server_version}" if server_version else ""
    player_count = server_message.get("server_player_count")
    max_player = server_message.get("server_maxinum_player_count")
    player_string = f"{player_count}/{max_player}" if player_count and max_player else ""

    result = {
        "status": server_status,
        "version": version_string, 
        "players": player_string, 
    }

    return JSONResponse(result)

# 控制Minecraft服务器
@app.post("/api/control_server")
async def control_server(request: Request, control_info: server_control):
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    
    server:PluginServerInterface = app.state.server_interface
    action = control_info.action
    
    allowed_actions = ["start", "stop", "restart"]
    if action not in allowed_actions:
        return JSONResponse(
            {"status": "error", "message": f"无效的操作: {action}，允许的操作: {', '.join(allowed_actions)}"}, 
            status_code=400
        )
    
    try:
        # 发送命令到MCDR
        server.execute_command(f"!!MCDR server {action}")
        
        # 根据操作返回对应的消息
        messages = {
            "start": "服务器启动命令已发送",
            "stop": "服务器停止命令已发送",
            "restart": "服务器重启命令已发送"
        }
        
        return JSONResponse({
            "status": "success",
            "message": messages[action]
        })
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": f"执行命令出错: {str(e)}"}, 
            status_code=500
        )

# 获取服务器日志
@app.get("/api/server_logs")
async def get_server_logs(request: Request, start_line: int = 0, max_lines: int = 100):
    """
    获取服务器日志
    
    Args:
        start_line: 开始行号（0为文件开始）
        max_lines: 最大返回行数（防止返回过多数据）
    """
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    
    try:
        # 限制最大返回行数，防止过多数据导致性能问题
        if max_lines > 500:
            max_lines = 500
        
        # 使用全局LogWatcher实例
        global log_watcher
        
        # 获取合并日志
        result = log_watcher.get_merged_logs(max_lines)
        
        # 格式化合并日志内容
        formatted_logs = []
        for i, log in enumerate(result["logs"]):
            formatted_logs.append({
                "line_number": i,
                "content": log["content"],
                "source": log["source"],
                "counter": log.get("sequence_num", i)
            })
        
        return JSONResponse({
            "status": "success",
            "logs": formatted_logs,
            "total_lines": result["total_lines"],
            "current_start": result["start_line"],
            "current_end": result["end_line"]
        })
        
    except Exception as e:
        import traceback
        error_msg = f"获取日志失败: {str(e)}\n{traceback.format_exc()}"
        app.state.server_interface.logger.error(error_msg)
        return JSONResponse(
            {"status": "error", "message": str(e)}, 
            status_code=500
        )

# 获取新增日志（基于计数器）
@app.get("/api/new_logs")
async def get_new_logs(request: Request, last_counter: int = 0, max_lines: int = 100):
    """
    获取指定计数器ID之后的新增日志
    
    Args:
        last_counter: 上次获取的最后一条日志的计数器ID
        max_lines: 最大返回行数
    """
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    
    try:
        # 限制最大返回行数
        if max_lines > 200:
            max_lines = 200
        
        # 使用全局LogWatcher实例
        global log_watcher
        
        # 获取新增日志
        result = log_watcher.get_logs_since_counter(last_counter, max_lines)
        
        return JSONResponse({
            "status": "success",
            "logs": result["logs"],
            "total_lines": result["total_lines"],
            "last_counter": result["last_counter"],
            "new_logs_count": result["new_logs_count"]
        })
        
    except Exception as e:
        import traceback
        error_msg = f"获取新日志失败: {str(e)}\n{traceback.format_exc()}"
        app.state.server_interface.logger.error(error_msg)
        return JSONResponse(
            {"status": "error", "message": str(e)}, 
            status_code=500
        )

@app.get("/terminal")
async def terminal_page(request: Request):
    """提供终端日志页面
    
    Args:
        request: FastAPI请求对象
    
    Returns:
        TemplateResponse: 终端日志页面
    """
    # 检查是否已登录
    username = request.session.get("username")
    if not username:
        return RedirectResponse(url="login?redirect=/terminal")
    
    return templates.TemplateResponse("terminal.html", {"request": request})

# 获取命令补全建议
@app.get("/api/command_suggestions")
async def get_command_suggestions(request: Request, input: str = ""):
    """
    获取MCDR命令补全建议
    
    Args:
        input: 用户当前输入的命令前缀
    """
    if not request.session.get("logged_in"):
        return JSONResponse({"status": "error", "message": "User not logged in"}, status_code=401)
    
    try:
        # 获取服务器接口
        server = app.state.server_interface
        
        # 如果MCDR服务器接口不可用，返回空列表
        if not server:
            return JSONResponse({"status": "success", "suggestions": []})
        
        # 获取命令管理器
        command_manager = getattr(server, "_mcdr_server", None)
        if not command_manager:
            return JSONResponse({"status": "success", "suggestions": []})
        command_manager = getattr(command_manager, "command_manager", None)
        if not command_manager:
            return JSONResponse({"status": "success", "suggestions": []})
        
        # 获取根命令节点
        root_nodes = getattr(command_manager, "root_nodes", {})
        
        # 命令建议列表
        suggestions = []
        
        # 将输入分割为命令部分
        parts = input.strip().split()
        
        # 检查输入是否以空格结尾，这表示用户需要子命令补全
        input_ends_with_space = input.endswith(' ')
        
        # 如果是空输入或者只有 !! 前缀，返回所有根命令
        if not parts or (len(parts) == 1 and parts[0].startswith("!!") and not input_ends_with_space):
            prefix = parts[0] if parts else ""
            # 收集所有以输入前缀开头的根命令
            for root_command in root_nodes.keys():
                if root_command.startswith(prefix):
                    suggestions.append({
                        "command": root_command,
                        "description": f"命令: {root_command}"
                    })
        # 如果是根命令后面跟空格，需要返回子命令
        elif len(parts) == 1 and parts[0] in root_nodes and input_ends_with_space:
            root_command = parts[0]
            # 遍历所有持有该根命令的插件
            for holder in root_nodes[root_command]:
                node = holder.node
                # 遍历根命令的所有子节点，返回所有可能的子命令
                for child in node.get_children():
                    # 字面量节点
                    if hasattr(child, "literals"):
                        for literal in child.literals:
                            suggestions.append({
                                "command": f"{root_command} {literal}",
                                "description": f"子命令: {literal}"
                            })
                    # 参数节点
                    elif hasattr(child, "get_name"):
                        param_name = child.get_name()
                        suggestions.append({
                            "command": f"{root_command} <{param_name}>",
                            "description": f"参数: {param_name}"
                        })
        # 否则尝试查找命令树中的补全
        else:
            # 当前输入的第一个部分（根命令）
            root_command = parts[0]
            
            # 查找匹配的根命令
            if root_command in root_nodes:
                # 遍历所有持有该根命令的插件
                for holder in root_nodes[root_command]:
                    node = holder.node
                    current_node = node
                    
                    # 依次匹配输入的每个部分
                    matched = True
                    # 如果最后一部分不是完整的命令（没有空格结尾），只处理到倒数第二部分
                    process_until = len(parts) - (0 if parts[-1].strip() and input_ends_with_space else 1)
                    
                    # 保存当前节点的路径，用于记录经过的参数节点
                    path_nodes = []
                    
                    for i in range(1, process_until):
                        part = parts[i]
                        found = False
                        
                        # 先尝试字面量节点匹配
                        for child in current_node.get_children():
                            # 字面量节点匹配
                            if hasattr(child, "literals"):
                                for literal in child.literals:
                                    if literal == part:  # 完全匹配
                                        current_node = child
                                        found = True
                                        path_nodes.append({"type": "literal", "node": child, "value": part})
                                        break
                                if found:
                                    break
                        
                        # 如果字面量节点未匹配，尝试参数节点
                        if not found:
                            for child in current_node.get_children():
                                if hasattr(child, "get_name"):
                                    # 参数节点，记录参数名称和值
                                    current_node = child
                                    found = True
                                    path_nodes.append({
                                        "type": "argument", 
                                        "node": child, 
                                        "name": child.get_name(),
                                        "value": part
                                    })
                                    break
                        
                        if not found:
                            matched = False
                            break
                    
                    # 如果前面的部分都匹配，找最后一部分的补全建议
                    if matched:
                        # 获取最后一部分作为前缀
                        last_part = parts[-1] if len(parts) > 1 and not input_ends_with_space else ""
                        
                        # 获取完整的命令前缀（不包括最后一部分）
                        prefix = " ".join(parts[:-1]) if last_part else " ".join(parts)
                        if prefix and not prefix.endswith(" "):
                            prefix += " "
                        
                        # 如果输入以空格结尾，我们应该提供下一级的完整建议列表
                        if input_ends_with_space:
                            # 遍历当前节点的所有子节点，查找可能的补全
                            for child in current_node.get_children():
                                # 字面量节点
                                if hasattr(child, "literals"):
                                    for literal in child.literals:
                                        # 构建完整的命令补全
                                        full_command = prefix + literal
                                        suggestions.append({
                                            "command": full_command,
                                            "description": f"子命令: {literal}"
                                        })
                                # 参数节点
                                elif hasattr(child, "get_name"):
                                    param_name = child.get_name()
                                    # 构建带参数提示的命令补全
                                    full_command = prefix + f"<{param_name}>"
                                    suggestions.append({
                                        "command": full_command,
                                        "description": f"参数: {param_name}"
                                    })
                        else:
                            # 处理没有以空格结尾的情况，对最后一部分进行前缀匹配
                            # 遍历当前节点的所有子节点，查找可能的补全
                            for child in current_node.get_children():
                                # 字面量节点
                                if hasattr(child, "literals"):
                                    for literal in child.literals:
                                        # 如果最后部分为空，或者literal以最后部分开头，则添加为建议
                                        if not last_part or literal.startswith(last_part):
                                            # 构建完整的命令补全
                                            full_command = prefix + literal
                                            suggestions.append({
                                                "command": full_command,
                                                "description": f"子命令: {literal}"
                                            })
                                # 参数节点
                                elif hasattr(child, "get_name"):
                                    param_name = child.get_name()
                                    # 仅当最后部分为空或没有明确指定参数时才添加参数建议
                                    if not last_part or last_part.startswith("<"):
                                        # 构建带参数提示的命令补全
                                        full_command = prefix + f"<{param_name}>"
                                        suggestions.append({
                                            "command": full_command,
                                            "description": f"参数: {param_name}"
                                        })
                        
                        # 如果最后一个参数有可能的匹配值（例如命令+参数情况下）
                        # 并且前一个节点是参数节点，尝试提供参数后的可能子命令
                        if input_ends_with_space and path_nodes and path_nodes[-1]["type"] == "argument":
                            # 假设用户已经输入了参数值，展示参数后可能的子命令
                            param_node = path_nodes[-1]["node"]
                            # 构建用于显示的完整命令前缀
                            param_prefix = prefix.strip()  # 移除末尾空格
                            
                            # 遍历参数节点的子节点
                            for child in param_node.get_children():
                                if hasattr(child, "literals"):
                                    for literal in child.literals:
                                        # 添加参数后可能的子命令
                                        full_command = f"{param_prefix} {literal}"
                                        suggestions.append({
                                            "command": full_command,
                                            "description": f"子命令: {literal}"
                                        })
                                elif hasattr(child, "get_name"):
                                    # 参数节点后还有参数
                                    next_param_name = child.get_name()
                                    full_command = f"{param_prefix} <{next_param_name}>"
                                    suggestions.append({
                                        "command": full_command,
                                        "description": f"参数: {next_param_name}"
                                    })
        
        # 按命令字母排序
        suggestions.sort(key=lambda x: x["command"])
        
        # 限制返回数量，避免太多
        max_suggestions = 100
        if len(suggestions) > max_suggestions:
            suggestions = suggestions[:max_suggestions]
        
        return JSONResponse({
            "status": "success", 
            "suggestions": suggestions,
            "input": input
        })
        
    except Exception as e:
        import traceback
        error_msg = f"获取命令补全失败: {str(e)}\n{traceback.format_exc()}"
        app.state.server_interface.logger.error(error_msg)
        return JSONResponse(
            {"status": "error", "message": str(e)}, 
            status_code=500
        )

@app.post("/api/send_command")
async def send_command(request: Request):
    """
    发送命令到MCDR终端
    """
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "User not logged in"}, status_code=401
        )
    
    try:
        # 获取请求体中的命令
        data = await request.json()
        command = data.get("command", "").strip()
        
        if not command:
            return JSONResponse(
                {"status": "error", "message": "Command cannot be empty"}, 
                status_code=400
            )
            
        # 检查是否为禁止的命令
        forbidden_commands = [
            '!!MCDR plugin reload guguwebui',
            '!!MCDR plugin unload guguwebui',
            'stop'
        ]
        if command in forbidden_commands:
            return JSONResponse(
                {"status": "error", "message": "该命令已被禁止执行"}, 
                status_code=403
            )
        
        # 获取MCDR服务器接口
        server:PluginServerInterface = app.state.server_interface
        
        # 输出到MCDR的日志
        server.logger.info(f"发送命令: {command}")
        
        # 处理以/开头的命令，尝试通过RCON发送
        if command.startswith("/"):
            # 去掉开头的/，因为RCON不需要
            mc_command = command[1:]
            
            # 检查RCON是否已连接
            if hasattr(server, "is_rcon_running") and server.is_rcon_running():
                try:
                    # 通过RCON发送命令并获取反馈
                    feedback = server.rcon_query(mc_command)
                    server.logger.info(f"RCON反馈: {feedback}")
                    return JSONResponse({
                        "status": "success",
                        "message": f"Command sent via RCON: {command}",
                        "feedback": feedback
                    })
                except Exception as e:
                    server.logger.error(f"RCON执行命令出错: {str(e)}")
                    # RCON执行失败，回退到普通方式执行
                    server.execute_command(command)
                    return JSONResponse({
                        "status": "success",
                        "message": f"Command sent (RCON failed): {command}",
                        "error": str(e)
                    })
            else:
                server.logger.info("RCON未启用，使用普通方式发送命令")
        
        # 普通方式执行命令
        server.execute_command(command)
        
        return JSONResponse({
            "status": "success",
            "message": f"Command sent: {command}"
        })
    except Exception as e:
        return JSONResponse(
            {"status": "error", "message": f"Error sending command: {str(e)}"}, 
            status_code=500
        )

@app.post("/api/deepseek")
async def query_deepseek(request: Request, query_data: DeepseekQuery):
    """
    向AI API发送问题并获取回答
    
    Args:
        request: FastAPI请求对象
        query_data: 查询数据，包含问题内容
    
    Returns:
        JSONResponse: AI回答内容
    """
    # 检查是否已登录
    if not request.session.get("logged_in"):
        return JSONResponse(
            {"status": "error", "message": "用户未登录"}, status_code=401
        )
    
    try:
        # 加载配置
        server = app.state.server_interface
        config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
        
        # 获取API密钥 - 优先使用请求中提供的临时api_key参数(用于验证)
        api_key = getattr(query_data, "api_key", None) or config.get("ai_api_key", "")
        if not api_key:
            return JSONResponse(
                {"status": "error", "message": "未配置AI API密钥"}, 
                status_code=400
            )
        
        # 获取模型配置
        model = query_data.model or config.get("ai_model", "deepseek-chat")
        
        # 获取API URL
        api_url = query_data.api_url or config.get("ai_api_url", "https://api.deepseek.com/chat/completions")
        
        # 检查查询内容
        query = query_data.query.strip()
        if not query:
            return JSONResponse(
                {"status": "error", "message": "查询内容不能为空"}, 
                status_code=400
            )
        
        # 准备API请求
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}"
        }
        
        # 构建消息
        messages = []
        
        # 添加系统指令（如果有）
        if query_data.system_prompt:
            messages.append({
                "role": "system",
                "content": query_data.system_prompt
            })
        
        # 添加用户问题
        messages.append({
            "role": "user",
            "content": query
        })
        
        # 准备请求数据
        json_data = {
            "model": model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 4000
        }
        
        # 发送请求到AI API
        async with aiohttp.ClientSession() as session:
            async with session.post(
                api_url, 
                headers=headers, 
                json=json_data
            ) as response:
                # 解析响应
                result = await response.json()
                
                if response.status != 200:
                    error_msg = result.get("error", {}).get("message", "未知错误")
                    return JSONResponse(
                        {"status": "error", "message": f"API错误: {error_msg}"}, 
                        status_code=response.status
                    )
                
                # 从响应中提取AI回答
                answer = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                return JSONResponse({
                    "status": "success",
                    "answer": answer,
                    "model": model
                })
                
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"AI API请求失败: {str(e)}")
        return JSONResponse(
            {"status": "error", "message": f"请求失败: {str(e)}"}, 
            status_code=500
        )

# PIM插件安装和任务模型
class PluginInstallRequest:
    def __init__(self, plugin_id: str):
        self.plugin_id = plugin_id

class TaskStatusRequest:
    def __init__(self, task_id: str):
        self.task_id = task_id
        
# ============================================================#
# PIM API 接口
@app.post("/api/pim/install_plugin")
async def api_install_plugin(
    request: Request, 
    plugin_req: dict = Body(...),
    token_valid: bool = Depends(verify_token)
):
    """
    安装指定的插件
    
    可接受的参数:
    - plugin_id: 必需，插件ID
    - version: 可选，指定版本号
    - repo_url: 可选，指定仓库URL
    """
    if not token_valid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "error": "未登录或会话已过期"}
        )
    
    plugin_id = plugin_req.get("plugin_id")
    version = plugin_req.get("version")
    repo_url = plugin_req.get("repo_url")
    
    if not plugin_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "error": "缺少插件ID"}
        )
    
    try:
        server = app.state.server_interface
        
        # 首先尝试使用已初始化的实例
        installer = getattr(app.state, "plugin_installer", None)
        if not installer:
            # 如果没有预初始化的实例，创建新的安装器实例
            server.logger.info("使用临时创建的安装器实例")
            installer = create_installer(server)
        
        # 启动异步安装
        task_id = installer.install_plugin(plugin_id, version, repo_url)
        
        # 构建响应消息
        message = f"开始安装插件 {plugin_id}"
        if version:
            message += f" v{version}"
        if repo_url:
            message += f" 从仓库 {repo_url}"
        
        return JSONResponse(
            content={
                "success": True, 
                "task_id": task_id, 
                "message": message
            }
        )
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"安装插件失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"安装插件失败: {str(e)}"}
        )

@app.post("/api/pim/update_plugin")
async def api_update_plugin(
    request: Request, 
    plugin_req: dict = Body(...),
    token_valid: bool = Depends(verify_token)
):
    """
    更新指定的插件到指定版本
    
    可接受的参数:
    - plugin_id: 必需，插件ID
    - version: 可选，指定版本号
    - repo_url: 可选，指定仓库URL
    """
    if not token_valid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "error": "未登录或会话已过期"}
        )
    
    plugin_id = plugin_req.get("plugin_id")
    version = plugin_req.get("version")
    repo_url = plugin_req.get("repo_url")
    
    if not plugin_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "error": "缺少插件ID"}
        )
    
    try:
        server = app.state.server_interface
        
        # 首先尝试使用已初始化的实例
        installer = getattr(app.state, "plugin_installer", None)
        if not installer:
            # 如果没有预初始化的实例，创建新的安装器实例
            server.logger.info("使用临时创建的安装器实例")
            installer = create_installer(server)
        
        # 启动异步安装/更新
        task_id = installer.install_plugin(plugin_id, version, repo_url)
        
        # 构建响应消息
        message = f"开始更新插件 {plugin_id}"
        if version:
            message += f" 到 v{version}"
        else:
            message += " 到最新版本"
        if repo_url:
            message += f" 从仓库 {repo_url}"
        
        return JSONResponse(
            content={
                "success": True, 
                "task_id": task_id, 
                "message": message
            }
        )
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"更新插件失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"更新插件失败: {str(e)}"}
        )

@app.post("/api/pim/uninstall_plugin")
async def api_uninstall_plugin(
    request: Request, 
    plugin_req: dict = Body(...),
    token_valid: bool = Depends(verify_token)
):
    """
    卸载指定的插件，支持卸载并删除未加载的插件
    """
    if not token_valid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "error": "未登录或会话已过期"}
        )
    
    plugin_id = plugin_req.get("plugin_id")
    
    if not plugin_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "error": "缺少插件ID"}
        )
    
    try:
        server = app.state.server_interface
        
        # 首先尝试使用已初始化的实例
        installer = getattr(app.state, "plugin_installer", None)
        if not installer:
            # 如果没有预初始化的实例，创建新的安装器实例
            server.logger.info("使用临时创建的安装器实例")
            installer = create_installer(server)
        
        # 启动异步卸载，同时处理已加载和未加载的插件
        task_id = installer.uninstall_plugin(plugin_id)
        
        return JSONResponse(
            content={
                "success": True, 
                "task_id": task_id, 
                "message": f"开始卸载插件 {plugin_id}"
            }
        )
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"卸载插件失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"卸载插件失败: {str(e)}"}
        )

@app.get("/api/pim/task_status")
async def api_task_status(
    request: Request, 
    task_id: str = None,
    plugin_id: str = None,
    token_valid: bool = Depends(verify_token)
):
    """
    获取任务状态
    
    可以通过任务ID或插件ID获取
    """
    if not token_valid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "error": "未登录或会话已过期"}
        )
    
    try:
        server = app.state.server_interface
        
        # 首先尝试使用已初始化的实例
        installer = getattr(app.state, "plugin_installer", None)
        if not installer:
            # 如果没有预初始化的实例，创建新的安装器实例
            server.logger.info("使用临时创建的安装器实例")
            installer = create_installer(server)
        
        # 如果指定了任务ID，返回单个任务状态
        if task_id:
            task_status = installer.get_task_status(task_id)
            # 如果找不到任务
            if not task_status:
                return JSONResponse(
                    status_code=status.HTTP_404_NOT_FOUND,
                    content={"success": False, "error": f"找不到任务 {task_id}"}
                )
            return JSONResponse(content={"success": True, "task_info": task_status})
        
        # 如果指定了插件ID，返回涉及该插件的所有任务
        elif plugin_id:
            all_tasks = installer.get_all_tasks()
            plugin_tasks = {}
            
            for tid, task in all_tasks.items():
                if task.get('plugin_id') == plugin_id:
                    plugin_tasks[tid] = task
            
            return JSONResponse(content={"success": True, "tasks": plugin_tasks})
        
        # 如果两者都未指定，返回所有任务
        else:
            all_tasks = installer.get_all_tasks()
            return JSONResponse(content={"success": True, "tasks": all_tasks})
            
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"获取任务状态失败: {e}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"获取任务状态失败: {str(e)}"}
        )

@app.get("/api/check_pim_status")
async def check_pim_status(request: Request, token_valid: bool = Depends(verify_token)):
    """检查PIM插件的安装状态"""
    if not request.session.get("logged_in"):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "message": "未登录或会话已过期"}
        )
    
    try:
        # 获取服务器接口
        server = app.state.server_interface
        
        # 获取已加载插件列表
        loaded_plugin_metadata, unloaded_plugin_metadata, loaded_plugin, disabled_plugin = load_plugin_info(server)
        
        # 检查是否有id为pim_helper的插件
        if "pim_helper" in loaded_plugin_metadata or "pim_helper" in unloaded_plugin_metadata:
            status = "installed"
        else:
            status = "not_installed"
        
        return JSONResponse(
            content={
                "status": "success",
                "pim_status": status
            }
        )
    except Exception as e:
        server.logger.error(f"检查PIM状态时出错: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"检查PIM状态时出错: {str(e)}"}
        )

@app.get("/api/install_pim_plugin")
async def install_pim_plugin(request: Request, token_valid: bool = Depends(verify_token)):
    """将PIM作为独立插件安装"""
    if not request.session.get("logged_in"):
        return JSONResponse(
            status_code=401,
            content={"status": "error", "message": "未登录或会话已过期"}
        )
    
    try:
        # 获取服务器接口
        server = app.state.server_interface
        
        # 获取已加载插件列表
        loaded_plugin_metadata, unloaded_plugin_metadata, loaded_plugin, disabled_plugin = load_plugin_info(server)
        
        # 检查是否已安装
        if "pim_helper" in loaded_plugin_metadata or "pim_helper" in unloaded_plugin_metadata:
            return JSONResponse(
                content={
                    "status": "success",
                    "message": "PIM插件已安装"
                }
            )
        
        # 获取MCDR根目录和plugins目录路径
        # mcdr_root = server.get_mcdr_root()  # 这个方法不存在
        # 使用get_data_folder()获取插件数据目录，然后回溯到MCDR根目录
        data_folder = server.get_data_folder()
        mcdr_root = os.path.dirname(os.path.dirname(data_folder))  # 从plugins/guguwebui/config回溯到MCDR根目录
        plugins_dir = os.path.join(mcdr_root, "plugins")
        
        # 创建plugins目录（如果不存在）
        os.makedirs(plugins_dir, exist_ok=True)
        
        # 使用__copyFile从插件包中复制PIM.py
        source_path = "guguwebui/utils/PIM.py"  # 相对于插件包的路径
        target_path = os.path.join(plugins_dir, "pim_helper.py")
        
        # 使用utils中的__copyFile函数
        __copyFile(server, source_path, target_path)
        
        # 加载插件
        server.load_plugin(target_path)
        
        return JSONResponse(
            content={
                "status": "success",
                "message": "PIM插件已成功安装并加载"
            }
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"status": "error", "message": f"安装PIM插件时出错: {str(e)}"}
        )

# 添加新的API端点，使用PluginInstaller获取插件版本
@app.get("/api/pim/plugin_versions_v2")
async def api_get_plugin_versions_v2(
    request: Request, 
    plugin_id: str,
    token_valid: bool = Depends(verify_token)
):
    """
    获取插件的所有可用版本（新版API）
    """
    if not token_valid:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"success": False, "error": "未登录或会话已过期"}
        )
    
    if not plugin_id:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"success": False, "error": "缺少插件ID"}
        )
    
    try:
        server = app.state.server_interface
        
        # 记录请求日志
        server.logger.debug(f"请求获取插件版本: {plugin_id}")
        
        # 首先尝试使用已初始化的 PluginInstaller 实例
        plugin_installer = getattr(app.state, "plugin_installer", None)
        if plugin_installer:
            server.logger.debug(f"使用已初始化的插件安装器获取版本信息")
            versions = plugin_installer.get_plugin_versions(plugin_id)
        else:
            # 如果没有预初始化的实例，创建临时安装器
            server.logger.info(f"使用临时创建的安装器获取版本信息")
            installer = create_installer(server)
            versions = installer.get_plugin_versions(plugin_id)
        
        # 记录结果
        if versions:
            server.logger.debug(f"成功获取插件 {plugin_id} 的 {len(versions)} 个版本")
        else:
            server.logger.debug(f"获取插件 {plugin_id} 版本列表为空")
            
        # 返回版本列表
        return JSONResponse(
            content={
                "success": True, 
                "versions": versions
            }
        )
    except Exception as e:
        server = app.state.server_interface
        server.logger.error(f"获取插件版本失败: {e}")
        import traceback
        server.logger.debug(traceback.format_exc())
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"success": False, "error": f"获取插件版本失败: {str(e)}"}
        )

# Pip包管理相关模型
class PipPackageRequest(BaseModel):
    package: str

# 定义一个函数用于获取已安装的pip包
def get_installed_pip_packages():
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        stdout, stderr = process.communicate()
        
        if process.returncode != 0:
            return {"status": "error", "message": f"获取包列表失败: {stderr}"}
        
        packages = json.loads(stdout)
        return {"status": "success", "packages": packages}
    except Exception as e:
        return {"status": "error", "message": f"获取包列表时出错: {str(e)}"}

# pip操作的异步任务
async def pip_task(task_id, action, package):
    try:
        output = []
        
        if action == "install":
            cmd = [sys.executable, "-m", "pip", "install", package]
            output.append(f"正在安装包: {package}")
        elif action == "uninstall":
            cmd = [sys.executable, "-m", "pip", "uninstall", "-y", package]
            output.append(f"正在卸载包: {package}")
        else:
            pip_tasks[task_id] = {
                "completed": True,
                "success": False,
                "output": ["不支持的操作类型"],
            }
            return
        
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1
        )
        
        # 更新初始状态
        pip_tasks[task_id] = {
            "completed": False,
            "success": False,
            "output": output.copy(),
        }
        
        # 读取输出
        while True:
            stdout_line = process.stdout.readline()
            if stdout_line:
                output.append(stdout_line.strip())
                pip_tasks[task_id]["output"] = output.copy()
            
            stderr_line = process.stderr.readline()
            if stderr_line:
                output.append(stderr_line.strip())
                pip_tasks[task_id]["output"] = output.copy()
            
            if not stdout_line and not stderr_line and process.poll() is not None:
                break
        
        # 获取最终退出码
        exit_code = process.wait()
        success = exit_code == 0
        
        if success:
            output.append(f"操作成功完成")
        else:
            output.append(f"操作失败，退出码: {exit_code}")
        
        # 更新最终状态
        pip_tasks[task_id] = {
            "completed": True,
            "success": success,
            "output": output,
        }
    except Exception as e:
        error_msg = f"执行pip操作时出错: {str(e)}"
        output.append(error_msg)
        pip_tasks[task_id] = {
            "completed": True,
            "success": False,
            "output": output,
        }

@app.get("/api/pip/list")
async def api_pip_list(request: Request, token_valid: bool = Depends(verify_token)):
    """获取已安装的pip包列表"""
    if not token_valid:
        return {"status": "error", "message": "未授权访问"}
    
    return get_installed_pip_packages()

@app.post("/api/pip/install")
async def api_pip_install(
    request: Request, 
    package_req: PipPackageRequest,
    token_valid: bool = Depends(verify_token)
):
    """安装pip包"""
    if not token_valid:
        return {"status": "error", "message": "未授权访问"}
    
    package = package_req.package.strip()
    if not package:
        return {"status": "error", "message": "包名不能为空"}
    
    # 创建任务ID并启动异步任务
    task_id = str(uuid.uuid4())
    asyncio.create_task(pip_task(task_id, "install", package))
    
    return {"status": "success", "task_id": task_id, "message": f"开始安装 {package}"}

@app.post("/api/pip/uninstall")
async def api_pip_uninstall(
    request: Request, 
    package_req: PipPackageRequest,
    token_valid: bool = Depends(verify_token)
):
    """卸载pip包"""
    if not token_valid:
        return {"status": "error", "message": "未授权访问"}
    
    package = package_req.package.strip()
    if not package:
        return {"status": "error", "message": "包名不能为空"}
    
    # 创建任务ID并启动异步任务
    task_id = str(uuid.uuid4())
    asyncio.create_task(pip_task(task_id, "uninstall", package))
    
    return {"status": "success", "task_id": task_id, "message": f"开始卸载 {package}"}

@app.get("/api/pip/task_status")
async def api_pip_task_status(
    request: Request, 
    task_id: str,
    token_valid: bool = Depends(verify_token)
):
    """获取pip任务状态"""
    if not token_valid:
        return {"status": "error", "message": "未授权访问"}
    
    if not task_id or task_id not in pip_tasks:
        return {"status": "error", "message": "无效的任务ID"}
    
    task_info = pip_tasks[task_id]
    
    return {
        "status": "success",
        "completed": task_info["completed"],
        "success": task_info["success"],
        "output": task_info["output"]
    }
