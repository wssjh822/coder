#!/usr/bin/env python3
"""
AI终端助手 - Web版 v7.8
支持终端模式和Web浏览器模式 - 流式思考
新增：system_use命令格式、修复思考重复输出
"""

import subprocess
import sys
import re
import os
import json
import platform
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any, Tuple, Generator
from dataclasses import dataclass
from enum import Enum
import traceback
from threading import Thread, Event
import queue
import time
import uuid
import signal

# ==================== 依赖检查 ====================

def check_and_install_dependencies():
    required_packages = {
        'rich': 'rich',
        'ollama': 'ollama',
        'openai': 'openai',
        'flask': 'flask',
        'flask-socketio': 'flask-socketio',
        'flask-cors': 'flask-cors',
    }
    
    missing = []
    for module_name, package_name in required_packages.items():
        try:
            __import__(module_name.replace('-', '_'))
        except ImportError:
            missing.append(package_name)
    
    if missing:
        print(f"正在安装依赖: {', '.join(missing)}...")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-q"] + missing,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            print("✅ 依赖安装完成")
        except subprocess.CalledProcessError as e:
            print(f"❌ 依赖安装失败: {e}")
            print("请手动安装: pip install rich ollama openai flask flask-socketio flask-cors")
            sys.exit(1)

check_and_install_dependencies()

from rich.console import Console
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text
from rich.layout import Layout
from rich.prompt import Prompt, Confirm
from rich import box
from rich.theme import Theme

from flask import Flask, render_template, request, jsonify, Response, stream_with_context
from flask_socketio import SocketIO, emit
from flask_cors import CORS
import threading

# ==================== 常量定义 ====================

BACKTICK = "```"

CUSTOM_THEME = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "red bold",
    "success": "green bold",
    "title": "bold blue",
    "path": "green underline",
    "thinking": "italic yellow",
})

# ==================== 配置管理 ====================

class ConfigManager:
    """配置管理器"""
    
    CONFIG_DIR = Path(".ai_terminal")
    CONFIG_FILE = CONFIG_DIR / "config.json"
    CONVERSATIONS_DIR = CONFIG_DIR / "conversations"
    SKILLS_DIR = CONFIG_DIR / "skills"
    PLUGINS_DIR = CONFIG_DIR / "plugins"
    
    DEFAULT_CONFIG = {
        "api_type": "openai",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "ollama_model": "qwen2.5:7b",
        "ollama_host": "http://localhost:11434",
        "tavily_api_key": None,
        "system_prompt": "",
        "max_history": 100,
        "timeout": 30,
        "show_thinking": True,
        "auto_save": True,
        "max_iterations": 5,
        "web_mode": False,
        "web_port": 5000,
        "web_host": "0.0.0.0",
        "no_timeout": True,
        "auto_save_conversation": True,
        "max_saved_conversations": 50,
    }
    
    def __init__(self):
        self.config = self._load()
        self._ensure_dirs()
    
    def _ensure_dirs(self):
        self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        self.CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        self.PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
    
    def _load(self) -> Dict:
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return {**self.DEFAULT_CONFIG, **json.load(f)}
            except Exception:
                pass
        return self.DEFAULT_CONFIG.copy()
    
    def save(self):
        try:
            self.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(self.CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"❌ 保存配置失败: {e}")
            return False
    
    def get(self, key: str, default=None):
        return self.config.get(key, self.DEFAULT_CONFIG.get(key, default))
    
    def set(self, key: str, value: Any):
        self.config[key] = value
    
    @property
    def api_type(self) -> str:
        return self.get('api_type')
    
    @property
    def show_thinking(self) -> bool:
        return self.get('show_thinking', True)
    
    @property
    def no_timeout(self) -> bool:
        return self.get('no_timeout', True)
    
    @property
    def auto_save_conversation(self) -> bool:
        return self.get('auto_save_conversation', True)

# ==================== 数据模型 ====================

@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    cwd: str = ""
    success: bool = True
    command_type: str = ""
    original_code: str = ""
    killed: bool = False
    
    def get_output(self, max_length: int = 5000) -> str:
        parts = []
        if self.killed:
            parts.append("⚠️ 命令已被用户停止")
        if self.stdout:
            output = self.stdout.rstrip()
            if len(output) > max_length:
                output = output[:max_length] + f"\n... (截断，共{len(self.stdout)}字符)"
            parts.append(output)
        if self.stderr:
            error = self.stderr.rstrip()
            if len(error) > max_length:
                error = error[:max_length] + f"\n... (截断，共{len(self.stderr)}字符)"
            if self.stdout:
                parts.append(f"\n标准错误:\n{error}")
            else:
                parts.append(f"错误:\n{error}")
        return "\n".join(parts) if parts else ("执行成功（无输出）" if not self.killed else "执行被停止")

# ==================== 对话管理器 ====================

class ConversationManager:
    """对话管理器"""
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self.save_dir = config.CONVERSATIONS_DIR
        self.save_dir.mkdir(parents=True, exist_ok=True)
    
    def save(self, messages: List[Dict], title: str = None) -> Optional[str]:
        if not messages:
            return None
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        if not title:
            for msg in messages:
                if msg['role'] == 'user':
                    title = msg['content'][:50].replace('\n', ' ').strip()
                    break
            if not title:
                title = "未命名对话"
        
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)[:50]
        filename = f"{timestamp}_{safe_title}.json"
        filepath = self.save_dir / filename
        
        try:
            conversation_data = {
                'version': '1.0',
                'timestamp': timestamp,
                'title': title,
                'message_count': len(messages),
                'messages': messages,
                'metadata': {
                    'cwd': os.getcwd(),
                    'platform': platform.system(),
                    'saved_at': datetime.now().isoformat()
                }
            }
            
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump(conversation_data, f, indent=2, ensure_ascii=False)
            
            self._cleanup_old()
            return filename
        except Exception as e:
            print(f"保存对话失败: {e}")
            return None
    
    def load(self, filename: str) -> Optional[Dict]:
        filepath = self.save_dir / filename
        if not filepath.exists():
            return None
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return None
    
    def delete(self, filename: str) -> bool:
        filepath = self.save_dir / filename
        if filepath.exists():
            try:
                filepath.unlink()
                return True
            except Exception:
                return False
        return False
    
    def list_all(self, limit: int = 30) -> List[Dict]:
        conversations = []
        for filepath in sorted(self.save_dir.glob("*.json"), reverse=True):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                preview = "（空对话）"
                for msg in data.get('messages', []):
                    if msg['role'] == 'user':
                        preview = msg['content'][:60]
                        break
                conversations.append({
                    'filename': filepath.name,
                    'timestamp': data.get('timestamp', ''),
                    'title': data.get('title', ''),
                    'message_count': data.get('message_count', 0),
                    'preview': preview,
                    'size': filepath.stat().st_size
                })
                if len(conversations) >= limit:
                    break
            except Exception:
                pass
        return conversations
    
    def _cleanup_old(self):
        max_conv = self.config.get('max_saved_conversations', 50)
        conversations = sorted(
            self.save_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True
        )
        for old_file in conversations[max_conv:]:
            try:
                old_file.unlink()
            except Exception:
                pass

# ==================== 技能管理器 ====================

class SkillManager:
    """技能管理器"""
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self.skills_dir = config.SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._init_default_skills()
    
    def _init_default_skills(self):
        default_skills = {
            "code_review": {
                "id": "code_review",
                "name": "代码审查",
                "description": "审查代码质量、安全性和性能",
                "prompt_template": "请审查以下代码，关注: 1.代码质量 2.安全性 3.性能 4.最佳实践\n\n{code}",
                "category": "开发工具",
                "version": "1.0"
            },
            "file_operations": {
                "id": "file_operations",
                "name": "文件操作",
                "description": "批量文件操作技能",
                "prompt_template": "对以下文件进行操作: {operation}\n\n文件列表:\n{files}",
                "category": "系统管理",
                "version": "1.0"
            },
            "data_analysis": {
                "id": "data_analysis",
                "name": "数据分析",
                "description": "数据分析和可视化建议",
                "prompt_template": "分析以下数据并提供见解:\n\n{data}",
                "category": "数据分析",
                "version": "1.0"
            },
            "web_scraper": {
                "id": "web_scraper",
                "name": "网页抓取",
                "description": "抓取网页内容并提取信息",
                "prompt_template": "抓取以下URL的内容并提取关键信息:\n\nURL: {url}\n需要提取: {target}",
                "category": "网络工具",
                "version": "1.0"
            },
            "system_monitor": {
                "id": "system_monitor",
                "name": "系统监控",
                "description": "监控系统资源使用情况",
                "prompt_template": "检查系统状态，关注: {metrics}\n\n当前系统信息:\n{system_info}",
                "category": "系统管理",
                "version": "1.0"
            }
        }
        
        for skill_id, skill_data in default_skills.items():
            skill_file = self.skills_dir / f"{skill_id}.json"
            if not skill_file.exists():
                skill_data['created_at'] = datetime.now().isoformat()
                with open(skill_file, 'w', encoding='utf-8') as f:
                    json.dump(skill_data, f, indent=2, ensure_ascii=False)
    
    def list_all(self) -> List[Dict]:
        skills = []
        for filepath in self.skills_dir.glob("*.json"):
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    skill = json.load(f)
                    if 'id' in skill:
                        skills.append(skill)
            except Exception:
                pass
        return skills
    
    def get(self, skill_id: str) -> Optional[Dict]:
        filepath = self.skills_dir / f"{skill_id}.json"
        if filepath.exists():
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return None
    
    def list_names(self) -> str:
        """获取技能名称列表（供AI使用）"""
        skills = self.list_all()
        if not skills:
            return "暂无可用技能"
        
        lines = ["可用技能列表:"]
        for s in skills:
            lines.append(f"  - {s['id']}: {s['name']} ({s.get('category', '未分类')})")
            lines.append(f"    {s.get('description', '无描述')}")
        return "\n".join(lines)
    
    def get_skill_detail(self, skill_id: str) -> str:
        """获取技能详情（供AI使用）"""
        skill = self.get(skill_id)
        if not skill:
            return f"技能 '{skill_id}' 不存在"
        
        return json.dumps(skill, indent=2, ensure_ascii=False)

