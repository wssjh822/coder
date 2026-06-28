# 🤖 AI终端助手 (AI Terminal Assistant)

一个强大的AI终端助手，支持终端模式和Web浏览器模式，集成多种AI能力。

## ✨ 特性

- 💬 **终端对话模式** - 命令行交互
- 🌐 **Web界面** - 浏览器访问
- 💭 **流式思考** - 实时显示AI思考过程
- 🔧 **命令执行** - 执行终端命令和Python代码
- 🔍 **网络搜索** - 集成Tavily搜索
- 📂 **对话保存** - 自动保存和加载对话历史
- 🎯 **技能系统** - 可扩展的AI技能
- 🔌 **插件系统** - 动态加载功能插件
- ⏹️ **命令停止** - Ctrl+C或停止按钮中断执行
- 📁 **cd复合命令** - 支持 cd /path && ls 等组合

## 🚀 快速开始

### 安装依赖

```bash
pip install rich ollama openai flask flask-socketio flask-cors
```

运行

```bash
# 终端模式
python coder.py

# Web模式（默认端口5000）
python coder.py --web

# Web模式（指定端口）
python coder.py -w -p 8080
```

配置API

首次运行会进入配置向导，或手动编辑 .ai_terminal/config.json

📖 使用说明

命令格式

AI可以理解以下命令格式：

```
```terminal
ls -la
```

```run_python
import os
print(os.getcwd())
```

```search
搜索内容
```

```system_use
skills list
plugins list
```
```

快捷键

模式 快捷键 功能
终端 Ctrl+C 停止当前命令
Web Enter 换行
Web Ctrl+Enter 发送消息

📂 目录结构

```
.ai_terminal/
├── config.json          # 配置文件
├── conversations/       # 对话记录
├── skills/              # 技能定义
└── plugins/             # 插件脚本
```

🔌 插件系统

插件放在 .ai_terminal/plugins/ 目录下，自动加载。

```python
# 示例插件
"""插件描述"""

def my_function():
    return "Hello"

__all__ = ["my_function"]
```

🎯 技能系统

技能定义在 .ai_terminal/skills/ 目录下，JSON格式。

📝 许可证

MIT License

👤 作者

wssjh822

🤝 贡献

欢迎提交Issue和Pull Request！
