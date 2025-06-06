import os
import platform

from mcdreforged.api.command import *
from mcdreforged.api.types import PluginServerInterface
import json

# 全局变量声明
web_server_interface = None

#============================================================#


def on_load(server: PluginServerInterface, old):
    global web_server_interface

    server.logger.info("[GUGUWebUI] 启动 WebUI 中...")
    
    # 首先检查并安装依赖包
    try:
        from .utils.dependency_checker import check_and_install_dependencies
        check_and_install_dependencies(server)
    except Exception as e:
        server.logger.error(f"[GUGUWebUI] 依赖检查过程中发生错误: {e}")
        server.logger.warning("[GUGUWebUI] 将尝试继续启动，但可能会遇到导入错误")
    
    # 导入必需的模块
    try:
        # 导入 uvicorn
        import uvicorn
        
        # 导入其他模块 - 明确导入需要的内容而不是使用通配符
        from fastapi.staticfiles import StaticFiles
        from .utils.utils import (
            amount_static_files, 
            create_account_command, 
            change_account_command, 
            get_temp_password_command
        )
        from .utils.constant import user_db
        from .web_server import (
            app, init_app, get_plugins_info, log_watcher, 
            DEFALUT_CONFIG, STATIC_PATH, ThreadedUvicorn
        )
        from .utils.server_util import patch_asyncio
        from .utils.PIM import PluginInstaller, get_installer, create_installer
        
        __all__ = ['PluginInstaller', 'get_installer', 'create_installer']
        
        server.logger.info("[GUGUWebUI] 所有模块导入成功")
        
    except ImportError as e:
        server.logger.error(f"[GUGUWebUI] 导入模块时发生错误: {e}")
        server.logger.error("[GUGUWebUI] 请检查依赖包是否正确安装")
        server.logger.error("[GUGUWebUI] 建议重新启动MCDR让依赖自动安装生效")
        return
    except Exception as e:
        server.logger.error(f"[GUGUWebUI] 导入模块时发生未知错误: {e}")
        return
    
    # 在 Windows 平台应用 asyncio 补丁，防止连接重置错误
    if platform.system() == 'Windows':
        server.logger.debug("[GUGUWebUI] 正在为 Windows 平台应用 asyncio 补丁...")
        patch_asyncio(server)
        server.logger.debug("[GUGUWebUI] asyncio 补丁应用完成")

    plugin_config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
    host = plugin_config['host']
    port = plugin_config['port']
    register_command(server, host, port) # register MCDR command

    amount_static_files(server) # move static resource
    app.mount("/src", StaticFiles(directory=f"{STATIC_PATH}/src"), name="src")
    app.mount("/js", StaticFiles(directory=f"{STATIC_PATH}/js"), name="js")
    app.mount("/css", StaticFiles(directory=f"{STATIC_PATH}/css"), name="css")
    app.mount("/custom", StaticFiles(directory=f"{STATIC_PATH}/custom"), name="custom")
    
    # 初始化应用程序和日志捕获器
    init_app(server)
    
    # 从配置中读取SSL设置
    ssl_enabled = plugin_config.get('ssl_enabled', False)
    
    # 基本配置
    config_params = {
        'app': app,
        'host': host,
        'port': port,
        'log_level': "warning"
    }
    
    # 如果启用了SSL，添加SSL配置
    if ssl_enabled:
        try:
            ssl_keyfile = plugin_config.get('ssl_keyfile', '')
            ssl_certfile = plugin_config.get('ssl_certfile', '')
            ssl_keyfile_password = plugin_config.get('ssl_keyfile_password', '')
            
            if ssl_keyfile and ssl_certfile:
                # 检查文件是否存在
                ssl_files_exist = True
                error_messages = []
                
                if not os.path.exists(ssl_certfile):
                    ssl_files_exist = False
                    error_messages.append(f"SSL证书文件不存在: {ssl_certfile}")
                    server.logger.error(f"[GUGUWebUI] SSL证书文件不存在: {ssl_certfile}")
                
                if not os.path.exists(ssl_keyfile):
                    ssl_files_exist = False
                    error_messages.append(f"SSL密钥文件不存在: {ssl_keyfile}")
                    server.logger.error(f"[GUGUWebUI] SSL密钥文件不存在: {ssl_keyfile}")
                    
                if ssl_files_exist:
                    # 文件都存在，添加SSL配置
                    try:
                        config_params['ssl_keyfile'] = ssl_keyfile
                        config_params['ssl_certfile'] = ssl_certfile
                        if ssl_keyfile_password:
                            config_params['ssl_keyfile_password'] = ssl_keyfile_password
                        server.logger.info("[GUGUWebUI] 已启用HTTPS模式")
                    except Exception as e:
                        server.logger.error(f"[GUGUWebUI] 配置HTTPS时发生错误: {e}")
                        server.logger.warning("[GUGUWebUI] 由于错误，将回退到HTTP模式")
                        ssl_enabled = False
                else:
                    # 文件不存在，回退到HTTP
                    server.logger.warning("[GUGUWebUI] SSL文件不存在，将回退至HTTP模式")
                    for error in error_messages:
                        server.logger.warning(f"[GUGUWebUI] {error}")
                    server.logger.info("[GUGUWebUI] 请在设置页面检查SSL配置，确保文件路径正确并且文件存在")
                    ssl_enabled = False
            else:
                server.logger.warning("[GUGUWebUI] SSL配置不完整，将使用HTTP模式启动")
                ssl_enabled = False
        except Exception as e:
            server.logger.error(f"[GUGUWebUI] 处理SSL配置时发生错误: {e}")
            server.logger.warning("[GUGUWebUI] 由于错误，将回退到HTTP模式")
            ssl_enabled = False
    
    # 创建配置对象
    config = uvicorn.Config(**config_params)
    web_server_interface = ThreadedUvicorn(server, config)

    # 显示URL（根据是否启用SSL选择协议）
    protocol = "https" if ssl_enabled else "http"
    server.logger.info(f"[GUGUWebUI] 网页地址: {protocol}://{host}:{port}")
    web_server_interface.start()
    get_plugins_info(app.state.server_interface, 'true')