# ==================== 插件管理器 ====================

class PluginManager:
    """插件管理器"""
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self.plugins_dir = config.PLUGINS_DIR
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        self._init_default_plugins()
    
    def _init_default_plugins(self):
        sysinfo_plugin = self.plugins_dir / "system_info.py"
        if not sysinfo_plugin.exists():
            with open(sysinfo_plugin, 'w', encoding='utf-8') as f:
                f.write('''
"""系统信息插件 - 提供系统信息获取功能"""

import platform
import os
import sys

def get_system_info():
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python_version": sys.version,
        "current_dir": os.getcwd(),
        "cpu_count": os.cpu_count(),
    }

def get_disk_usage(path="/"):
    try:
        import shutil
        usage = shutil.disk_usage(path)
        return {
            "total_gb": usage.total / (1024**3),
            "used_gb": usage.used / (1024**3),
            "free_gb": usage.free / (1024**3),
            "percent": usage.used / usage.total * 100
        }
    except Exception as e:
        return {"error": str(e)}

__all__ = ["get_system_info", "get_disk_usage"]
''')
        
        text_plugin = self.plugins_dir / "text_processor.py"
        if not text_plugin.exists():
            with open(text_plugin, 'w', encoding='utf-8') as f:
                f.write('''
"""文本处理插件 - 提供文本处理功能"""

import re

def word_count(text: str) -> dict:
    words = text.split()
    lines = text.split('\\n')
    return {
        "characters": len(text),
        "words": len(words),
        "lines": len(lines),
    }

def extract_urls(text: str) -> list:
    pattern = r'https?://[^\\s]+'
    return re.findall(pattern, text)

def extract_emails(text: str) -> list:
    pattern = r'[\\w.-]+@[\\w.-]+\\.\\w+'
    return re.findall(pattern, text)

__all__ = ["word_count", "extract_urls", "extract_emails"]
''')
        
        file_plugin = self.plugins_dir / "file_utils.py"
        if not file_plugin.exists():
            with open(file_plugin, 'w', encoding='utf-8') as f:
                f.write('''
"""文件工具插件 - 提供文件操作功能"""

import os
import json
from pathlib import Path

def list_files(directory: str = ".", pattern: str = "*") -> list:
    path = Path(directory)
    if not path.exists():
        return [f"目录不存在: {directory}"]
    files = []
    for f in path.glob(pattern):
        info = {
            "name": f.name,
            "size": f.stat().st_size if f.is_file() else 0,
            "is_dir": f.is_dir(),
            "modified": f.stat().st_mtime
        }
        files.append(info)
    return files

def read_file(filepath: str, encoding: str = "utf-8") -> str:
    try:
        with open(filepath, 'r', encoding=encoding) as f:
            return f.read()
    except Exception as e:
        return f"读取错误: {e}"

def write_file(filepath: str, content: str, encoding: str = "utf-8") -> str:
    try:
        with open(filepath, 'w', encoding=encoding) as f:
            f.write(content)
        return f"写入成功: {filepath}"
    except Exception as e:
        return f"写入错误: {e}"

__all__ = ["list_files", "read_file", "write_file"]
''')
    
    def list_all(self) -> List[Dict]:
        plugins = []
        for filepath in self.plugins_dir.glob("*.py"):
            if filepath.name.startswith('_'):
                continue
            info = {
                'filename': filepath.name,
                'size': filepath.stat().st_size,
                'modified': datetime.fromtimestamp(filepath.stat().st_mtime).isoformat()
            }
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()
                    doc_match = re.search(r'"""(.*?)"""', content, re.DOTALL)
                    if doc_match:
                        first_line = doc_match.group(1).strip().split('\n')[0]
                        info['description'] = first_line
                    all_match = re.search(r'__all__\s*=\s*\[(.*?)\]', content, re.DOTALL)
                    if all_match:
                        funcs = re.findall(r'"([^"]+)"', all_match.group(1))
                        info['functions'] = funcs
            except Exception:
                pass
            plugins.append(info)
        return plugins
    
    def list_names(self) -> str:
        """获取插件名称列表（供AI使用）"""
        plugins = self.list_all()
        if not plugins:
            return "暂无可用插件"
        
        lines = ["可用插件列表:"]
        for p in plugins:
            name = p['filename'].replace('.py', '')
            desc = p.get('description', '无描述')
            funcs = p.get('functions', [])
            lines.append(f"  - {name}: {desc}")
            if funcs:
                lines.append(f"    函数: {', '.join(funcs)}")
        return "\n".join(lines)
    
    def get_plugin_detail(self, plugin_name: str) -> str:
        """获取插件详情（供AI使用）"""
        filepath = self.plugins_dir / f"{plugin_name}.py"
        if not filepath.exists():
            return f"插件 '{plugin_name}' 不存在"
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
            return f"插件: {plugin_name}.py\n\n{content}"
        except Exception as e:
            return f"读取错误: {e}"

# ==================== SystemUse 处理器 ====================

class SystemUseProcessor:
    """处理AI的 system_use 命令"""
    
    def __init__(self, config: ConfigManager, session, skill_manager: SkillManager, plugin_manager: PluginManager):
        self.config = config
        self.session = session
        self.skill_manager = skill_manager
        self.plugin_manager = plugin_manager
    
    def execute(self, code: str) -> CommandResult:
        """执行 system_use 命令"""
        lines = code.strip().split('\n')
        results = []
        
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            parts = line.split()
            if not parts:
                continue
            
            cmd = parts[0]
            
            if cmd == 'skills':
                if len(parts) >= 2 and parts[1] == 'list':
                    results.append(self.skill_manager.list_names())
                elif len(parts) >= 3 and parts[1] == 'get':
                    results.append(self.skill_manager.get_skill_detail(parts[2]))
                elif len(parts) >= 3 and parts[1] == 'cd':
                    # cd到技能目录的相对路径
                    target = parts[2]
                    results.append(self._cd_to_relative(self.config.SKILLS_DIR, target))
                else:
                    results.append("用法: skills list | skills get <名称> | skills cd <相对路径>")
            
            elif cmd == 'plugins':
                if len(parts) >= 2 and parts[1] == 'list':
                    results.append(self.plugin_manager.list_names())
                elif len(parts) >= 3 and parts[1] == 'get':
                    results.append(self.plugin_manager.get_plugin_detail(parts[2]))
                elif len(parts) >= 3 and parts[1] == 'cd':
                    target = parts[2]
                    results.append(self._cd_to_relative(self.config.PLUGINS_DIR, target))
                else:
                    results.append("用法: plugins list | plugins get <包名> | plugins cd <相对路径>")
            
            else:
                results.append(f"未知命令: {cmd}")
        
        return CommandResult(
            stdout="\n".join(results),
            success=True,
            command_type='system_use',
            original_code=code
        )
    
    def _cd_to_relative(self, base_dir: Path, target: str) -> str:
        """cd到相对于base_dir的路径"""
        if target == '..':
            # 返回上级目录
            self.session.current_dir = str(base_dir.parent)
            return f"目录已切换到: {base_dir.parent}"
        elif target == '.':
            self.session.current_dir = str(base_dir)
            return f"目录已切换到: {base_dir}"
        else:
            # 相对路径
            new_path = (base_dir / target).resolve()
            if new_path.exists() and new_path.is_dir():
                self.session.current_dir = str(new_path)
                return f"目录已切换到: {new_path}"
            else:
                # 尝试作为文件路径
                parent = new_path.parent
                if parent.exists() and parent.is_dir():
                    self.session.current_dir = str(parent)
                    return f"目录已切换到: {parent}"
                else:
                    return f"路径不存在: {target}"

# ==================== API客户端 ====================

