# DCL-IDE 项目脚手架

> DCL加工系统 — 集成开发环境
> 版本: v0.1 (脚手架) | 日期: 2026-07-11

## 快速开始

```bash
# 1. 创建虚拟环境
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 启动IDE
python shell/main.py
```

## 项目结构

```
dcl-ide/
├── shell/                    # 壳层 (PyInstaller打包入口)
├── server/                   # 服务层 (LSP + USB + AI)
├── web/                      # 前端 (HTML/JS + Monaco)
├── compiler/                 # 复用已验证的编译器
└── tests/                    # 测试
```

## 技术栈

- **壳**: PyInstaller + 系统浏览器 (Edge/WKWebView)
- **后端**: Python + pygls (LSP) + pyserial (USB) + Claude API
- **前端**: Monaco Editor + WebSocket + Chart.js
- **打包**: PyInstaller → 单文件exe/dmg/AppImage
