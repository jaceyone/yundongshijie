# 校园跑控制台 · UI 实现规格（超前内容）

> ⚠️ **已过期（2026-11-11）**：本文件对应已删除的 UI v2（6 页面 / 37 静态状态），尚未按 `UI设计说明.md` v3.0.0 重写。
> 新页面逻辑加入了避建筑路线、演练结果、轨迹回读、本地待重试任务与重试提交；本文件中的页面章节、状态与调用说明不能直接当作新实现契约。
> 重建顺序：先按 `UI设计说明.md` v3.0.0 更新页面行为，再重写本文件的接口、桥方法、错误码与脱敏规则。

> 版本 1.0.0 · 2026-11-06
> 本文件收纳从 `UI设计说明.md` v1.0.1 拆出的实现契约：数据与调用、错误码、安全脱敏、功能标注、缺口/假设/待确认。
> 页面行为以 `UI设计说明.md` v3.0.0 为准；视觉数值待新的视觉规范重建；功能 / 协议以 `AI_SPEC.json` 为准。
> 下文小节编号沿用旧文档 v1.0.1，便于逐条对照；在新契约完成前，不要据此实现新页面。

---

## 1. 数据与调用

### 1.1 登录（原 §5.1）

**数据与调用**

| 时机 | 调用 | 参数 | 使用返回 |
|---|---|---|---|
| 冷启动 | `initialize()` | — | `hasSession` 决定是否跳控制台 |
| 点击登录 | `prepareLogin({username})` | 去首尾空格的手机号 | `sessionUuid`、`needCaptcha`、`captchaId` |
| 需要验证 | 打开 `CaptchaSheet` | `captchaId` | 回传 `lotNumber`、`captchaOutput`、`passToken`、`genTime` |
| 提交 | `submitLogin({username,password,sessionUuid,geetest?})` | 密码仅内存 | `profile` |
| 会话过期 | `authExpired` 事件 | — | 清会话、跳登录、Toast |

> `submitLogin()` 成功后，桥接自动把 `uid / unid / token` 写入 SecureStore；若返回的 `UserProfile` 含 `token`，Vue 必须在写入 auth store 前剥离，不持久化、不渲染。

错误码处理：`INVALID_INPUT` 行内提示；`NETWORK` 可重试；`SERVER` 显示服务端 message；`AUTH_FAILED` 显示「手机号或密码不正确」并清空密码；`CAPTCHA_REQUIRED` 打开 Sheet；`CAPTCHA_FAILED` 留在 Sheet 重试；`INTERNAL` 显示 `traceId`。

### 1.2 Token 自动写入（原 §5.2）

**页面**：无。`/login/token`、`TokenImportPage.vue` 与 `importSession` 从 v1 UI 中移除。

**流程**

1. 用户在 `/login` 输入手机号、密码。
2. `prepareLogin` 判断是否需要极验；需要则打开 `CaptchaSheet`。
3. `submitLogin` 成功，返回 `profile`（可能含 token）。
4. Kotlin/Python 桥接层把 `uid / unid / token` 写入 `SecureStore`。
5. Vue 在写 auth store 前剥离 `profile.token`，只保留姓名、校区、班级等展示字段。
6. 跳转控制台；下次冷启动 `initialize()` 读取 SecureStore，`hasSession=true` 直接恢复会话。

**规则**

- 前端不做「手动获取 token」的任何提示或入口。
- 登录失败不写入任何凭证；密码只在内存。
- Token 过期：`authExpired` → `logout()` 清会话（保留设备 ID）→ 回登录页。
- `TokenImportPage.vue` 如仍存在于仓库，v1 不挂路由、不构建入口。
- 上游 `AI_SPEC` 的 `/login/token` 是 PC 兜底方案；本版按产品要求取消该兜底。若极验在 WebView 内被阻断，v1 无法登录，需要后续替代方案（见 §14 A-08）。

**验收**

- 登录成功后重启 App 能直接进入控制台，无需任何 token 输入。
- 界面、日志、导出中不出现 token。
- 不存在 `/login/token` 路由或 token 输入表单。
- `submitLogin` 失败不产生本地会话。

### 1.3 跑步首页：原控制台（原 §5.3）

**数据与调用**