class APIClientFactory:
    @staticmethod
    def create(config: ConfigManager) -> Tuple[Any, str]:
        if config.api_type == 'ollama':
            return OllamaClient.create(config)
        else:
            return OpenAIClient.create(config)

class OllamaClient:
    def __init__(self, model: str, host: str = "http://localhost:11434"):
        self.model = model
        self.host = host
        import ollama
        self.ollama = ollama
        if host:
            os.environ['OLLAMA_HOST'] = host
    
    @classmethod
    def create(cls, config: ConfigManager):
        try:
            import ollama
        except ImportError:
            return None, None
        model = config.get('ollama_model', 'qwen2.5:7b')
        host = config.get('ollama_host', 'http://localhost:11434')
        try:
            client = cls(model, host)
            return client, model
        except Exception as e:
            print(f"❌ 连接失败: {e}")
            return None, None
    
    def chat_stream(self, messages: List[Dict]):
        try:
            stream = self.ollama.chat(model=self.model, messages=messages, stream=True)
            for chunk in stream:
                yield chunk
        except Exception as e:
            yield {'message': {'content': f"❌ Ollama错误: {str(e)}"}}

class OpenAIClient:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model
    
    @classmethod
    def create(cls, config: ConfigManager):
        api_key = config.get('api_key')
        if not api_key:
            return None, None
        try:
            from openai import OpenAI
        except ImportError:
            return None, None
        try:
            client = OpenAI(
                base_url=config.get('base_url', 'https://api.openai.com/v1'),
                api_key=api_key,
            )
            return cls(client, config.get('model', 'gpt-4o-mini')), config.get('model')
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            return None, None
    
    def chat_stream(self, messages: List[Dict]):
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=0.7
        )
        for chunk in stream:
            yield chunk

# ==================== 终端会话 ====================

class TerminalSession:
    def __init__(self, timeout: int = 30, no_timeout: bool = True):
        self.current_dir = os.getcwd()
        self.env = os.environ.copy()
        self.history: List[Dict] = []
        self.last_exit_code = 0
        self.timeout = timeout
        self.no_timeout = no_timeout
        self._running_process = None
        self._stop_event = Event()
    
    def stop_current_command(self):
        self._stop_event.set()
        if self._running_process:
            try:
                self._running_process.send_signal(signal.SIGINT)
                time.sleep(0.5)
                if self._running_process.poll() is None:
                    self._running_process.kill()
            except Exception:
                pass
    
    def execute_command(self, command: str) -> CommandResult:
        try:
            processed_cmd, new_dir = self._process_cd_compound(command.strip())
            if new_dir:
                self.current_dir = new_dir
                self.env['PWD'] = new_dir
            processed_cmd = self._process_export(processed_cmd)
            
            result = subprocess.run(
                processed_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=None if self.no_timeout else self.timeout,
                cwd=self.current_dir,
                env=self.env,
                preexec_fn=os.setsid if platform.system() != 'Windows' else None
            )
            
            return CommandResult(
                stdout=result.stdout,
                stderr=result.stderr,
                returncode=result.returncode,
                cwd=self.current_dir,
                success=result.returncode == 0,
                killed=False
            )
        except subprocess.TimeoutExpired:
            return CommandResult(stderr=f'⏰ 超时（{self.timeout}秒）', returncode=-1, success=False)
        except Exception as e:
            return CommandResult(stderr=f'💥 错误: {str(e)}', returncode=-1, success=False)
    
    def _process_cd_compound(self, command: str) -> Tuple[str, Optional[str]]:
        new_dir = None
        processed_cmd = command
        separators = ['&&', '||', ';', '|', '&']
        first_sep_index = len(command)
        
        for sep in separators:
            idx = command.find(sep)
            if idx != -1 and idx < first_sep_index:
                before = command[:idx]
                if before.count("'") % 2 == 0 and before.count('"') % 2 == 0:
                    first_sep_index = idx
        
        if first_sep_index > 0 and first_sep_index < len(command):
            first_part = command[:first_sep_index].strip()
            remaining = command[first_sep_index:]
        else:
            first_part = command.strip()
            remaining = ''
        
        if first_part.startswith('cd ') or first_part == 'cd':
            if first_part == 'cd':
                new_dir = os.path.expanduser('~')
            else:
                cd_target = first_part[3:].strip().strip('"').strip("'")
                if cd_target.startswith('~'):
                    cd_target = os.path.expanduser(cd_target)
                if not os.path.isabs(cd_target):
                    cd_target = os.path.join(self.current_dir, cd_target)
                cd_target = os.path.normpath(cd_target)
                if os.path.isdir(cd_target):
                    new_dir = cd_target
                else:
                    processed_cmd = f'echo "目录不存在: {cd_target}"' + (f' {remaining}' if remaining else '')
                    return processed_cmd, None
            
            if new_dir:
                if remaining:
                    processed_cmd = f'cd "{new_dir}" {remaining}' if platform.system() != 'Windows' else f'cd /d "{new_dir}" {remaining}'
                else:
                    processed_cmd = f'echo "目录已切换到: {new_dir}"'
        
        return processed_cmd, new_dir
    
    def _process_export(self, command: str) -> str:
        if command.startswith('export ') and '=' in command:
            parts = command[7:].strip().split('=', 1)
            if len(parts) == 2:
                self.env[parts[0].strip()] = parts[1].strip().strip('"').strip("'")
                return f'echo "环境变量已设置: {parts[0].strip()}={self.env[parts[0].strip()]}"'
        return command
    
    def reset(self):
        self.stop_current_command()
        self.current_dir = os.getcwd()
        self.env = os.environ.copy()
        self.history.clear()
        self.last_exit_code = 0

# ==================== 代码执行器 ====================

class CodeExecutor:
    def __init__(self, session: TerminalSession, tavily_client=None,
                 system_use_processor: SystemUseProcessor = None):
        self.session = session
        self.tavily_client = tavily_client
        self.system_use_processor = system_use_processor
        self._stop_event = Event()
    
    def stop(self):
        self._stop_event.set()
        self.session.stop_current_command()
    
    def execute(self, code: str, cmd_type: str) -> CommandResult:
        self._stop_event.clear()
        
        if cmd_type == 'terminal':
            return self._exec_terminal(code)
        elif cmd_type == 'run_python':
            return self._exec_python(code)
        elif cmd_type == 'search':
            return self._exec_search(code)
        elif cmd_type == 'system_use':
            return self._exec_system_use(code)
        else:
            return CommandResult(stderr=f"不支持的类型: {cmd_type}", success=False)
    
    def _exec_terminal(self, code: str) -> CommandResult:
        result = self.session.execute_command(code.strip())
        result.command_type = 'terminal'
        result.original_code = code
        return result
    
    def _exec_python(self, code: str) -> CommandResult:
        if not code.strip():
            return CommandResult(stderr="❌ 代码为空", success=False, command_type='run_python')
        
        tmpfile = None
        try:
            import tempfile
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
                f.write("# -*- coding: utf-8 -*-\n")
                f.write(code)
                tmpfile = f.name
            
            result = subprocess.run(
                [sys.executable, tmpfile],
                capture_output=True,
                text=True,
                timeout=None,
                cwd=self.session.current_dir,
                env=self.session.env
            )
            
            parts = []
            if result.stdout:
                parts.append(result.stdout.rstrip())
            if result.stderr:
                if parts:
                    parts.append("─" * 40)
                parts.append(f"标准错误输出:\n{result.stderr.rstrip()}")
            
            return CommandResult(
                stdout="\n".join(parts) if parts else "执行成功（无输出）",
                success=result.returncode == 0,
                cwd=self.session.current_dir,
                command_type='run_python',
                original_code=code
            )
        except Exception as e:
            return CommandResult(stderr=f"💥 执行错误: {str(e)}", success=False, command_type='run_python')
        finally:
            if tmpfile and os.path.exists(tmpfile):
                try:
                    os.unlink(tmpfile)
                except:
                    pass
    
    def _exec_search(self, query: str) -> CommandResult:
        if not self.tavily_client:
            return CommandResult(stderr="❌ 搜索功能未启用", success=False, command_type='search')
        try:
            response = self.tavily_client.search(query=query, search_depth="advanced", max_results=5, include_answer=True)
            parts = []
            if response.get('answer'):
                parts.append(f"📝 AI摘要:\n{response['answer']}\n")
            results = response.get('results', [])
            if results:
                parts.append(f"🔍 搜索结果 ({len(results)}条):\n")
                for i, r in enumerate(results, 1):
                    title = r.get('title', '无标题')
                    content = r.get('content', '')
                    parts.append(f"{i}. {title}")
                    if content:
                        parts.append(f"   {content[:200]}...")
                    parts.append("")
            return CommandResult(
                stdout="\n".join(parts) if parts else "未找到相关结果",
                success=True, command_type='search', original_code=query
            )
        except Exception as e:
            return CommandResult(stderr=f"❌ 搜索错误: {str(e)}", success=False, command_type='search')
    
    def _exec_system_use(self, code: str) -> CommandResult:
        """执行 system_use 命令"""
        if self.system_use_processor:
            return self.system_use_processor.execute(code)
        return CommandResult(stderr="system_use处理器未初始化", success=False, command_type='system_use')

