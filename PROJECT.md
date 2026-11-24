# CampusRun Console · 项目说明

> 这是项目的**稳定身份**：它是什么、为什么存在、边界在哪。
> 当前进度、最近决策、已知问题在 `PROJECT_CHAIN.md`；具体功能与 UI 规范在 `可视化页面/` 下的规格文件。
> 最后核对：2026-11-11

## 一句话

把一套只有 CLI 的校园跑协议，做成一台**完全跑在 Android 手机本机**的控制台 App：登录、看规则、预览路线、执行跑步、看结果与回查。

## 为什么存在

`协议/7.3.92` 是一套纯 Python 标准库实现的协议套件，原本只能这样用：

```bash
python cli.py -u <手机号> -p <密码>
python cli.py -t <Token> --uid <UID> --rules --profile
python cli.py -t <Token> --uid <UID> --run --distance 3000 --time-sec 1060
```

目标是让不做逆向工程的人也能用 UI 完成同一条链路，同时保留 CLI 不变。

纯浏览器网页做不到，原因有三条，决定了整个架构：

1. 协议请求用 `urllib` 直连并带自定义 `headerSign` / NetSec 加密，浏览器 `fetch` 会被 CORS 拦截。
2. 协议核心是 Python；WebView 只能跑 JS/WASM，跑不了 CPython。
3. 登录依赖 Node.js + Playwright 解极验；手机浏览器没有这层运行时。

所以唯一满足「手机本机 + 无服务进程 + 真实控制」的形态是：**把 Python 运行时打进 Android App**。

## 用户与场景

- 使用者：本人（单账号），在自己的 Android 手机上完成一次有效跑步。
- 典型流程：打开 App → 登录（必要时手动滑极验）→ 看规则/围栏/点位 → 配置距离与时长 → 预览路线与速度/步频曲线 → 提交执行 → 看阶段进度 → 结算与服务器回查。
- 关键体验约束：跑步链路不可见、不可逆、会写服务器，所以界面必须让用户随时知道「现在到哪一步、能不能取消、这次算不算数」。

## 交付形态与技术栈

- 交付：可侧载的 Android APK（debug 自签；release 自签 `.jks`）。
- 平台：Android，minSdk 24 / targetSdk 35，ABI 只支持 `arm64-v8a` 与 `x86_64`。
- 前端：Vue 3.5 + TypeScript + Vite 7 + Pinia 3 + Vue Router 4（hash 路由）+ ECharts 5。
- 壳与桥：Capacitor 7（`https://localhost` 是 WebViewAssetLoader 虚拟源，不是监听服务）+ Kotlin `ProtocolPlugin`。
- 运行时：Chaquopy 17.1.0 内嵌 CPython 3.12；协议包随 APK 打包。
- 无后端：App 不监听任何端口；仍需要联网访问目标 API、极验与华为 OBS。

## 架构与调用链

```text
Vue3 页面 / 组件
  → Protocol.<method>()            Capacitor JS proxy
  → Kotlin ProtocolPlugin          白名单、参数校验、单线程执行、安全存储
  → Chaquopy Python 3.12           app_bridge.py
  → 7.3.92 协议套件                sport_client / core / login / profile / policy / telemetry / running
  → HTTPS                          目标 API · GeeTest · 华为 OBS
```

事件反向走：Python `event_callback` → Kotlin `ProgressSink` → `notifyListeners('runProgress')` → Pinia run store → Vue 阶段进度 / 日志。

分层职责：

| 层 | 负责 | 不负责 |
| --- | --- | --- |
| Vue | 页面、输入、预览、进度、错误展示 | 协议请求、Python 执行、原生存储 |
| Kotlin | 桥方法白名单与校验、线程、EncryptedSharedPreferences、设备信息 | 业务协议细节 |
| Python | 登录、规则、轨迹合成、签名、OBS、结算、回查 | UI 与导航 |

## 仓库地图