| 区块 | 调用 | 参数 | 失败表现 |
|---|---|---|---|
| 档案 | `getProfile()` | — | 卡片级 ErrorPanel；`AUTH_EXPIRED` 则跳登录 |
| 学期 | `getSemesterProgress({mode:1})` | mode 固定 1 | 卡片显示「暂无法获取学期进度」+ 重试；其余区块照常 |
| 规则 | `getPolicy({mode:1})` | mode 取 policy store | 卡片级错误；「开始跑步」仍可点，进入配置页再重试 |
| 在线 | `ui.online` | — | 离线时 Header 显示「离线」danger 点 + 顶部 28 高提示条 |

- 三个请求并行；各自独立 loading/error，不做整页阻塞。
- 刷新按钮重新拉三份数据；保留旧数据直到新数据返回（`stale-while-revalidate`），刷新中图标旋转 240ms。
- 第 1 次进入或 `fetchedAt` 超过 5 分钟才自动刷新；Tab 切换回来不强制刷新。

**检查清单推导规则**（必须在 store 计算，不允许写死）

| 项 | `ok` | `warn` | `danger` | `pending/skipped` |
|---|---|---|---|---|
| 登录状态 | `auth.loggedIn` | — | `expired/error` | `boot/restoring` |
| 跑步规则 | `rule != null` | 规则字段缺失（如 minDistance=0） | 拉取失败 | 加载中 |
| 围栏数据 | `fences.length > 0` | `fences.length == 0`（将使用合规兜底） | 拉取失败 | 加载中 |
| 官方点位 | mode 1 且 `checkpoints.length > 0` | `CHECKPOINT_FALLBACK` | 查询失败 | 未查询 / mode 6「自由跑无需点位」 |
| 人脸抽检 | `faceCheck.enable == false` | — | `enable == true` | 规则未加载 |
| 路线预览 | `run.preview` 存在且参数与当前一致 | 预览过期（参数已改） | 预览失败 | 未预览 |

### 1.4 规则与围栏（原 §5.4）

**数据与调用**

| 调用 | 参数 | 返回 | 触发 |
|---|---|---|---|
| `getPolicy({mode})` | 1 或 6 | `RunRule` | 首次进入、切换模式、刷新 |
| `getFences()` | — | `GeoFenceArea[]` | 首次进入、刷新；与 mode 无关 |
| `getCheckpoints({fenceId, mode:1, uuid?})` | 仅计分跑 | `{points, areaId, error?}` | 点击「查询」或切换围栏后自动查询 |
| `getSemesterProgress` | — | 不在此页使用 | — |

- 切换模式：立刻清空 `checkpoints` 与 `checkpointError`，重新 `getPolicy(mode)`；围栏保留。mode 6 不调用点位接口。
- 切换围栏：清空点位，自动重新查询（计分跑）；查询中列表显示骨架。
- 刷新：并行重拉 rule + fences；已加载点位若围栏未变则保留。
- `getCheckpoints` 返回 `error` 字段或 `CHECKPOINT_FAILED`：显示错误面板，不静默使用 `fence.checkPoints` 旧数据。

**多跑步区域（GAP-02）**

- 触发：`MULTI_RUN_AREA`，或返回 `error` 含区域冲突。
- UI：底部 `ConfirmSheet` 变为「选择跑步区域」列表，选项来自 `rule.raw.runAreaModels` 中 `valid === true` 的项，显示区域名 + 关联围栏名。
- 选择后：保存 `runAreaId`，重新查询点位。
- **契约缺口**：现有 `getCheckpoints` / `previewRun` / `executeRun` 没有 `runAreaId` 参数。文档按「新增可选 `runAreaId`」设计，详见 §14 GAP-02；未补齐前，选择后显示「区域选择尚未接通，请稍后重试」，不得静默选第一个。
- 不会自动选 `select=true` 的区域；用户必须明确选择。

### 1.5 跑步配置（原 §5.5）

**数据与调用**