# ==================== 内容解析器 ====================

class ContentParser:
    @staticmethod
    def extract_code_blocks(content: str) -> List[Tuple[str, str, str]]:
        if not content:
            return []
        code_blocks = []
        # 支持的代码块类型
        cmd_types = ['terminal', 'run_python', 'search', 'system_use']
        for cmd_type in cmd_types:
            pattern = rf'{BACKTICK}{cmd_type}\n(.*?){BACKTICK}'
            for match in re.finditer(pattern, content, re.DOTALL):
                code = match.group(1).strip()
                if code:
                    code_blocks.append((cmd_type, code, match.group(0)))
        return code_blocks
    
    @staticmethod
    def clean_content(content: str) -> str:
        if not content:
            return ""
        for cmd_type in ['terminal', 'run_python', 'search', 'system_use']:
            content = re.sub(rf'{BACKTICK}{cmd_type}\n.*?{BACKTICK}', '', content, flags=re.DOTALL)
        content = re.sub(r'\n\s*\n\s*\n', '\n\n', content)
        return content.strip()

# ==================== 系统提示词 ====================

class SystemPromptGenerator:
    """系统提示词生成器"""
    
    def __init__(self, skill_manager: SkillManager = None, plugin_manager: PluginManager = None):
        self.skill_manager = skill_manager
        self.plugin_manager = plugin_manager
    
    def generate(self, session: TerminalSession, config: ConfigManager) -> str:
        system_info = f"{platform.system()} {platform.release()}"
        
        prompt = config.get('system_prompt') or self._get_default_prompt()
        
        # 添加可用技能和插件信息
        skills_info = ""
        plugins_info = ""
        
        if self.skill_manager:
            skills = self.skill_manager.list_all()
            if skills:
                skill_names = [s['id'] for s in skills]
                skills_info = f"\n\n## 可用技能\n技能列表: {', '.join(skill_names)}\n使用 {BACKTICK}system_use\nskills list\nskills get 技能名\nskills cd 相对路径\n{BACKTICK} 来管理技能"
        
        if self.plugin_manager:
            plugins = self.plugin_manager.list_all()
            if plugins:
                plugin_names = [p['filename'].replace('.py', '') for p in plugins]
                plugins_info = f"\n\n## 可用插件\n插件列表: {', '.join(plugin_names)}\n使用 {BACKTICK}system_use\nplugins list\nplugins get 包名\nplugins cd 相对路径\n{BACKTICK} 来管理插件"
        
        prompt += skills_info + plugins_info
        
        replacements = {
            '{system_info}': system_info,
            '{cwd}': session.current_dir,
            '{python_version}': sys.version.split()[0]
        }
        
        for key, value in replacements.items():
            prompt = prompt.replace(key, value)
        
        return prompt
    
    def _get_default_prompt(self) -> str:
        return f"""你是一个强大的AI终端助手，运行在{{system_info}}环境中。

## 命令执行格式

### 1. 终端命令
{BACKTICK}terminal
ls -la
{BACKTICK}

### 2. Python代码
{BACKTICK}run_python
import os
print(os.getcwd())
{BACKTICK}

### 3. 网络搜索
{BACKTICK}search
搜索内容
{BACKTICK}

### 4. 系统命令（管理技能和插件）
{BACKTICK}system_use
skills list
plugins list
{BACKTICK}

## 当前状态
- 📁 工作目录: {{cwd}}
- 🖥️  系统: {{system_info}}
- 🐍 Python: {{python_version}}

请用中文回答，保持简洁、准确、有用。"""

# ==================== 消息处理器 ====================

class MessageProcessor:
    def __init__(self, config: ConfigManager, session: TerminalSession,
                 executor: CodeExecutor, conv_manager: ConversationManager = None):
        self.config = config
        self.session = session
        self.executor = executor
        self.parser = ContentParser()
        self.prompt_generator = None  # 由外部设置
        self.conv_manager = conv_manager
        self._stop_flag = False
    
    def stop(self):
        self._stop_flag = True
        self.executor.stop()
    
    def process_stream(self, messages: List[Dict], client, 
                       socketio=None, request_id=None):
        max_iterations = self.config.get('max_iterations', 5)
        show_thinking = self.config.show_thinking
        self._stop_flag = False
        all_messages = messages[:]
        
        try:
            for iteration in range(max_iterations):
                if self._stop_flag:
                    self._emit(socketio, request_id, {'type': 'stopped', 'data': '⏹️ 已停止'})
                    break
                
                if self.prompt_generator:
                    messages[0] = {
                        "role": "system",
                        "content": self.prompt_generator.generate(self.session, self.config)
                    }
                
                if iteration > 0:
                    self._emit(socketio, request_id, {
                        'type': 'iteration',
                        'data': f'🔄 继续分析... ({iteration + 1}/{max_iterations})'
                    })
                
                assistant_reply = ""
                reasoning_content = ""
                thinking_stream_active = False
                
                self._emit(socketio, request_id, {'type': 'assistant_start'})
                
                stream = client.chat_stream(messages)
                
                for chunk in stream:
                    if self._stop_flag:
                        break
                    
                    chunk_text = ""
                    chunk_reasoning = ""
                    
                    if isinstance(chunk, dict):
                        chunk_reasoning = chunk.get('reasoning_content', '')
                        if 'message' in chunk and 'content' in chunk['message']:
                            chunk_text = chunk['message']['content']
                    elif hasattr(chunk, 'choices') and chunk.choices:
                        delta = chunk.choices[0].delta
                        chunk_reasoning = getattr(delta, 'reasoning_content', '') or ''
                        chunk_text = getattr(delta, 'content', '') or ''
                    
                    # 流式输出思考
                    if chunk_reasoning and show_thinking:
                        reasoning_content += chunk_reasoning
                        if not thinking_stream_active:
                            self._emit(socketio, request_id, {'type': 'thinking_stream_start'})
                            thinking_stream_active = True
                        self._emit(socketio, request_id, {'type': 'thinking_stream', 'data': chunk_reasoning})
                    
                    # 流式输出正文
                    if chunk_text:
                        if thinking_stream_active:
                            self._emit(socketio, request_id, {'type': 'thinking_stream_end'})
                            thinking_stream_active = False
                        assistant_reply += chunk_text
                        self._emit(socketio, request_id, {'type': 'content', 'data': chunk_text})
                
                if self._stop_flag:
                    break
                
                if thinking_stream_active:
                    self._emit(socketio, request_id, {'type': 'thinking_stream_end'})
                
                self._emit(socketio, request_id, {'type': 'assistant_end'})
                
                all_messages.append({"role": "assistant", "content": assistant_reply})
                
                # 执行代码块
                code_blocks = self.parser.extract_code_blocks(assistant_reply)
                
                if code_blocks:
                    results = []
                    for cmd_type, code, _ in code_blocks:
                        if self._stop_flag:
                            break
                        
                        self._emit(socketio, request_id, {
                            'type': 'code_block',
                            'data': {'type': cmd_type, 'code': code}
                        })
                        
                        result = self.executor.execute(code, cmd_type)
                        results.append(result)
                        
                        self._emit(socketio, request_id, {
                            'type': 'execution_result',
                            'data': {
                                'type': cmd_type,
                                'success': result.success,
                                'output': result.get_output(),
                                'killed': result.killed
                            }
                        })
                        
                        if result.killed:
                            self._stop_flag = True
                            break
                    
                    if self._stop_flag:
                        break
                    
                    formatted_results = self._format_results(results)
                    messages.append({"role": "assistant", "content": assistant_reply})
                    messages.append({"role": "user", "content": formatted_results})
                    
                    if iteration >= max_iterations - 1:
                        self._emit(socketio, request_id, {
                            'type': 'warning',
                            'data': '⚠️ 已达到最大迭代次数'
                        })
                    continue
                
                messages.append({"role": "assistant", "content": assistant_reply})
                break
            
            # 自动保存对话
            if self.conv_manager and self.config.auto_save_conversation:
                filename = self.conv_manager.save(all_messages)
                if filename:
                    self._emit(socketio, request_id, {
                        'type': 'conversation_saved',
                        'data': {'filename': filename}
                    })
            
            self._emit(socketio, request_id, {'type': 'processing_complete', 'data': '完成'})
                
        except Exception as e:
            self._emit(socketio, request_id, {'type': 'error', 'data': str(e)})
            self._emit(socketio, request_id, {'type': 'processing_complete', 'data': '完成（有错误）'})
    
    def _format_results(self, results: List[CommandResult]) -> str:
        if not results:
            return ""
        parts = ["=" * 50, "📊 执行结果汇总", "=" * 50]
        for i, result in enumerate(results, 1):
            parts.append(f"\n--- 结果 {i} ({result.command_type.upper()}) ---")
            if result.original_code:
                code_preview = result.original_code[:100]
                if len(result.original_code) > 100:
                    code_preview += "..."
                parts.append(f"代码: {code_preview}")
            if result.killed:
                parts.append("状态: ⏹️ 被用户停止")
            else:
                parts.append(f"状态: {'✅ 成功' if result.success else '❌ 失败'}")
            if result.stdout:
                output = result.stdout[:1000]
                if len(result.stdout) > 1000:
                    output += f"\n... (截断，共{len(result.stdout)}字符)"
                parts.append(f"输出:\n{output}")
            if result.stderr:
                error = result.stderr[:500]
                if len(result.stderr) > 500:
                    error += f"\n... (截断，共{len(result.stderr)}字符)"
                parts.append(f"错误:\n{error}")
        parts.append(f"\n📁 当前目录: {os.getcwd()}")
        parts.append("=" * 50)
        return "\n".join(parts)
    
    def _emit(self, socketio, request_id, data):
        if socketio and request_id:
            socketio.emit('ai_response', {'request_id': request_id, **data})