def on_unload(server: PluginServerInterface):
    server.logger.info("[GUGUWebUI] 正在卸载 WebUI...")
    
    # 停止日志捕获
    if 'log_watcher' in globals() and log_watcher:
        try:
            log_watcher.stop()
            server.logger.debug("[GUGUWebUI] 日志捕获器已停止")
        except Exception as e:
            server.logger.warning(f"[GUGUWebUI] 停止日志捕获器时出错: {e}")
    
    # 停止Web服务器
    try:
        if 'web_server_interface' in globals() and web_server_interface:
            # 如果使用了SSL，添加特殊处理
            try:
                from .web_server import DEFALUT_CONFIG
                plugin_config = server.load_config_simple("config.json", DEFALUT_CONFIG, echo_in_console=False)
                ssl_enabled = plugin_config.get('ssl_enabled', False)
                
                if ssl_enabled:
                    server.logger.debug("[GUGUWebUI] 检测到HTTPS模式，使用特殊卸载流程")
                    
                    # 尝试特殊处理HTTPS相关资源
                    try:
                        import gc
                        import ssl
                        
                        # 尝试关闭所有SSL相关对象
                        for obj in gc.get_objects():
                            try:
                                if isinstance(obj, ssl.SSLSocket) and hasattr(obj, 'close'):
                                    obj.close()
                            except Exception:
                                pass
                            
                        server.logger.debug("[GUGUWebUI] HTTPS资源清理完成")
                    except Exception as e:
                        server.logger.warning(f"[GUGUWebUI] HTTPS资源清理时出错: {e}")
            except Exception as e:
                server.logger.warning(f"[GUGUWebUI] 检查SSL配置时出错: {e}")
            
            # 正常停止Web服务器
            web_server_interface.stop()
            server.logger.debug("[GUGUWebUI] Web服务器已停止")
    except Exception as e:
        server.logger.warning(f"[GUGUWebUI] 停止Web服务器时出错: {e}")
    
    # 清理事件循环和asyncio相关资源
    try:
        import asyncio
        try:
            # 获取当前事件循环
            try:
                loop = asyncio.get_event_loop()
                if not loop.is_closed():
                    server.logger.debug("[GUGUWebUI] 关闭asyncio事件循环")
                    # 停止所有任务
                    try:
                        for task in asyncio.all_tasks(loop):
                            task.cancel()
                    except Exception:
                        pass
                    
                    try:
                        # 运行一次loop确保任务被取消
                        if not loop.is_closed():
                            loop.run_until_complete(asyncio.sleep(0))
                    except Exception:
                        pass
                    
                    # 关闭事件循环
                    try:
                        if not loop.is_closed():
                            loop.close()
                    except Exception:
                        pass
            except Exception:
                pass
        except Exception as e:
            server.logger.warning(f"[GUGUWebUI] 清理asyncio资源时出错: {e}")
    except ImportError:
        pass
        
    # 强制清理环境
    try:
        import gc
        gc.collect()  # 强制垃圾回收
        server.logger.debug("[GUGUWebUI] 垃圾回收已完成")
    except Exception as e:
        server.logger.warning(f"[GUGUWebUI] 垃圾回收时出错: {e}")
    
    server.logger.info("[GUGUWebUI] WebUI 已卸载")