| 调用 | 参数 | 返回 | 时机 |
|---|---|---|---|
| `getPolicy({mode})` | 1/6 | `RunRule` | 进入页面、从规则页返回 |
| `getFences()` | — | `GeoFenceArea[]` | 进入页面 |
| `getCheckpoints({fenceId,mode:1,uuid?})` | 计分跑 | 点位 | 进入页面（若规则页已加载则复用） |
| `previewRun({mode,distanceM,timeSec,fenceId?,uuid?})` | 当前参数 | `RunPreview` | 首次进入自动、参数稳定 600ms 后自动、手动刷新 |
| `executeRun({mode,distanceM,timeSec,dryRun})` | 当前参数 | `RunResult` | 点击主按钮并确认后 |
| `cancelRun()` | — | — | 运行页使用 |

- 预览只读：不申请 OBS 地址、不 PUT、不 save/record、不 `get_one_record`。
- 真实提交**不复用**预览的 `preparedId`，由后端在提交前重新 prepare，确保时间戳最新。
- 预览请求失败保留上一次成功预览，标记「预览已过期」，主按钮禁用。
- 防抖期间显示「预览更新中…」细条，不阻塞输入。

### 1.6 运行中（原 §5.6）

**事件到界面**

| 事件 | 载荷 | 界面处理 |
|---|---|---|
| `runProgress` | `{runId, stage, status, message, percent, ts, data?}` | 更新对应阶段行、总进度、当前阶段卡；`percent` 只增不减 |
| `runLog` | `{runId, level, message, ts}` | 追加日志；最多 500 条；info/warn/error 着色 |
| `authExpired` | `{message}` | 立即停止轮询式更新、清会话、跳登录；运行页显示 Toast「登录已过期」 |
| `executeRun` resolve | `RunResult` | 成功后 600ms 跳结果页；失败/取消同样进结果页 |
| `cancelRun` resolve | `{cancelled, stage}` | 成功：按钮「正在取消…」→ 结果页 cancelled；`CANCEL_TOO_LATE`：恢复按钮禁用态 + Toast |

**阶段映射与元信息**

| 行 | stage | 成功元信息示例 | 失败表现 |
|---|---|---|---|
| 01 | `time-sync` | `+0.12s` / `已同步` | 红色节点 + 「时间同步失败」 |
| 02 | `policy-fence` | `2 个围栏` / `12 个点位` | 「规则或围栏加载失败」 |
| 03 | `trajectory` | `353 点` / `回填时间` | 「轨迹生成失败，未上传」 |
| 04 | `obs` | `48 KB` / `HTTP 200` | 「轨迹上传失败」；此时不可取消 |
| 05 | `settle`+`recheck` | `rrid 123…` / `有效` | settle 失败→「结算失败」；recheck 失败→「回查未完成」warn，结果未知 |

- `settle` 与 `recheck` 共用第 05 行：先显示「结算中…」，settle 成功后显示「回查中…」，recheck 失败时该行标 `warn`，不标失败。
- `skipped` 状态显示灰色短横 + 「已跳过」，例如自由跑的某些步骤。
- 事件乱序或重复：以 `stage + status` 的最后一次为准；`percent` 取最大值。
- 事件里没有的阶段不擅自推进；不按定时器假装进度。

### 1.7 跑步结果（原 §5.7）

**数据与状态推导**

| 字段 | 来源 |
|---|---|
| `runId` / `success` / `rrid` / `totalDistance` / `totalTime` / `avgPaceSecPerKm` | `executeRun` 返回 `RunResult` |
| `isValid` | `RunResult.isValid`（`true/false/null`） |
| `obs` | `RunResult.obs`：`putOk/putStatus/bytes/objectKey/message` |
| `signature` | `RunResult.signature`：字段数、掩码字段、掩码签名 |
| `recheck` | `RunResult.recheck` / `recheckRecord` 返回 `RunRecordDetail` |
| `warnings` / `traceId` | `RunResult` |

**状态推导顺序（必须严格按此顺序）**

1. 若本次为取消 → `cancelled`。
2. 若 `success === true`：
   - `isValid === true` → `success`
   - `isValid === false` → `notValid`
   - `isValid === null` → `unknown`
3. 若 `success === false`：
   - 已有 `rrid` 且 `isValid === null`（回查失败）→ `unknown`
   - `obs.putOk === true` 且无有效 `rrid` → `partial`
   - 否则 → `error`
4. 重新回查后重算：回查成功且 `isValid` 明确 → 覆盖为 success/notValid；回查失败 → `unknown`，**不得回到 success**。