# ==================== Web应用 ====================

class WebApp:
    def __init__(self, config: ConfigManager, session: TerminalSession):
        self.config = config
        self.session = session
        self.app = Flask(__name__)
        self.app.config['SECRET_KEY'] = os.urandom(24).hex()
        CORS(self.app)
        self.socketio = SocketIO(self.app, cors_allowed_origins="*", async_mode='threading')
        self.processor = None
        self.current_executor = None
        self.is_processing = False
        self.current_messages = []
        self.conv_manager = ConversationManager(config)
        self.skill_manager = SkillManager(config)
        self.plugin_manager = PluginManager(config)
        self.system_use_processor = SystemUseProcessor(config, session, self.skill_manager, self.plugin_manager)
        self.prompt_generator = SystemPromptGenerator(self.skill_manager, self.plugin_manager)
        self._setup_routes()
        self._setup_socketio()
    
    def _setup_routes(self):
        
        @self.app.route('/')
        def index():
            return render_template_string(HTML_TEMPLATE)
        
        @self.app.route('/api/status')
        def status():
            return jsonify({
                'cwd': self.session.current_dir,
                'model': self.config.get('model') or self.config.get('ollama_model', ''),
                'is_processing': self.is_processing,
                'auto_save': self.config.auto_save_conversation,
                'skills_count': len(self.skill_manager.list_all()),
                'plugins_count': len(self.plugin_manager.list_all()),
            })
        
        @self.app.route('/api/config', methods=['GET', 'POST'])
        def config_api():
            if request.method == 'POST':
                data = request.json
                for key, value in data.items():
                    self.config.set(key, value)
                self.config.save()
                return jsonify({'success': True})
            return jsonify(self.config.config)
        
        @self.app.route('/api/stop', methods=['POST'])
        def stop_execution():
            if self.processor:
                self.processor.stop()
            if self.current_executor:
                self.current_executor.stop()
            self.session.stop_current_command()
            return jsonify({'success': True})
        
        # 对话管理
        @self.app.route('/api/conversations')
        def list_conversations():
            return jsonify(self.conv_manager.list_all())
        
        @self.app.route('/api/conversations/save', methods=['POST'])
        def save_conversation():
            data = request.json
            messages = data.get('messages', self.current_messages)
            title = data.get('title', '')
            if not messages:
                return jsonify({'success': False, 'error': '没有消息'})
            filename = self.conv_manager.save(messages, title)
            return jsonify({'success': bool(filename), 'filename': filename})
        
        @self.app.route('/api/conversations/<filename>')
        def load_conversation(filename):
            data = self.conv_manager.load(filename)
            if data:
                return jsonify({'success': True, 'conversation': data})
            return jsonify({'success': False, 'error': '对话不存在'})
        
        @self.app.route('/api/conversations/<filename>', methods=['DELETE'])
        def delete_conversation(filename):
            return jsonify({'success': self.conv_manager.delete(filename)})
        
        # 技能管理
        @self.app.route('/api/skills')
        def list_skills():
            return jsonify(self.skill_manager.list_all())
        
        @self.app.route('/api/skills/<skill_id>')
        def get_skill(skill_id):
            skill = self.skill_manager.get(skill_id)
            if skill:
                return jsonify({'success': True, 'skill': skill})
            return jsonify({'success': False, 'error': '技能不存在'})
        
        # 插件管理
        @self.app.route('/api/plugins')
        def list_plugins():
            return jsonify(self.plugin_manager.list_all())
    
    def _setup_socketio(self):
        
        @self.socketio.on('connect')
        def handle_connect():
            emit('connected', {'status': 'ok'})
        
        @self.socketio.on('send_message')
        def handle_message(data):
            user_message = data.get('message', '')
            request_id = data.get('request_id', str(uuid.uuid4()))
            messages = data.get('messages', [])
            
            if not user_message:
                return
            
            self.is_processing = True
            messages.append({"role": "user", "content": user_message})
            self.current_messages = messages[:]
            
            emit('ai_response', {'request_id': request_id, 'type': 'user_message', 'data': user_message})
            
            client, model = APIClientFactory.create(self.config)
            if not client:
                emit('ai_response', {'request_id': request_id, 'type': 'error', 'data': 'API客户端未配置'})
                emit('ai_response', {'request_id': request_id, 'type': 'processing_complete'})
                self.is_processing = False
                return
            
            tavily_client = None
            if self.config.get('tavily_api_key'):
                try:
                    from tavily import TavilyClient
                    tavily_client = TavilyClient(api_key=self.config.get('tavily_api_key'))
                except:
                    pass
            
            self.current_executor = CodeExecutor(
                self.session, tavily_client, self.system_use_processor
            )
            self.processor = MessageProcessor(
                self.config, self.session, self.current_executor, self.conv_manager
            )
            self.processor.prompt_generator = self.prompt_generator
            
            def process():
                try:
                    self.processor.process_stream(messages, client, self.socketio, request_id)
                finally:
                    self.is_processing = False
            
            thread = threading.Thread(target=process, daemon=True)
            thread.start()
        
        @self.socketio.on('stop_execution')
        def handle_stop():
            if self.processor:
                self.processor.stop()
            if self.current_executor:
                self.current_executor.stop()
            self.session.stop_current_command()
            self.is_processing = False
            emit('execution_stopped', {'message': '执行已停止'})
        
        @self.socketio.on('load_conversation')
        def handle_load_conversation(data):
            filename = data.get('filename', '')
            conv_data = self.conv_manager.load(filename)
            if conv_data:
                emit('conversation_loaded', {
                    'messages': conv_data.get('messages', []),
                    'metadata': conv_data.get('metadata', {})
                })
    
    def run(self, host: str = None, port: int = None, debug: bool = False):
        host = host or self.config.get('web_host', '0.0.0.0')
        port = port or self.config.get('web_port', 5000)
        
        print(f"""
╔══════════════════════════════════════════╗
║     AI终端助手 Web版 v7.8               ║
║     http://{host}:{port}                  ║
║                                          ║
║     ✨ system_use 命令格式                ║
║     💭 思考流式不重复                     ║
╚══════════════════════════════════════════╝
        """)
        
        self.socketio.run(self.app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)