def register_command(server:PluginServerInterface, host:str, port:int):
    from .utils.utils import get_temp_password_command, create_account_command, change_account_command  # 在函数内部导入所有需要的命令函数
    
    # 注册指令
    server.register_command(
        Literal('!!webui')
        .requires(lambda src: src.has_permission(3))
        .then(
            Literal('create')
            .then(
                Text('account')
                .then(
                    Text('password')
                    .runs(lambda src, ctx: create_account_command(src, ctx, host, port))
                )
            )
        )
        .then(
            Literal('change')
            .then(
                Text('account')
                .then(
                    Text('password')
                    .runs(lambda src, ctx: change_account_command(src, ctx, host, port))
                )
            )
        )
        .then(
            Literal('temp')
            .runs(lambda src, ctx: get_temp_password_command(src, ctx, host, port))
        )
    )

    server.register_help_message("!!webui", "GUGUWebUI 相关指令", 3)

def __get_help_message():
    help_message = "!!webui create <account> <password>: 注册 guguwebui 账户\n"
    help_message += "!!webui change <account> <old password> <new password>: 修改 guguwebui 账户密码\n"   
    help_message += "!!webui temp: 获取 guguwebui 临时密码\n"
    return help_message

    # 初始化 tls.json 配置
def init_tls():    
    try:
        tls_config_path = os.path.join('config', 'guguwebui', 'tls.json')
        if os.path.exists(tls_config_path):
            with open(tls_config_path, 'r', encoding='utf-8') as f:
                tls_config = json.load(f)
            plugin_config.update(tls_config)
            server.logger.info('[GUGUWebUI] 成功加载 tls.json 配置')
        else:
            server.logger.warning('[GUGUWebUI] 未找到 tls.json 配置文件')
    except Exception as e:
        server.logger.error(f'[GUGUWebUI] 加载 tls.json 配置时出错: {e}')
         # 可以在这里将 tls_config 合并到原有的配置中
        plugin_config.update(tls_config)
          # 例如更新 SSL 相关配置
        if 'ssl_enabled' in tls_config:
            ssl_enabled = tls_config['ssl_enabled']
        if 'ssl_keyfile' in tls_config:
            ssl_keyfile = tls_config['ssl_keyfile']
        if 'ssl_certfile' in tls_config:
            ssl_certfile = tls_config['ssl_certfile']
        if 'ssl_keyfile_password' in tls_config:
            ssl_keyfile_password = tls_config['ssl_keyfile_password']
    except FileNotFoundError:
        server.logger.error('[GUGUWebUI] 未找到 tls.json 配置文件')
    except json.JSONDecodeError:
        server.logger.error('[GUGUWebUI] tls.json 配置文件解析失败')