- `isValid` 只在 `recheck` 或 `RunResult.isValid` 明确非 null 时显示「有效/无效」；否则一律「未知」。
- `partial` 不再显示 Hero 的成功对勾；即使 OBS 200 也不代表成绩已结算。

### 1.8 我的（原 §5.8）

**数据与调用**

| 调用 | 返回 | 用途 |
|---|---|---|
| `getProfile()` | `UserProfileDetails` | 姓名、年级班级、学院、校区 |
| `getRuntimeInfo()` | appVersion、protocolVersion、pythonVersion、deviceId、packageName | 信息行 |
| `logout()` | `{ok}` | 清除会话 |

- `initialize()` 的 `hasSession` 与 auth store 决定会话状态；不单独请求网络。
- 页面加载时并行 `getProfile` + `getRuntimeInfo`；profile 失败只影响 AccountCard，版本行仍显示。

**会话状态推导**

| auth 状态 | 徽标 | 凭证行 |
|---|---|---|
| `loggedIn` | `ok` 「有效」 | `••••••••` + `本机加密存储` |
| `expired` | `danger` 「已过期」 | `••••••••` + `请重新登录` |
| `loggedOut` | `neutral` 「未登录」 | `—` |
| `boot/restoring` | `neutral` 「检查中…」 | `—` |

> `initialize()` 返回的 `profile` 可能携带 token 字段；前端必须在 store 入口立即丢弃，不存储、不渲染。因此**不显示尾号**，固定 8 个圆点只表示「已保存」，不代表真实长度。

**设备 ID 与版本**

- 设备 ID：`mask(deviceId)`，规则为前 4 + `…` + 后 4；长度 < 12 时前 2 + `…` + 后 2；长度 < 5 全部 `•`。
- `ⓘ` 打开说明 Sheet：`设备 ID 用于保持登录设备一致；完整值不会显示，也不会随清除会话改变。`
- 版本行：应用 `0.1.0`、协议 `7.3.92`、Python `3.12`、包名 `com.local.campusrunconsole`（窄屏可截断，长按复制）。
- `getRuntimeInfo` 失败：值显示 `—`，行尾出现 44 的重试图标。

**清除会话**

- 点击 → `ConfirmSheet`（danger）：
  - 标题：`清除本机会话？`
  - 正文：`需要重新登录才能继续使用。设备 ID 会保留。`
  - 主按钮：`清除会话`；次按钮：`取消`
- 确认 → `logout()` → 清 auth/policy/run stores → `replace('/login')` → Toast「会话已清除」。
- 失败（`INTERNAL`）：Toast `清除失败，请重试`，保持页面。
- 会话已过期时，按钮文案改为 `重新登录`：先调用 `logout()` 清除失效会话（保留设备 ID），再 `replace('/login')`；`logout()` 失败也继续清 UI store 并跳转，避免死循环。

### 1.9 调用时序（原 §6）

#### 6.1 冷启动与会话恢复

```text
启动
 └─ initialize()                     （只读本机会话，不发网络）
     ├─ hasSession = true  → auth.loggedIn → 替换到 /
     └─ hasSession = false → auth.loggedOut → 替换到 /login
```

- 启动画面：`bg0` + 居中 logo + 12 号「正在恢复会话…」，最长 1.5s；超时也继续进入登录页。
- `INIT_FAILED`：进入登录页并 Toast「本机会话读取失败」，不阻塞登录。
- Token 存在但已失效时，`initialize` 不做网络校验；第一次业务请求返回 `AUTH_EXPIRED` 再统一处理。

#### 6.2 登录（含手动极验）

```text
[点击登录]
  → 本地校验非空
  → prepareLogin(username)
      ├─ needCaptcha = false → submitLogin(无 geetest)
      │     └─ 成功 → 写 SecureStore → 清空密码 → / → Toast「登录成功」
      │     └─ 失败 → 错误面板（保留手机号，清空密码）
      └─ needCaptcha = true
            → CaptchaSheet 加载 gt4.js
            → 用户拖动滑块
            → getValidate() 得到 4 字段
            → submitLogin(带 geetest)
                ├─ 成功 → 写 SecureStore → 清空密码 → /
                ├─ CAPTCHA_FAILED → 留在 Sheet 重试
                └─ 其他失败 → 关闭 Sheet，回表单错误
```

