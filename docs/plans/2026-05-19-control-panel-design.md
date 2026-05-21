# Source1 控制面板设计

## 目标

为 `chatgpt_assistant_source(1)` 制作一个可打包为 Windows `.exe` 的桌面控制面板。使用人员双击启动后，可以管理配置、资源池，并从面板启动脚本流程。

## 已确认需求

- 只修改 `chatgpt_assistant_source(1)`，不再同步其它源码副本。
- 桌面窗口 GUI，不使用浏览器页面。
- 不加登录密码。
- API key、代理、卡密等敏感内容全部明文显示。
- 导入和导出只支持 TXT。
- 面板管理范围包括 API Key、代理池、卡密池、虚拟卡池、手机号池、邮箱池、长链接池、待授权账号池、`.env` 常用配置。
- 面板提供流程启动按钮：流程1、流程2、流程3、全自动 1 -> 2 -> 3。
- 面板显示实时日志。
- 发行方式为 PyInstaller 文件夹版，不做纯单文件 exe。

## 推荐技术方案

使用 Python + Tkinter 构建桌面 GUI。理由是 Tkinter 随 Python 标准库提供，新增依赖少，适合当前项目这种脚本型工具。打包使用 PyInstaller 的 `onedir` 模式，生成一个包含 `ControlPanel.exe`、配置文件、数据目录、输出目录和运行依赖的发行文件夹。

## 界面结构

左侧导航栏：

- 运行流程
- API Key
- 代理池
- 卡密池
- 虚拟卡池
- 手机号池
- 邮箱池
- 长链接池
- 待授权账号
- 常用配置
- 日志

资源池页面统一提供：

- 查看当前 TXT 内容
- 保存
- 导入 TXT
- 导出 TXT
- 清空
- 去重
- 刷新

运行流程页面提供：

- 邮箱源选择
- 数量和并发输入
- 流程1按钮
- 流程2按钮
- 流程3按钮
- 全自动按钮
- 停止任务按钮
- 实时日志窗口
- 结果输出面板

结果输出面板需要和实时日志分开显示。日志保留完整流水；结果面板只显示每次任务的结构化结果：

- 时间
- 流程
- 账号或邮箱
- 状态：成功 / 失败
- 失败原因或成功产物
- 相关输出路径

每次运行结束后，结果面板应能快速看出本次成功几个、失败几个，以及失败账号分别因为什么失败。第一版可以从子进程日志中解析 `[OK]`、`[FAIL]`、`失败:`、`成功`、`link ok`、`支付成功`、`授权成功` 等关键词生成结果行；后续再扩展为 runner 输出 JSONL 事件。

## 数据文件映射

- 代理池：`data/proxies/proxies.txt`、`data/proxies/proxies_jp.txt`、`data/proxies/proxies_us.txt`
- 卡密池：`data/paypal/card_codes.txt`
- 虚拟卡池：`data/paypal/cards.txt`
- 手机号池：`data/paypal/phones.txt`
- Hotmail 邮箱池：`data/hotmail/accounts.txt`、`data/hotmail/mail_pool.txt`
- iCloud 邮箱池：`data/icloud/accounts.txt`、`data/icloud/mail_pool.txt`
- 通用邮箱池：`data/accounts.txt`、`data/mail_pool.txt`
- PayPal 长链接池：`output/paypal注册/长链接账号/account.txt`
- PayPal 待授权池：`output/paypal注册/待授权账号/account.txt`
- 常用配置：`.env`

## 配置管理

API Key 和常用开关从 `.env` 读取和写入。第一版只做常用字段，不做完整 `.env` 编辑器。未知字段必须原样保留，保存时只更新面板认识的键。

重点字段：

- `MOEMAIL_API_KEY`
- `AUTH_SERVER_API_KEY`
- `HERO_SMS_API_KEY`
- `GRIZZLY_API_KEY`
- `FIVESIM_API_KEY`
- `CAPSOLVER_API_KEY`
- `TWOCAPTCHA_API_KEY`
- `PAYPAL_CARD_REDEEM_API_KEY`
- `PAYPAL_CAPTCHA_MODE`
- `CAPTCHA_API_PROVIDER`
- `PAYPAL_USE_PROXY`
- `PAYPAL_REGISTER_USE_PROXY`
- `PAYPAL_PROXY_FILE`
- `PAYPAL_REGISTER_PROXY_FILE`
- `PAYPAL_CARD_REDEEM_ENABLED`

## 流程启动

控制面板不直接重写业务逻辑，而是以子进程方式调用当前 Python 解释器运行 `main.py` 或专用入口脚本。日志通过 stdout/stderr 实时读取并显示在面板日志区域。

为了避免交互式菜单难以控制，第一版应新增一个轻量 CLI 入口，例如 `panel_runner.py`，由面板传入动作、数量、并发等参数，然后调用现有模块函数：

- `run_paypal_register`
- `run_paypal_pay`
- PayPal 授权入口
- 全自动编排入口

`panel_runner.py` 应尽量输出机器可解析的结果行。推荐格式为 JSONL，每行一个事件，例如：

```json
{"type":"result","flow":"paypal-flow1","account":"user@example.com","status":"success","message":"link ok","path":"output/paypal注册/长链接账号/account.txt"}
```

GUI 日志面板显示原始输出；结果输出面板优先读取 JSONL 事件。如果某些旧流程暂时没有 JSONL，GUI 可用关键词解析作为兜底。

## 打包方案

使用 PyInstaller `onedir` 模式生成发行目录。发行目录保留可写文件夹：

- `data/`
- `output/`
- `profiles/`
- `logs/`
- `.env`
- `config.yaml`

不推荐纯单文件 exe，因为资源池和浏览器 profile 都需要频繁读写。

## 风险和边界

- 所有敏感内容明文显示，使用人员需要自行保护发行目录。
- 浏览器自动化仍依赖 Playwright 浏览器组件，打包时要验证运行环境能找到浏览器。
- 资源池格式不统一，第一版以纯文本编辑为主，表格化只用于简单行列表。
- 流程启动仍可能受代理、验证码、页面变化影响；面板只负责管理和运行，不改变核心业务风控逻辑。