# ==================== HTML模板 ====================

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI终端助手 v7.8</title>
    <script src="https://cdn.socket.io/4.5.4/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/lib/index.min.js"></script>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/highlight.js@11.9.0/styles/github-dark.min.css">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #1a1b26; color: #a9b1d6; height: 100vh;
            display: flex; flex-direction: column;
        }
        .header {
            background: #24283b; padding: 10px 20px;
            display: flex; align-items: center; justify-content: space-between;
            border-bottom: 1px solid #414868;
        }
        .header h1 { font-size: 1.2em; color: #7aa2f7; }
        .status-bar { display: flex; gap: 15px; font-size: 0.85em; align-items: center; }
        .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; }
        .dot.green { background: #9ece6a; }
        
        .toolbar {
            background: #1e2030; padding: 8px 20px;
            border-bottom: 1px solid #414868;
            display: flex; justify-content: space-between; align-items: center;
            flex-wrap: wrap; gap: 8px;
        }
        .toolbar-group { display: flex; align-items: center; gap: 8px; }
        .toolbar-btn {
            padding: 5px 12px; border: 1px solid #414868; border-radius: 4px;
            background: #24283b; color: #a9b1d6; cursor: pointer;
            font-size: 0.85em; transition: all 0.2s;
        }
        .toolbar-btn:hover { background: #7aa2f7; color: #1a1b26; }
        .toolbar-btn.danger:hover { background: #f7768e; }
        .toolbar-select {
            background: #1a1b26; color: #a9b1d6;
            border: 1px solid #414868; padding: 5px 10px;
            border-radius: 4px; font-size: 0.85em; max-width: 250px;
        }
        
        .chat-container {
            flex: 1; overflow-y: auto; padding: 20px;
            display: flex; flex-direction: column; gap: 10px;
        }
        .message {
            max-width: 85%; padding: 12px 16px; border-radius: 12px;
            line-height: 1.6; word-wrap: break-word;
        }
        .message.user { background: #2ac3de; color: #1a1b26; align-self: flex-end; }
        .message.assistant { background: #24283b; border: 1px solid #414868; align-self: flex-start; }
        .message.thinking-stream {
            background: #1e2030; border: 1px solid #e0af68;
            border-left: 3px solid #e0af68; color: #e0af68;
            align-self: flex-start; max-width: 90%;
        }
        .message.error { background: #2d1b1b; border: 1px solid #f7768e; color: #f7768e; align-self: flex-start; }
        .message.iteration { background: transparent; color: #565f89; font-size: 0.85em; text-align: center; align-self: center; }
        
        .code-block {
            background: #1a1b26; border: 1px solid #414868;
            border-radius: 8px; margin: 10px 0; overflow: hidden; width: 100%;
        }
        .code-header { background: #24283b; padding: 8px 15px; display: flex; justify-content: space-between; align-items: center; }
        .code-content { padding: 15px; overflow-x: auto; }
        .code-content pre { margin: 0; }
        
        .execution-result {
            background: #1e2030; border: 1px solid #414868;
            border-radius: 8px; margin: 10px 0; padding: 15px; width: 100%;
        }
        .execution-result.success { border-color: #9ece6a; }
        .execution-result.error { border-color: #f7768e; }
        .execution-result.killed { border-color: #e0af68; }
        
        .input-container {
            padding: 15px 20px; background: #24283b;
            border-top: 1px solid #414868; display: flex; gap: 10px;
        }
        .input-container textarea {
            flex: 1; padding: 10px 15px; border: 1px solid #414868;
            border-radius: 8px; background: #1a1b26; color: #a9b1d6; font-size: 0.95em;
            resize: none; min-height: 40px; max-height: 120px; font-family: inherit;
        }
        .input-container textarea:focus { outline: none; border-color: #7aa2f7; }
        .btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-weight: bold; }
        .btn-send { background: #7aa2f7; color: #1a1b26; }
        .btn-stop { background: #f7768e; color: #1a1b26; display: none; }
        .btn-stop.visible { display: inline-block; }
        .thinking-content { white-space: pre-wrap; word-break: break-word; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🤖 AI终端助手 v7.8</h1>
        <div class="status-bar">
            <span class="dot green" id="statusDot"></span>
            <span id="statusText">已连接</span>
            <span id="autoSaveStatus">💾 自动保存</span>
            <span id="processingStatus" style="display:none; color:#e0af68;">⏳</span>
        </div>
    </div>
    
    <div class="toolbar">
        <div class="toolbar-group">
            <button onclick="newConversation()" class="toolbar-btn">🆕 新对话</button>
            <button onclick="saveConversation()" class="toolbar-btn">💾 保存</button>
        </div>
        <div class="toolbar-group">
            <select id="conversationSelect" onchange="loadConversation(this.value)" class="toolbar-select">
                <option value="">-- 历史对话 --</option>
            </select>
            <button onclick="deleteConversation()" class="toolbar-btn danger">🗑</button>
        </div>
        <div class="toolbar-group">
            <button onclick="showSkills()" class="toolbar-btn">🎯 技能</button>
            <button onclick="showPlugins()" class="toolbar-btn">🔌 插件</button>
        </div>
    </div>
    
    <div class="chat-container" id="chatContainer">
        <div class="message assistant">
            👋 你好！我是AI终端助手 v7.8<br><br>
            ✨ 新功能：<br>
            • <b>system_use</b> 命令格式<br>
            • 思考过程流式不重复<br>
            • skills/plugins 管理<br><br>
            ⌨️ <b>Enter</b>换行 | <b>Ctrl+Enter</b>发送
        </div>
    </div>
    
    <div class="input-container">
        <textarea id="userInput" placeholder="输入消息... (Ctrl+Enter发送)" rows="1" onkeydown="handleKeyDown(event)"></textarea>
        <button onclick="sendMessage()" id="sendBtn" class="btn btn-send">发送</button>
        <button onclick="stopExecution()" id="stopBtn" class="btn btn-stop">⏹ 停止</button>
    </div>
    
    <script>
        const socket = io();
        const chatContainer = document.getElementById('chatContainer');
        const userInput = document.getElementById('userInput');
        const sendBtn = document.getElementById('sendBtn');
        const stopBtn = document.getElementById('stopBtn');
        let messages = [], currentAssistantMsg = null, currentThinkingMsg = null, isProcessing = false;
        
        userInput.addEventListener('input', function() {
            this.style.height = 'auto';
            this.style.height = Math.min(this.scrollHeight, 120) + 'px';
        });
        
        function handleKeyDown(e) {
            if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                if (!isProcessing) sendMessage();
            }
        }
        
        marked.setOptions({highlight: (c,l) => l&&hljs.getLanguage(l)?hljs.highlight(c,{language:l}).value:c, breaks:true});
        
        socket.on('connect', () => {
            document.getElementById('statusDot').className = 'dot green';
            document.getElementById('statusText').textContent = '已连接';
            loadConversations();
        });
        
        socket.on('ai_response', (r) => {
            switch(r.type) {
                case 'user_message': addMessage('user', r.data); break;
                case 'assistant_start': currentAssistantMsg=null; currentThinkingMsg=null; isProcessing=true; updateButtons(); break;
                case 'thinking_stream_start': if(!currentThinkingMsg) currentThinkingMsg=addThinkingStream(); break;
                case 'thinking_stream':
                    if(currentThinkingMsg){const e=currentThinkingMsg.querySelector('.thinking-content');if(e){e.textContent+=r.data;scrollToBottom();}} break;
                case 'thinking_stream_end': currentThinkingMsg=null; break;
                case 'content':
                    if(!currentAssistantMsg) currentAssistantMsg=addMessage('assistant','',true);
                    const c=currentAssistantMsg.getAttribute('data-content')||'', n=c+r.data;
                    currentAssistantMsg.setAttribute('data-content',n);
                    currentAssistantMsg.innerHTML=marked.parse(n); scrollToBottom(); break;
                case 'assistant_end':
                    if(currentAssistantMsg&&currentAssistantMsg.getAttribute('data-content'))
                        messages.push({role:'assistant',content:currentAssistantMsg.getAttribute('data-content')});
                    currentAssistantMsg=null; break;
                case 'code_block': addCodeBlock(r.data.type,r.data.code); break;
                case 'execution_result': addExecutionResult(r.data); break;
                case 'conversation_saved': loadConversations(); break;
                case 'error': addMessage('error','❌ '+r.data); break;
                case 'iteration': addMessage('iteration',r.data); break;
                case 'processing_complete': isProcessing=false; updateButtons(); userInput.focus(); break;
            }
        });
        
        socket.on('execution_stopped',()=>{addMessage('error','⏹️ 已停止'); isProcessing=false; updateButtons();});
        
        function addMessage(type,content,streaming){
            const d=document.createElement('div');
            d.className='message '+type;
            if(streaming&&type==='assistant') d.setAttribute('data-content','');
            if(type==='assistant'&&content) d.innerHTML=marked.parse(content);
            else if(type!=='assistant'||!streaming) d.textContent=content;
            chatContainer.appendChild(d); scrollToBottom(); return d;
        }
        
        function addThinkingStream(){
            const d=document.createElement('div');
            d.className='message thinking-stream';
            d.innerHTML='<div class="thinking-label">💭 思考中...</div><div class="thinking-content"></div>';
            chatContainer.appendChild(d); scrollToBottom(); return d;
        }
        
        function addCodeBlock(type,code){
            const d=document.createElement('div');
            d.className='code-block';
            d.innerHTML=`<div class="code-header"><span>📋 ${type.toUpperCase()}</span><span style="color:#e0af68;">执行中...</span></div><div class="code-content"><pre><code>${escapeHtml(code)}</code></pre></div>`;
            chatContainer.appendChild(d); scrollToBottom();
        }
        
        function addExecutionResult(data){
            const d=document.createElement('div');
            d.className='execution-result '+(data.killed?'killed':(data.success?'success':'error'));
            d.innerHTML=`<strong>${data.type.toUpperCase()} | ${data.killed?'⏹️ 已停止':(data.success?'✅ 成功':'❌ 失败')}</strong><pre style="margin-top:10px;white-space:pre-wrap;">${escapeHtml(data.output)}</pre>`;
            chatContainer.appendChild(d); scrollToBottom();
        }
        
        function sendMessage(){
            if(isProcessing) return;
            const t=userInput.value.trim(); if(!t) return;
            messages.push({role:'user',content:t});
            socket.emit('send_message',{message:t,request_id:Date.now().toString(),messages:messages.slice()});
            userInput.value=''; userInput.style.height='auto'; isProcessing=true; updateButtons();
        }
        
        function stopExecution(){socket.emit('stop_execution');fetch('/api/stop',{method:'POST'}).catch(()=>{});}
        
        function updateButtons(){
            sendBtn.disabled=isProcessing;
            if(isProcessing){stopBtn.classList.add('visible');document.getElementById('processingStatus').style.display='inline';}
            else{stopBtn.classList.remove('visible');document.getElementById('processingStatus').style.display='none';}
        }
        
        function loadConversations(){
            fetch('/api/conversations').then(r=>r.json()).then(data=>{
                const s=document.getElementById('conversationSelect');
                s.innerHTML='<option value="">-- 历史对话 --</option>';
                data.forEach(c=>{const o=document.createElement('option');o.value=c.filename;o.textContent=`${(c.timestamp||'').substring(0,15)} - ${(c.title||'无标题').substring(0,20)} (${c.message_count}条)`;s.appendChild(o);});
            }).catch(()=>{});
        }
        
        function saveConversation(){
            if(!messages.length){alert('没有可保存的对话');return;}
            const t=prompt('标题（可选）:','');
            fetch('/api/conversations/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages,title:t})}).then(r=>r.json()).then(d=>{if(d.success){alert('已保存');loadConversations();}}).catch(()=>{});
        }
        
        function loadConversation(fn){
            if(!fn) return;
            if(messages.length>1&&!confirm('将替换当前对话，继续？')){document.getElementById('conversationSelect').value='';return;}
            fetch('/api/conversations/'+fn).then(r=>r.json()).then(d=>{
                if(d.success){chatContainer.innerHTML='';messages=d.conversation.messages||[];messages.forEach(m=>addMessage(m.role,m.content));scrollToBottom();}
            }).catch(()=>{});
        }
        
        function deleteConversation(){
            const fn=document.getElementById('conversationSelect').value; if(!fn) return;
            if(!confirm('确定删除？')) return;
            fetch('/api/conversations/'+fn,{method:'DELETE'}).then(()=>loadConversations()).catch(()=>{});
        }
        
        function newConversation(){
            if(messages.length>1&&!confirm('将清空当前对话，继续？')) return;
            chatContainer.innerHTML='<div class="message assistant">👋 新对话已开始！</div>';
            messages=[]; document.getElementById('conversationSelect').value='';
        }
        
        function showSkills(){
            fetch('/api/skills').then(r=>r.json()).then(s=>{
                let l='技能列表:\n\n';
                s.forEach(s=>{l+=`🎯 ${s.name} (${s.id})\n   ${s.description}\n   📂 ${s.category}\n\n`;});
                alert(l||'暂无技能');
            }).catch(()=>{});
        }
        
        function showPlugins(){
            fetch('/api/plugins').then(r=>r.json()).then(p=>{
                let l='插件列表:\n\n';
                p.forEach(p=>{l+=`🔌 ${p.filename}\n   ${p.description||'无描述'}`;if(p.functions)l+=`\n   函数: ${p.functions.join(', ')}`;l+='\n\n';});
                alert(l||'暂无插件');
            }).catch(()=>{});
        }
        
        function scrollToBottom(){chatContainer.scrollTop=chatContainer.scrollHeight;}
        function escapeHtml(t){const d=document.createElement('div');d.textContent=t;return d.innerHTML;}
        window.addEventListener('load',()=>{userInput.focus();loadConversations();});
    </script>
</body>
</html>
'''

def render_template_string(template):
    return template

# ==================== 终端UI ====================

class TerminalUI:
    """终端模式UI - 思考流式不重复输出"""
    
    def __init__(self, config: ConfigManager, session: TerminalSession):
        self.config = config
        self.session = session
        self.console = Console(theme=CUSTOM_THEME)
        self.processor = None
        self.executor = None
        self.conv_manager = ConversationManager(config)
        self.skill_manager = SkillManager(config)
        self.plugin_manager = PluginManager(config)
        self.system_use_processor = SystemUseProcessor(config, session, self.skill_manager, self.plugin_manager)
        self.prompt_generator = SystemPromptGenerator(self.skill_manager, self.plugin_manager)
    
    def run(self):
        self.console.clear()
        self._show_header()
        
        client, model = APIClientFactory.create(self.config)
        if not client:
            self.console.print("[error]❌ API客户端初始化失败[/error]")
            return
        
        tavily_client = None
        if self.config.get('tavily_api_key'):
            try:
                from tavily import TavilyClient
                tavily_client = TavilyClient(api_key=self.config.get('tavily_api_key'))
            except:
                pass
        
        self.executor = CodeExecutor(self.session, tavily_client, self.system_use_processor)
        self.processor = MessageProcessor(self.config, self.session, self.executor, self.conv_manager)
        self.processor.prompt_generator = self.prompt_generator
        
        messages = [{"role": "system", "content": self.prompt_generator.generate(self.session, self.config)}]
        
        self.console.print(f"[success]✅ {model} 已就绪[/success]")
        self.console.print(f"[info]📂 对话: {self.config.CONVERSATIONS_DIR}[/info]")
        self.console.print(f"[info]🎯 技能: {len(self.skill_manager.list_all())}个[/info]")
        self.console.print(f"[info]🔌 插件: {len(self.plugin_manager.list_all())}个[/info]")
        self.console.print("[dim]命令: menu | exit | save | stop | pwd | reset | clear | help[/dim]")
        
        import signal
        
        def signal_handler(sig, frame):
            self.console.print("\n[yellow]⚠️ 正在停止...[/yellow]")
            if self.executor: self.executor.stop()
            if self.processor: self.processor.stop()
        
        original_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal_handler)
        
        try:
            while True:
                try:
                    cwd_display = self._get_cwd_display()
                    user_input = Prompt.ask(f"\n📁 [path]{cwd_display}[/path]\n>>> ")
                    
                    if not user_input: continue
                    cmd = user_input.lower().strip()
                    
                    if cmd in ['exit', 'q']:
                        if self.config.auto_save_conversation and len(messages) > 1:
                            fn = self.conv_manager.save(messages)
                            if fn: self.console.print(f"[success]💾 已保存: {fn}[/success]")
                        self.console.print("[success]👋 再见！[/success]")
                        break
                    elif cmd == 'save':
                        fn = self.conv_manager.save(messages)
                        if fn: self.console.print(f"[success]💾 已保存: {fn}[/success]")
                        else: self.console.print("[warning]没有可保存的内容[/warning]")
                        continue
                    elif cmd == 'stop':
                        if self.executor: self.executor.stop()
                        if self.processor: self.processor.stop()
                        continue
                    elif cmd == 'menu': break
                    elif cmd == 'pwd':
                        self.console.print(f"[path]{self.session.current_dir}[/path]")
                        continue
                    elif cmd == 'reset':
                        self.session.reset()
                        messages = [{"role": "system", "content": self.prompt_generator.generate(self.session, self.config)}]
                        self.console.print("[success]✅ 已重置[/success]")
                        continue
                    elif cmd == 'clear':
                        self.console.clear(); continue
                    elif cmd == 'help':
                        self._show_help(); continue
                    
                    messages.append({"role": "user", "content": user_input})
                    self._process_message(messages, client, model)
                    
                    max_history = self.config.get('max_history', 100)
                    if len(messages) > max_history:
                        messages = [messages[0]] + messages[-(max_history - 1):]
                        
                except KeyboardInterrupt:
                    self.console.print("\n[yellow]⚠️ 操作被中断[/yellow]")
                    if self.executor: self.executor.stop()
                    if self.processor: self.processor.stop()
                    continue
                except Exception as e:
                    self.console.print(f"[error]错误: {e}[/error]")
        finally:
            signal.signal(signal.SIGINT, original_handler)
    
    def _get_cwd_display(self):
        cwd = self.session.current_dir
        home = os.path.expanduser("~")
        if cwd.startswith(home): cwd = "~" + cwd[len(home):]
        if len(cwd) > 50: cwd = "..." + cwd[-47:]
        return cwd
    
    def _process_message(self, messages, client, model):
        """处理消息 - 思考流式输出，不重复打印面板"""
        console = self.console
        max_iterations = self.config.get('max_iterations', 5)
        show_thinking = self.config.show_thinking
        
        for iteration in range(max_iterations):
            try:
                if self.processor._stop_flag:
                    console.print("[yellow]⏹️ 已停止[/yellow]")
                    self.processor._stop_flag = False
                    break
                
                messages[0] = {"role": "system", "content": self.prompt_generator.generate(self.session, self.config)}
                
                if iteration > 0:
                    console.print(f"\n[dim]🔄 继续分析... ({iteration + 1}/{max_iterations})[/dim]")
                
                console.print(f"\n[bold green]🤖 {model}:[/bold green]")
                
                assistant_reply = ""
                reasoning_content = ""
                thinking_live = None
                content_live = None
                thinking_was_streaming = False  # 标记是否有流式思考
                
                try:
                    stream = client.chat_stream(messages)
                    
                    for chunk in stream:
                        if self.processor._stop_flag: break
                        
                        chunk_text = ""
                        chunk_reasoning = ""
                        
                        if isinstance(chunk, dict):
                            chunk_reasoning = chunk.get('reasoning_content', '')
                            if 'message' in chunk and 'content' in chunk['message']:
                                chunk_text = chunk['message']['content']
                        elif hasattr(chunk, 'choices') and chunk.choices:
                            delta = chunk.choices[0].delta
                            chunk_reasoning = getattr(delta, 'reasoning_content', '') or ''
                            chunk_text = getattr(delta, 'content', '') or ''
                        
                        # 流式显示思考 - 使用Rich Text
                        if chunk_reasoning and show_thinking:
                            reasoning_content += chunk_reasoning
                            thinking_was_streaming = True
                            if thinking_live is None:
                                thinking_live = Live(console=console, auto_refresh=False, vertical_overflow="visible")
                                thinking_live.start()
                            thinking_text = Text()
                            thinking_text.append("💭 思考:\n", style="italic yellow")
                            thinking_text.append(reasoning_content, style="italic yellow")
                            thinking_live.update(thinking_text, refresh=True)
                        
                        # 正文开始 - 关闭思考Live
                        if chunk_text:
                            assistant_reply += chunk_text
                            if thinking_live:
                                thinking_live.stop()
                                thinking_live = None
                                # 【不打印总结面板】思考已经流式输出完毕
                            if content_live is None:
                                content_live = Live(console=console, auto_refresh=False, vertical_overflow="visible")
                                content_live.start()
                            content_live.update(Markdown(assistant_reply, code_theme="monokai"), refresh=True)
                    
                finally:
                    if thinking_live:
                        thinking_live.stop()
                    if content_live:
                        content_live.stop()
                
                if self.processor._stop_flag:
                    self.processor._stop_flag = False
                    break
                
                # 不再重复打印思考面板（已流式输出）
                
                # 执行代码块
                code_blocks = ContentParser.extract_code_blocks(assistant_reply)
                results = []
                
                for cmd_type, code, _ in code_blocks:
                    if self.processor._stop_flag: break
                    
                    language_map = {'terminal': 'bash', 'run_python': 'python', 'search': 'text', 'system_use': 'bash'}
                    console.print()
                    console.print(Panel(
                        Syntax(code, language_map.get(cmd_type, 'text'), theme="monokai", line_numbers=True, word_wrap=True),
                        title=f"📋 {cmd_type.upper()}", border_style="blue", box=box.ROUNDED
                    ))
                    console.print(f"[yellow]执行中... (Ctrl+C停止)[/yellow]")
                    
                    result = self.executor.execute(code, cmd_type)
                    results.append(result)
                    
                    status = "⏹️ 已停止" if result.killed else ("✅ 成功" if result.success else "❌ 失败")
                    console.print(Panel(
                        Text(result.get_output()),
                        title=f"{cmd_type.upper()} | {status}",
                        border_style="yellow" if result.killed else ("green" if result.success else "red"),
                        box=box.ROUNDED
                    ))
                    
                    if result.killed:
                        self.processor._stop_flag = True
                        break
                
                if self.processor._stop_flag:
                    self.processor._stop_flag = False
                    break
                
                if results:
                    formatted_results = self.processor._format_results(results)
                    messages.append({"role": "assistant", "content": assistant_reply})
                    messages.append({"role": "user", "content": formatted_results})
                    if iteration >= max_iterations - 1:
                        console.print("[yellow]⚠️ 已达到最大迭代次数[/yellow]")
                    continue
                
                messages.append({"role": "assistant", "content": assistant_reply})
                break
                
            except KeyboardInterrupt:
                console.print("\n[yellow]⚠️ 操作被中断[/yellow]")
                if self.executor: self.executor.stop()
                if self.processor: self.processor.stop()
                break
            except Exception as e:
                console.print(f"[error]错误: {e}[/error]")
                break
    
    def _show_header(self):
        self.console.rule("[title]🤖 AI终端助手 v7.8[/title]")
    
    def _show_help(self):
        help_text = f"""# 🤖 AI终端助手 v7.8

## 命令格式

### system_use - 系统管理
{BACKTICK}system_use
skills list
skills get 技能名
plugins list
plugins get 包名
{BACKTICK}

## 内置命令
- `save` - 保存对话
- `stop` - 停止当前命令
- `pwd` - 查看目录
- `reset` - 重置会话
"""
        self.console.print(Panel(Markdown(help_text, code_theme="monokai"), border_style="blue"))

# ==================== 主应用 ====================

class AITerminalApp:
    def __init__(self):
        self.config = ConfigManager()
        self.session = TerminalSession(timeout=self.config.get('timeout', 30), no_timeout=self.config.get('no_timeout', True))
        self.console = Console(theme=CUSTOM_THEME)
    
    def run(self):
        if '--web' in sys.argv or '-w' in sys.argv:
            self._run_web()
        elif '--help' in sys.argv or '-h' in sys.argv:
            self._show_usage()
        else:
            self._run_terminal_menu()
    
    def _run_web(self):
        port = self.config.get('web_port', 5000)
        for i, arg in enumerate(sys.argv):
            if arg in ['--port', '-p'] and i + 1 < len(sys.argv):
                port = int(sys.argv[i + 1]); break
        WebApp(self.config, self.session).run(port=port)
    
    def _run_terminal_menu(self):
        self.console.clear()
        self.console.rule("[title]🤖 AI终端助手 v7.8[/title]")
        self.console.print("[info]💭 流式思考 | system_use命令 | 对话保存[/info]")
        
        client, model = APIClientFactory.create(self.config)
        if not client:
            self.console.print("[warning]⚠️ 需要配置API[/warning]")
            self._setup_wizard()
            client, model = APIClientFactory.create(self.config)
            if not client:
                self.console.print("[error]❌ 配置失败[/error]")
                return
        
        while True:
            try:
                choice = self._show_menu(model)
                if choice == 'chat':
                    TerminalUI(self.config, self.session).run()
                elif choice == 'web':
                    self._run_web(); break
                elif choice == 'setup':
                    self._setup_wizard()
                elif choice == 'exit':
                    self.console.print("[success]👋 再见！[/success]"); break
            except KeyboardInterrupt:
                self.console.print("\n\n[success]👋 再见！[/success]"); break
    
    def _show_menu(self, model: str) -> str:
        cwd = self.session.current_dir
        home = os.path.expanduser("~")
        if cwd.startswith(home): cwd = "~" + cwd[len(home):]
        
        table = Table(box=box.ROUNDED, padding=(0, 2))
        table.add_column("选项", style="cyan bold", width=8)
        table.add_column("功能", style="bold", width=20)
        table.add_column("说明", style="dim")
        table.add_row("1", "💬 终端对话", "system_use + 流式思考")
        table.add_row("2", "🌐 Web界面", "浏览器模式")
        table.add_row("3", "⚙️  配置", "修改API设置")
        table.add_row("0", "🚪 退出", "结束程序")
        
        self.console.print(Panel(table, title="📋 主菜单", border_style="blue"))
        self.console.print(f"\n[dim]📁 {cwd}[/dim]")
        self.console.print(f"[dim]🤖 {model}[/dim]")
        
        choice = Prompt.ask("\n请选择", choices=["0", "1", "2", "3"], default="1")
        return {"1": "chat", "2": "web", "3": "setup", "0": "exit"}.get(choice, "chat")
    
    def _setup_wizard(self):
        self.console.rule("[title]⚙️ 配置向导[/title]")
        self.console.print("\nAPI类型:\n  1. OpenAI API\n  2. Ollama (本地)")
        
        api_choice = Prompt.ask("选择", choices=["1", "2"], default="1")
        
        if api_choice == "2":
            self.config.set('api_type', 'ollama')
            self.config.set('ollama_model', Prompt.ask("模型名称", default="qwen2.5:7b"))
            self.config.set('ollama_host', Prompt.ask("Ollama地址", default="http://localhost:11434"))
        else:
            self.config.set('api_type', 'openai')
            self.config.set('base_url', Prompt.ask("API地址", default="https://api.openai.com/v1"))
            self.config.set('api_key', Prompt.ask("API密钥", password=True))
            self.config.set('model', Prompt.ask("模型名称", default="gpt-4o-mini"))
        
        tavily_key = Prompt.ask("Tavily API Key (可选)", default="", password=True)
        if tavily_key: self.config.set('tavily_api_key', tavily_key)
        
        self.config.save()
        self.console.print("[success]✅ 配置已保存[/success]")
    
    def _show_usage(self):
        print("""
AI终端助手 v7.8

用法:
  python coder.py                终端模式
  python coder.py --web          Web模式
  python coder.py -w -p 8080     Web模式(指定端口)

新增:
  ✨ system_use 命令格式 (skills/plugins管理)
  💭 思考过程流式不重复输出
""")

if __name__ == "__main__":
    AITerminalApp().run()