- `submitLogin` 内部顺序固定：先 `geeValidate`，后 `submitLogin`；前端不拆步骤。
- 密码只在内存；登录成功/失败/页面卸载时都清空。
- 极验 4 字段任一缺失视为失败，不提交。
- 用户关闭 Sheet → 回 `loggedOut`，本次 `sessionUuid` 作废；下次登录重新 `prepareLogin`。

#### 6.3 Token 自动写入与会话恢复

```text
[登录成功]
  → submitLogin 返回 profile（可能含 token）
  → 桥接自动写 uid/unid/token 到 SecureStore
  → Vue 剥离 profile.token，写 auth store
  → replace('/')
  → 下次冷启动 initialize() 读取 SecureStore → hasSession=true → /
```

- UI 不提供 token 输入、展示或复制入口。
- 登录失败不写入任何凭证。
- Token 过期：`authExpired` → `logout()` 清会话（保留设备 ID）→ `/login`。

#### 6.4 规则、围栏与官方点位

```text
进入 /policy
  → 并行 getPolicy(mode) + getFences()
  → 选中围栏（默认第一个）
  → mode = 1 → getCheckpoints({fenceId, mode:1})
      ├─ 成功 → 渲染点位 + 画布
      ├─ MULTI_RUN_AREA → 区域选择 Sheet → 重查（GAP-02）
      └─ 失败 → 点位区块 ErrorPanel；不静默使用 fence.checkPoints
  → mode = 6 → 不查询点位，显示「自由跑不查询官方点位」
```

- 切换模式：清空点位 → 重新拉规则。
- 切换围栏：清空点位 → 自动重查。
- 刷新：规则/围栏并行；围栏未变时保留已加载点位。

#### 6.5 预览

```text
参数合法
  → 600ms 防抖
  → previewRun({mode,distanceM,timeSec,fenceId})
      → 返回 RunPreview
      → 渲染：轨迹 / 速度 / 步频 / 签名摘要 / 警告
      → 不产生 OBS、save、record 请求
```

- 首次进入自动预览一次；参数变化后重新防抖预览。
- 预览中：画布/图表骨架；主按钮禁用。
- 预览失败：保留上一份预览并标「已过期」；主按钮禁用；ErrorPanel 可重试。
- 预览的时间戳是示例；真实提交会重新 prepare。

#### 6.6 提交与运行

```text
[开始提交]
  → 参数合法 + previewReady
  → ConfirmSheet（展示距离/时长/配速 + 不可撤销提示）
  → executeRun({mode,distanceM,timeSec,dryRun:false})
  → 立即 replace 到 /run/running
  → 订阅 runProgress / runLog
      → 阶段推进 → 100%
      → executeRun resolve
          ├─ success → 结果页 success/notValid/unknown
          ├─ partial → 结果页 partial
          ├─ cancelled → 结果页 cancelled
          └─ failed → 结果页 error
```

- 演练：`dryRun:true`，不弹确认，运行页显示「演练」徽标；同样展示阶段，但无 OBS/结算写入。
- 真实提交不复用预览的 `preparedId`；由后端在提交前重新 prepare。
- 页面跳转不等待网络；`executeRun` 的 Promise 由 store 持有，结果页读取 store，避免刷新丢失。

#### 6.7 结果、回查与导出

```text
进入 /run/result
  → 读取 store.result → 按 §5.7 顺序推导 Hero
  → [重新回查] → recheckRecord({rrid})
      ├─ 成功 → 更新 recheck + isValid → 重算 Hero
      └─ RECHECK_UNKNOWN → Hero unknown（不得 success）
  → [导出记录] → exportTrace()
      ├─ 成功 → 系统分享 Sheet（filename）
      └─ NO_TRACE/INTERNAL → Toast 错误
```

- 回查失败只影响「有效性」和回查行，不把已有 rrid/里程/签名抹掉。
- 导出由后端脱敏；前端不拼接原始 payload。

#### 6.8 取消与离开

```text
[取消运行]
  → ConfirmSheet
  → cancelRun()
      ├─ cancelled = true → 运行页 Toast「已取消」→ 结果页 cancelled
      ├─ CANCEL_TOO_LATE → Toast「上传已开始，无法取消」→ 按钮保持禁用
      └─ NO_ACTIVE_RUN → 回 /run
[返回/手势离开]
  → ConfirmSheet「确定离开运行页？」
      ├─ 留在本页
      └─ 取消运行并离开 → 同取消流程
```

