# PeerTrain 部署到 PythonAnywhere

## 1. 本次准备好的部署内容

- 数据库：`deployment/peertrain-pythonanywhere.sqlite3`。
- 核对清单：`deployment/peertrain-pythonanywhere.manifest.json`，记录 session ID、各表数量及数据库 SHA256。
- 环境变量模板：`deployment/pythonanywhere.env.example`。
- WSGI 模板：`deployment/pythonanywhere_wsgi.py`。

预设数据库有三个账户，密码均为 `password123`：

| 用途 | 邮箱 / 用户名 | 角色 |
| --- | --- | --- |
| 管理员演示 | admin@university.ac.uk | ADMIN |
| 学员演示 | alex.chen@university.ac.uk | TRAINEE |
| 新增学员 | teo.wang@university.ac.uk | TRAINEE |

管理员保留 PeerTrain 管理权限，不额外授予 Django superuser 权限。Teo 是普通 trainee；Alex 保留项目现有的演示账户标识行为。

当前快照包含 Alex 的 **15 个 completed session**，筛选条件为 `start_time >= 2026-11-01 00:00:00 Europe/London` 且当前状态为 `COMPLETED`。一并保留 141 条对话消息、15 份反馈、4 份反思、33 条反思聊天，以及有关联的逐轮评估、安全检查、反馈审阅和交互事件。其他日期、其他账户或其他状态的 session 不进入快照。原本地 `db.sqlite3` 不变。

`DEBUG=false` 时登录页默认隐藏 Demo accounts，部署模板还显式设置 `SHOW_DEMO_ACCOUNTS=false`。这会移除页面上的演示账户内容，但账户本身仍能登录。

数据库与清单已加入 Git 忽略规则，需要单独上传；它们不在静态文件目录内。

本地验证：6 项部署及 seed 测试通过；部署快照的所有 session 及关联记录与源数据库逐行一致，SQLite integrity 和外键检查通过，本地源数据库 SHA256 未变化。扩大的 PageSmokeTests 检查存在 18 处既有断言失败，使用原有 seed 逻辑复测结果相同，涉及现有页面内容及展示预期；本次未修改这些无关页面。PythonAnywhere 线上环境与真实 AI 连接仍需按第 9 步验收。

## 2. 确认 PythonAnywhere 环境

本项目锁定 Django 6.0.6，要求 Python 3.12 或以上。本文统一使用 **Python 3.13**；虚拟环境和 Web app 必须选相同版本。

在 PythonAnywhere 的 **Account → System image** 检查系统镜像；官方列表中的 `innit` 支持 Python 3.13。如果账户只能选择 3.10/3.11，先升级系统镜像，再建立 Web app。不要直接把现有 Django 6 项目放到 Python 3.10/3.11。