```text
刷校园跑/
├─ README.md                  免责声明、研究范围与授权限制
├─ 协议/7.3.92/               Python 协议套件（CLI 仍可用）
├─ 原始脱壳资料/              逆向资料，只读参考
├─ _analysis/                 分析产物
├─ 可视化页面/
│  ├─ AI_SPEC.json            功能 / 协议 / 架构事实源（approved, v1.0.0）
│  ├─ UI设计说明.md           页面逻辑（功能 / 交互 / 状态 / 跳转 / 文案，v3.0.0）
│  ├─ UI实现规格.md           实现契约（对应旧 UI v2，待按 v3 重写）
│  ├─ 路线方案/
│  │  ├─ OSM自动轨迹生成与上传方案.md  避建筑路线与上传链路
│  │  ├─ 手动路线与路线库方案.md      第二路线源与路线库
│  │  └─ 锚点就近取点方案.md          取点锚点池（已实现，桌面端）
│  └─ app/                    （规划中，尚未创建）Vue + Capacitor 工程
├─ PROJECT.md                 本文件
├─ PROJECT_CHAIN.md           思维链：当前状态 + 决策/里程碑
└─ .claude/skills/campus-run-frontend/   前端规范 skill
```

> 注意：AI_SPEC 的 `repoLayout` 规划了 `可视化页面/app/`，但当前仓库里还没有 `app/`。不要假装它存在；搭骨架属于 M0。
>
> 2026-11-11：原 `UI_SPEC.json`、`方案说明.md`、`静态稿/` 已按用户要求永久删除。页面行为与文案以 `UI设计说明.md` v3.0.0 为准；视觉规范、静态稿与实现契约待重建。

## 首版范围

**做：**

- 登录 / Token 管理 / 个人档案
- 规则、有效时段、人脸抽检状态、电子围栏、官方点位
- 跑步配置、路线预览（不写服务器）、全链路执行、阶段进度
- 结果：rrid、有效性、指标、签名摘要、OBS 状态、服务器回查、轨迹导出

**不做：**

- 历史列表、打卡日历、广告、排行榜、多账号
- 服务端托管、代理、云同步
- iOS
- 自动极滑识别（v1 只做手动滑块）
- 真实地图瓦片（轨迹用自绘 SVG）
- 后台常驻服务（跑步期间保持前台）

## 不变量（不可违反）

- 无后端、无本地 HTTP 服务、无监听端口、无 PC / Termux 依赖。
- App 仍需联网；「无服务器」不等于「离线」。
- Python 必须是 3.12（协议里用了嵌套引号 f-string）；Chaquopy 只支持两个 64 位 ABI。
- 前端不嵌入 token、密码、盐、验证码 ID 等敏感值。
- 桥方法必须白名单，Kotlin 与 Python 两侧都校验输入。
- 路由使用 hash 模式，避免 WebView 深链问题。
- 不使用外部字体、CDN、统计或崩溃上报。
- 仓库路径含中文；构建时用 UTF-8 安全设置或 ASCII 构建路径。

## 里程碑

| 阶段 | 目标 | 关键交付 |
| --- | --- | --- |
| M0 | 脚手架与桥验证 | Vue + Capacitor 工程、Chaquopy hello-world、ProtocolPlugin ping、极验 WebView 试验 |
| M1 | 登录 | 登录 UI、手动极验、Token 安全存储、档案、会话恢复 |
| M2 | 规则 | 规则页、围栏、点位、SVG 地图、多区域处理 |
| M3 | 预览 | 跑步配置、prepare_run、路线/图表、演练 |
| M4 | 执行 | 全链路、阶段事件、结果、回查、取消 |
| M5 | 加固 | 错误处理、真机测试、签名、文档复核 |

## 当前待确认（只列入口，不在此展开）

`UI设计说明.md` §6 与 `PROJECT_CHAIN.md` 的「当前状态」是权威列表。最影响实现的三项：

- **GAP-02（blocking）**：跑步配置缺少 `runAreaId` 入参，多区域学校无法选择区域。
- **Q-05**：目标学校是单个还是多个跑步区域，决定是否需要区域选择器。
- **Q-04**：真实提交验证使用哪个测试账号。

## 去哪里找什么

| 想知道 | 看 |
| --- | --- |
| 功能能不能做、协议怎么走 | `可视化页面/AI_SPEC.json` |
| 页面有什么功能、点击后发生什么、状态怎么变、跳到哪里、看到什么文案 | `可视化页面/UI设计说明.md` |
| 路线规划与上传链路 | `可视化页面/路线方案/OSM自动轨迹生成与上传方案.md` |
| 接口调用、桥方法、错误码、脱敏、GAP / 假设 | `可视化页面/UI实现规格.md`（待按 v3 重写） |
| 为什么变成现在这样 | `PROJECT_CHAIN.md` |
| 前端怎么改、改完怎么验 | `.claude/skills/campus-run-frontend/SKILL.md` |

> 视觉规范、静态稿与「人读方案」在 2026-11-11 已删除，重建前不要引用旧路径。