- OBS 一开始，取消按钮立即禁用，不等接口返回。
- 取消后保持已完成的阶段历史，供结果页和导出使用。

#### 6.9 会话过期

```text
任意请求返回 AUTH_EXPIRED
  → 后端发 authExpired 事件
  → 前端：停止当前 UI 操作 → Toast「登录已过期」→ logout() 清 SecureStore → 清 auth/policy/run → replace /login
  → 设备 ID、设备档案保留
  → 登录成功后不自动恢复未完成的运行；用户需重新配置
```

- 运行中过期：保留日志到这次会话结束，结果页不显示成功；提示重新登录。
- 登录页显示「登录已过期，请重新登录」的一次性提示。

---

## 2. 错误码与警告码（原 §7.2–7.3）

### 7.2 错误码与界面映射

| code | 出现位置 | 标题文案 | 动作 | 可重试 |
|---|---|---|---|---|
| `NETWORK` | 区块/页面 | 网络连接失败 | 重试；顶部离线条 | 是 |
| `SERVER` | 区块/页面 | 服务器暂时不可用 | 重试；显示服务端 message | 是 |
| `AUTH_REQUIRED` | 任意 | 请先登录 | 跳登录 | — |
| `AUTH_EXPIRED` | 任意 | 登录已过期 | 清会话、跳登录 | — |
| `CAPTCHA_REQUIRED` | 登录 | 需要安全验证 | 打开 CaptchaSheet | — |
| `CAPTCHA_FAILED` | CaptchaSheet | 安全验证未通过 | 重新拖动 / 重新加载 | 是 |
| `RULE_FAILED` | 控制台/规则/配置 | 跑步规则获取失败 | 重试；回规则页 | 是 |
| `FENCE_FAILED` | 规则/配置 | 围栏数据获取失败 | 重试 | 是 |
| `CHECKPOINT_FAILED` | 规则/配置 | 官方点位查询失败 | 重试；不得静默兜底 | 是 |
| `MULTI_RUN_AREA` | 规则/配置 | 检测到多个跑步区域 | 打开区域选择 | — |
| `OBS_URL_FAILED` | 运行中/结果 | 上传凭证申请失败 | 重新提交 | 是 |
| `OBS_PUT_FAILED` | 运行中/结果 | 轨迹上传失败 | 重新提交 | 是 |
| `SAVE_FAILED` | 运行中/结果 | 结算失败 | 重新回查；显示服务端 code | 视情况 |
| `RECHECK_UNKNOWN` | 结果 | 回查未返回结果 | 重新回查；显示未知 | 是 |
| `CANCELLED` | 运行/结果 | 已取消 | 回配置页 | — |
| `CANCEL_TOO_LATE` | 运行 | 上传已开始，无法取消 | 继续等待 | — |
| `DRY_RUN` | 结果 | 演练完成 | 查看结果；不写服务器 | — |
| `INTERNAL` | 任意 | 出现未知错误 | 显示 traceId；重试 | 是 |
| `INVALID_INPUT` | 登录/导入/配置 | 输入有误 | 行内错误 | — |
| `INIT_FAILED` | 启动 | 本机会话读取失败 | 进入登录页 | 是 |
| `NO_TRACE` | 结果 | 暂无可导出的记录 | 先完成一次运行 | — |
| `NO_ACTIVE_RUN` | 运行 | 当前没有运行任务 | 回配置页 | — |

### 7.3 警告码（不阻断）

| code | 展示 |
|---|---|
| `FACE_CHECK_ENABLED` | 规则页/配置页 danger 横幅 |
| `CHECKPOINT_FALLBACK` | 点位列表顶部 warn 徽标 + 结果/预览警告 |
| `TIME_BACKFILL` | 运行页 info 说明 |
| `TOKEN_SOON_EXPIRED` | 控制台/我的 info 提示 |
| `SPEED_OUT_OF_RANGE` | 配置页 warn |
| `STEP_OUT_OF_RANGE` | 配置页 warn |
| `MULTI_RUN_AREA` | 规则页区域选择 |
| `REAL_DEVICE_FINGERPRINT` | 设置页 info（设备指纹说明） |