官方说明：[Python 版本](https://help.pythonanywhere.com/pages/PythonVersions)、[Django 6 的 Python 要求](https://docs.djangoproject.com/en/6.0/faq/install/)。

免费账户可以先做功能演示，但外部 API 请求经过代理及域名允许列表。PeerTrain 需要访问配置的 AI 服务，务必完成后面的真实对话测试；付费账户没有这项出站限制。免费 Web app 还需留意到期和续期。

官方说明：[出站网络限制](https://help.pythonanywhere.com/pages/403ForbiddenError/)、[免费账户功能](https://help.pythonanywhere.com/pages/FreeAccountsFeatures/)。

## 3. 上传项目代码

后文把 PythonAnywhere 用户名写成 `YOUR_USERNAME`，请全部替换成实际值；项目目录统一使用 `/home/YOUR_USERNAME/PeerTrain`。

方式 A：代码已提交到你的仓库时，在 PythonAnywhere **Consoles → Bash** 执行：

```bash
cd ~
git clone https://github.com/你的组织或用户名/你的仓库.git PeerTrain
cd ~/PeerTrain
```

请确保克隆到的是包含本次改动的分支或提交。本地尚未提交的文件不会随 clone 上传。私有仓库按 GitHub 的认证方式授权，不要把令牌写进项目文件。

方式 B：通过 **Files** 上传项目源码 ZIP，在 Bash 解压后确保以下文件位于同一目录：

```text
/home/YOUR_USERNAME/PeerTrain/manage.py
/home/YOUR_USERNAME/PeerTrain/requirements.txt
/home/YOUR_USERNAME/PeerTrain/config/settings.py
```

打包源码时不包含 `.git`、本地虚拟环境、`.env`、本地 `db.sqlite3`、缓存或密钥。部署数据库按第 6 步单独上传。

## 4. 创建虚拟环境并安装依赖

```bash
cd ~/PeerTrain
mkvirtualenv --python=/usr/bin/python3.13 peertrain-venv
python --version
python -m pip install -r requirements.txt
python -m django --version
```

预期版本分别为 Python 3.13.x 和 Django 6.0.6。以后重新打开 Bash 时，先运行：

```bash
workon peertrain-venv
cd ~/PeerTrain
```

## 5. 创建云端 .env

```bash
cp deployment/pythonanywhere.env.example .env
chmod 600 .env
python -c 'import secrets; print(secrets.token_urlsafe(64)); print(secrets.token_urlsafe(32))'
```

在 **Files** 编辑项目根目录的 `.env`：第一串随机值填 `DJANGO_SECRET_KEY`，第二串填 `ADMIN_REGISTRATION_CODE`。填写真实用户名、域名和 API key，示例：

```dotenv
DEBUG=false
SHOW_DEMO_ACCOUNTS=false
DJANGO_SECRET_KEY=刚刚生成的第一串随机值
ALLOWED_HOSTS=YOUR_USERNAME.pythonanywhere.com
DJANGO_DB_PATH=/home/YOUR_USERNAME/PeerTrain/db.sqlite3
ADMIN_REGISTRATION_CODE=刚刚生成的第二串随机值
LLM_PROVIDER=openai
OPENAI_API_KEY=你的实际密钥
OPENAI_MODEL=与你本地成功使用的模型一致
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=1
BOUNDARY_JUDGE_ENABLED=false
```

如果你用 Gemini，设置 `LLM_PROVIDER=google`，填写 `GEMINI_API_KEY` 和 `GOOGLE_MODEL`。如果本地启用了 boundary judge，同步相应的 `BOUNDARY_JUDGE_*` 设置。不要把本地 `.env` 直接提交进仓库。

如果 PythonAnywhere 分配的实际域名不同，例如 EU 账户或自定义域名，将 `ALLOWED_HOSTS` 改为 Web 页显示的真实域名，只写域名、不带协议或路径。多个域名以英文逗号分隔。项目会据此生成 HTTPS 的 CSRF trusted origins。

项目的 `config/settings.py` 会自动读取根目录 `.env`，命令行和 WSGI 均适用；不需要只在某个 Bash 会话中 `export`。已有系统环境变量优先于 `.env`。

## 6. 安装预设数据库（仅首次部署）

通过 **Files** 将本地文件：

```text
D:\Coding\Git\FerryBot\PeerTrain\deployment\peertrain-pythonanywhere.sqlite3
```

上传到云端项目的 `deployment/` 目录，也可同时上传 `.manifest.json` 核对清单。

首次部署、尚未开放网站时执行：

```bash
cd ~/PeerTrain
test ! -e db.sqlite3 && cp -n deployment/peertrain-pythonanywhere.sqlite3 db.sqlite3
ls -lh db.sqlite3
python manage.py migrate --noinput
python manage.py check
```

如果已经存在 `db.sqlite3`，第一条复制命令不会覆盖它。先确认现有库是否有需保留的数据并备份，再决定如何迁移；不要在网站已有新用户数据后再次用预设库替换线上数据库。

**这里不需要执行 `seed_peertrain` 或 `createsuperuser`**：生成的数据库已完成迁移并包含三个账户及 Alex 的历史记录。

核对账户与 session：

```bash
python manage.py shell -c 'from accounts.models import User; from training.models import TrainingSession; print(list(User.objects.values_list("email", "role"))); print(list(TrainingSession.objects.values_list("id", "status", "start_time"))); print("sessions:", TrainingSession.objects.count())'
```

预期为三个账户及 15 个 completed session。数据库位置由 `DJANGO_DB_PATH` 决定，不填写时默认项目根目录 `db.sqlite3`。

## 7. 创建 Web app 并配置 WSGI

在 PythonAnywhere **Web → Add a new web app**：

1. 选择所需域名。
2. 选择 **Manual configuration**。
3. 选择 **Python 3.13**。
4. 在 **Virtualenv** 填 `/home/YOUR_USERNAME/.virtualenvs/peertrain-venv`。
5. 在 **Code** 中，将 Source code 和 Working directory 都填为 `/home/YOUR_USERNAME/PeerTrain`。
6. 点击 Web 页的 **WSGI configuration file** 链接，使用下面内容替换默认示例并保存：

```python
import os
import sys

project_path = "/home/YOUR_USERNAME/PeerTrain"
if project_path not in sys.path:
    sys.path.insert(0, project_path)

os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"

from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

模板在 `deployment/pythonanywhere_wsgi.py`。务必修改 **Web 页链接指向的文件**（通常位于 `/var/www/`），仅修改仓库中的 `config/wsgi.py` 不会配置 PythonAnywhere 的入口。无需运行 `runserver`。

官方说明：[部署现有 Django 项目](https://help.pythonanywhere.com/pages/DeployExistingDjangoProject)。

## 8. 配置静态文件和 HTTPS

在 Bash 中执行：

```bash
workon peertrain-venv
cd ~/PeerTrain
python manage.py collectstatic --noinput
```

在 **Web → Static files** 添加映射：

| URL | Directory |
| --- | --- |
| /static/ | /home/YOUR_USERNAME/PeerTrain/staticfiles |

如果 Web 页提供 **Force HTTPS**，将其开启。项目在 `DEBUG=false` 时使用 secure cookies，因此验收及登录均使用 `https://`。

点击 Web 页顶部 **Reload**。

官方说明：[Django 静态文件](https://help.pythonanywhere.com/pages/DjangoStaticFiles)。

## 9. 上线验收

打开 `https://你的实际域名/`，依次验证：

1. 首页、登录页的样式与图标正常，登录页没有 Demo accounts 或演示密码。
2. Teo 可以用指定邮箱和 `password123` 登录，初始没有训练历史。
3. Alex 可以登录，并查看导入的 15 个 completed session，以及对话、反馈与已有反思。
4. admin 可以登录并访问 PeerTrain 管理页面。
5. 用 Teo 开始一次训练，实际发送消息，确认 AI 回复和反馈正常。仅验证首页或登录不能验证外部 API 的连接和密钥。

最后在 Bash 运行部署检查：

```bash
python manage.py check --deploy
```

当前项目未默认启用 Django 的 HTTPS 重定向和 HSTS，这两项可能产生提示。HTTPS 强制跳转在 PythonAnywhere 配置；HSTS 可在确认域名长期提供 HTTPS 后另行配置。不要为消除提示而把 `DEBUG` 改回 `true`。

## 10. 常见问题

| 现象 | 检查方向 |
| --- | --- |
| 安装报 Python 版本不兼容 | Django 6 需要 Python ≥3.12；确认虚拟环境和 Web app 都是 3.13 |
| DisallowedHost / 400 | `.env` 的 `ALLOWED_HOSTS` 是否包含实际域名，修改后 Reload |
| CSRF / 403 或无法保持登录 | 使用 HTTPS，确认域名及 CSRF origin；项目默认根据 ALLOWED_HOSTS 生成 HTTPS origin |
| 页面无样式 | `collectstatic` 是否成功，映射目录是否为 `staticfiles` |
| no such table | 核对 DJANGO_DB_PATH、上传数据库位置，以及 migrate 是否成功 |
| AI 请求 403 / Connection refused | 免费账户出站允许列表及代理、AI 服务域名、API key、服务额度 |
| 500 / 502 / 504 | 查看 Web 页 Error log 和 Server log，确认 WSGI 路径、虚拟环境、AI 请求耗时 |
| 登录页仍显示 Demo accounts | 确认云端 `.env` 为 SHOW_DEMO_ACCOUNTS=false，且新模板已上传并 Reload |

不要把 `.env`、API key 或数据库映射到公开的 `/static/`。

## 11. 后续更新

更新代码前备份线上数据库；下面用 SQLite backup API 得到一致的备份：

```bash
workon peertrain-venv
cd ~/PeerTrain
mkdir -p backups
python -c 'import os, sqlite3; from datetime import datetime; from dotenv import load_dotenv; load_dotenv(); src=sqlite3.connect(os.getenv("DJANGO_DB_PATH") or "db.sqlite3"); dst=sqlite3.connect("backups/db-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".sqlite3"); src.backup(dst); dst.close(); src.close()'
bash scripts/pythonanywhere_update.sh
```

更新脚本默认从 `origin/main` 拉取、安装依赖、检查、迁移、收集静态文件并尝试 Reload。分支不同可使用 `--branch 分支名`；WSGI 路径不同可使用 `--wsgi-path /var/www/实际文件名.py`。也可在 Web 页面手动 Reload。

常规更新不要重新上传或覆盖数据库。不要随意加 `--seed`：现有 seed 命令会重置三个预设账户的密码为 `password123`，并刷新场景和平台设置。

## 12. 将来重新生成预设数据库

在本地、包含源数据的项目目录执行（使用 Python ≥3.12 环境）：

```powershell
uv run --python 3.14 --with-requirements requirements.txt python manage.py build_deployment_db --since 2026-11-01 --output deployment/peertrain-pythonanywhere-v2.sqlite3
```

命令只读取源数据库，生成一个完成迁移、含三个预设账户及筛选记录的新数据库，并附带 manifest。现有目标文件不会被覆盖；每次使用新的输出文件名。该命令用于制作首次部署快照，不是线上增量同步命令。