---

## 3. 安全与脱敏展示（原 §11）

| 对象 | 界面规则 |
|---|---|
| 密码 | 只在登录表单内存中存在；成功/失败/卸载即清空；不写日志、不做「记住密码」 |
| Token | 由桥接在 `submitLogin` 成功后自动写入 SecureStore；若 `initialize()` / `submitLogin()` 的 profile 携带 token，前端入口立即丢弃；不存储、不渲染；「我的」只显示固定 `••••••••` + 状态；不显示尾号 |
| 极验载荷 | 只在内存中传给 `submitLogin`；不落盘、不进日志、不回显 |
| uid / unid | 不在界面展示；错误详情里也不出现 |
| 手机号 | 如需展示，中间 4 位掩码：`138****0000` |
| 设备 ID | 前 4 + `…` + 后 4；长度不足时更严格；不提供「复制完整值」 |
| 签名 / originalSign | 前 6 + `…` + 后 4；盐与完整 canonical 串永不出现 |
| 对象键 | 保留 bucket 与前两段路径，文件名掩码 |
| 日志 | 由 Python/Kotlin 源头脱敏；前端只渲染，不拼接敏感字段；最多 500 条，不持久化 |
| 导出 | 后端脱敏后生成 JSON；分享前提示「导出文件可能包含运行记录，请妥善保管」 |
| WebView | 主页面只加载本地资源；极验在独立 WebView/Sheet 中，不暴露主桥 |
| 外部资源 | 不用 CDN、外链字体、统计 SDK、崩溃上报 |

- 截屏与最近任务预览不在 v1 控制范围内；免责声明中说明数据保存在本机。
- 任何「复制」动作不针对 token、密码、完整设备 ID。

---

## 4. 功能标注（原 §13）

| 页面 | existing | adapter | new |
|---|---|---|---|
| 登录 | checkGeeUse、geevalidate、submitLogin | LoginClient 分步编排 | 登录 UI、手动极验、安全存储（自动写入） |
| 控制台 | profile、rule | 学期进度 | 控制台 UI、检查清单、进度聚合 |
| 规则与围栏 | rules、fences、checkpoints | 多区域选择、点位错误上浮 | 页面 UI、SVG 地图、风险提示 |
| 跑步配置 | 距离/时长参数、轨迹构造、分桶 | prepare_run、签名摘要、结构化预览 | 配置 UI、配速校验、图表、演练 |
| 运行中 | 协议步骤、progress 字符串 | 结构化事件、阶段合并、取消 | 运行 UI、日志控制台、常亮、离开确认 |
| 跑步结果 | RunningSubmissionResult、get_one_record | 签名摘要、OBS 状态、结构化结果 | 结果 UI、状态推导、trace 导出 |
| 我的 | 版本常量、README 免责声明 | 设备 ID 掩码 | 设置 UI、会话状态、稳定设备 ID、清除会话 |

---

## 5. 缺口、假设与待确认（原 §14）

> 这些是文档写作过程中发现的、需要实现前闭环的问题。GAP-02 会直接影响多区域学校能否跑通。

### 14.1 契约缺口（需补齐桥接）

| ID | 缺口 | 影响 | 建议 |
|---|---|---|---|
| GAP-02 | `getCheckpoints/previewRun/executeRun` 没有 `runAreaId` 参数 | 多跑步区域时无法真正执行用户选择 | 三个方法增加可选 `runAreaId`；`getCheckpoints` 返回 `areaId` 供回填；未补齐前只显示错误，不静默选第一个 |
| GAP-03 | `speedTop/speedBottom` 单位未在协议中标注 | 配速区间对比可能误判 | 按 m/s 解析；不可解析时只展示原值（假设 A-02） |
| GAP-04 | `initialize` 的 profile 可能含 token，但不返回 `savedAt` | 「我的」无法显示保存时间/尾号 | 入口剥离 token；显示固定掩码 + 状态；不编造尾号/时间 |
| GAP-05 | `getSemesterProgress` 响应结构未经 CLI 验证 | 学期卡可能空白或字段缺失 | 缺字段显示 `—`；全部缺失显示「暂无法获取」，不影响其他区块 |
| GAP-06 | 结果页状态机没有 `cancelled`，但运行页有 | 取消后结果页无状态可映射 | UI 增加 `cancelled` Hero（本文档 §5.7） |
| GAP-07 | 区域选择依赖 `rule.raw.runAreaModels`，未在 AI_SPEC 数据模型中正式暴露 | 选择器可能拿不到选项 | 在 `RunRule.raw` 保留原始字段；或桥接返回 `runAreas[]` |
| GAP-08 | 极验 WebView 与主桥隔离方式未定 | 安全边界不清晰 | 独立 WebView/路由；只回传 4 个字段；不注入 `Protocol` 桥 |

### 14.2 设计假设

| ID | 假设 | 验证方式 |
|---|---|---|
| A-01 | 学期进度按 mode=1 获取，且 0 目标时显示「—」 | 真机账号 |
| A-02 | 规则速度单位按 m/s 解析；不可解析只展示原值 | 真实学校规则 |
| A-03 | 有效时段格式为 `HH:MM~HH:MM`，本地时间比较仅作提醒 | 真实规则 |
| A-04 | 分桶为 10 秒；速度 = distance/10 m/s；步频 = stepsNum×6 步/分 | 协议源码已确认分桶 10s |
| A-05 | 结果页三列指标在 360 宽可容纳 2 位整数 + 2 位小数 | 360/412 截图 |
| A-06 | 运行页 5 行阶段 + 日志 + 取消按钮在 800 高内同屏 | 360×800 截图 |
| A-07 | 系统字体放大 200% 时允许指标降为 2 列 | 系统字体测试 |
| A-08 | 极验在 WebView 内可用；若被阻断，v1 无法登录（不提供手动 Token 导入），需后续替代方案 | 真机验证 |

### 14.3 需要你确认的产品问题

1. 最终应用显示名与包名是否就用 `校园跑控制台` / `com.local.campusrunconsole`？
2. 深色任务控制台作为唯一主题是否确认？v1 是否完全不做浅色？
3. Token 自动写入已确认；手动 Token 导入页从 v1 移除。
4. 真实提交验证使用哪个测试账号？
5. 目标学校是单跑步区域还是多区域？如果是多区域，GAP-02 必须在开发前补齐。
6. 结果页是否需要展示「有效距离/有效时长/消耗」三项？当前按有 recheck 数据时展示设计。
7. 运行中是否允许切到后台继续等待？当前设计为「无前台服务、提示保持前台、切后台只记录警告」。
8. 是否需要在「我的」提供浅色/深色切换？当前不提供。

---

## 6. 与旧文档的对照（原 §15）

| 本文档 | `UI_SPEC.json` 对应 |
|---|---|
| §1 设计定位 | `principles` |
| §2 信息架构 | `informationArchitecture` |
| §3 设计系统 | `designTokens`、`iconography` |
| §4 通用组件 | `components[]` |
| §5 页面详设 | `screens[]` |
| §6 关键流程 | `flows[]` |
| §7 状态与错误 | `errorHandling` |
| §8 文案规范 | `copyDeck` |
| §9 响应式 | `responsive` |
| §10 无障碍 | `accessibility` |
| §11 安全展示 | `securityDisplay` |
| §12 UI 验收 | `acceptanceChecklist` |
| §13 功能标注 | `traceability` |
| §14 缺口与假设 | `gaps`、`assumptions`、`openQuestions` |

维护规则：

- 页面行为变更先改 `UI_SPEC.json`，再同步本文件的线框与文案，避免两份漂移。
- 任何新增字段必须同时标注来源（existing/adapter/new）和脱敏要求。
- GAP 关闭后在本表标记「已关闭 + 版本号」，不删除历史。
- 文案表是唯一来源；实现不得自行改写按钮/错误文案。

---

> 维护提示：本文件在静态稿阶段不维护。需要修改实现契约时，先确认页面逻辑是否已稳定，再同时更新本文件、`AI_SPEC.json` 与桥接代码；不要把静态稿阶段的临时简化写进这里。
>
> 注：上文「页面行为变更先改 `UI_SPEC.json`」是旧文档的维护规则，已被新的页面逻辑文档取代；当前以 `UI设计说明.md` §6 的维护规则为准。
