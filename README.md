# 本地 API 测试台

OpenAI 兼容 API 测试工具。API 测试和主界面只使用 Python 标准库 `urllib` + `tkinter`。
获取模型 + 测连通性 + 流式首字延迟 + 批量测速 + Embedding + 视觉识图 + 绘图 + 鹈鹕绘制对比 + 结果导出。
鹈鹕预览是主界面「鹈鹕测试」标签内的单一原生 WebView2 区域，与主界面使用同一套深色、青绿色样式；并发测试只复用这一个区域，不为每个模型创建窗口。渲染器作为固定子控件跟随标签页尺寸变化。其 `pywebview` 依赖安装在本项目的 `.preview-venv`，不修改全局 Python 环境。

## 文件

| 文件 | 作用 |
| --- | --- |
| `api_tester.py` | 主程序：GUI + CLI 双模 |
| `mock_server.py` | 本地 mock 接口，用来给自己做自测（key=`sk-test`，端口默认 18080） |
| `pelican/benchmark.py` | 统一鹈鹕题目、独立结果存储、沙箱预览服务 |
| `pelican/preview.py` / `pelican/preview.html` | 主界面内嵌预览控件的记录、缩略图、预览与错误详情 |
| `pelican/runtime.py` / `pelican/requirements.txt` | 原生预览的独立依赖安装 |
| `pelican/results/` | 每次生成的提示词、模型原始回复、HTML 和记录；运行后自动创建 |
| `pelican/examples/` | 原目录已有的独立 HTML 作品；不参与模型请求 |
| `pelican/diagnostics/` | 预览日志与本地验证截图 |
| `run.bat` | cmd / 双击启动（纯 ASCII + CRLF，见下文坑位说明） |
| `run.ps1` | PowerShell 启动（UTF-8 BOM + CRLF，PS 5.1 与 7 通用） |

## 启动

```bat
:: cmd 或双击
run.bat

:: PowerShell
.\run.ps1

:: 直接跑 Python
python api_tester.py --gui
```

两个启动器都支持**参数透传**，例如：

```bat
run.bat --base-url http://127.0.0.1:18080/v1 --api-key sk-test --all
.\run.ps1 --base-url http://127.0.0.1:18080/v1 --api-key sk-test --stream --test-model mock-small
```

无参数 = 打开 GUI。找不到 Python 时会给中文/英文明确提示而不是一闪而过。

本目录已安装原生预览依赖。迁移到其他电脑时，第一次打开预览会自动安装到 `.preview-venv`；也可提前执行 `python -m pelican.runtime`。Windows 需要 Microsoft Edge WebView2 Runtime（当前电脑已安装）。复制项目到新路径后应重新创建 `.preview-venv`。

## 跨 shell 兼容的坑（已规避）

写 Windows 启动脚本时踩过三个坑，记下来免得再犯：

1. **`.bat` 不能带 LF 换行**。cmd.exe 的解析器只认 CRLF，纯 LF 会让行首字符被吃掉，报出 `'ho' 不是内部或外部命令`（`echo` 被劈成 `ho`）、`'rrorlevel'`（`errorlevel` 被劈成 `rrorlevel`）这类看似见鬼的错误。
2. **`.bat` 里不要写中文**。`chcp 65001` 只改控制台**输出**代码页，改不了**解析**用的代码页。UTF-8 中文字节落在 GBK 前导字节区间，会吞掉后面的 `)` 和换行，导致 `if (` 多行块整个塌方，报出 `'锡洤渭??'` 这种乱码"命令"。所以 `run.bat` 是纯 ASCII，中文提示交给 Python 打印。
3. **`.ps1` 建议加 UTF-8 BOM**。Windows PowerShell 5.1 对无 BOM 的脚本按本地 ANSI 代码页（简中是 GBK）解码，中文注释同样会吞换行；加了 BOM 后 PS 5.1 和 PS 7 都能正确识别。

`run.bat` 额外做了：优选 `python` → `python3` → `py -3` 三级兜底；用 `python -c` 探活，挡住 Microsoft Store 的假 `python.exe` 桩；校验主版本是 3；非零退出码会打印并 `pause`，方便双击时看到错误。

## CLI 用法

```bat
:: 获取模型列表
python api_tester.py --base-url https://api.deepseek.com/v1 --api-key sk-xxx --limit 50

:: 测单个模型连通性
python api_tester.py --base-url https://api.deepseek.com/v1 --api-key sk-xxx ^
  --skip-models --test-model deepseek-chat

:: 流式测首字延迟（TTFB）
python api_tester.py --base-url ... --api-key sk-xxx --skip-models --test-model deepseek-chat --stream

:: 拉全部模型并发测速
python api_tester.py --base-url ... --api-key sk-xxx --all --concurrency 8

:: 走代理 + 跳过证书校验（自签中转站常用）
python api_tester.py --base-url ... --api-key sk-xxx --proxy http://127.0.0.1:7890 --insecure
```

参数速查：

| 参数 | 说明 |
| --- | --- |
| `--base-url` | 如 `https://api.deepseek.com/v1`，不带路径会自动补 `/v1` |
| `--api-key` | Bearer Key |
| `--timeout` | 超时秒，默认 30 |
| `--proxy` | HTTP 代理，如 `http://127.0.0.1:7890` |
| `--insecure` | 跳过 TLS 证书校验 |
| `--test-model` | 指定模型，多个用逗号分隔 |
| `--all` | 测 `/models` 返回的全部模型 |
| `--concurrency` | 并发线程数，默认 8 |
| `--limit` | 打印模型数量上限，默认 50 |
| `--stream` | 用 SSE 流式模式测连通性 |
| `--image` | 对话请求附带一张内置 1×1 PNG，用来测视觉模型 |
| `--image-gen` | 改走 `POST /images/generations`，用来测绘图模型 |
| `--skip-models` | 跳过获取模型，直接测 `--test-model` |
| `--prompt` / `--max-tokens` | 测试提示词与 token 上限 |
| `--profile` | 客户端伪装：`default` / `claude-code` / `codex` / `pi` |
| `--protocol` | 对话协议，默认跟随 profile：`openai` / `anthropic` / `responses` |
| `--auth-mode` | 鉴权头：`auto`（anthropic 用 `x-api-key`，其余 Bearer）/ `bearer` / `x-api-key` / `both` |
| `--client-version` | 覆盖 UA 中的客户端版本号 |
| `--ua` | 直接指定 User-Agent |
| `--header 'K: V'` | 追加/覆盖请求头，可重复，优先级最高 |
| `--show-headers` | 打印实际发出的请求头（key 已隐藏） |

## 客户端伪装

让请求带上主流 CLI 客户端的请求头**并走对应协议**，用于测试中转站是否按 UA / `originator` 做白名单、分流或限速。

| profile | 协议 / 路径 | 关键请求头 |
| --- | --- | --- |
| `default` | Chat Completions `/chat/completions` | `User-Agent: dsh-local-api-tester/1.0` |
| `claude-code` | Anthropic Messages `/messages?beta=true` | `User-Agent: claude-cli/<ver> (external, cli)`、`x-app: cli`、`X-Claude-Code-Session-Id`、`anthropic-version: 2023-06-01`、`anthropic-beta`、`X-Stainless-*`（SDK 0.128.0） |
| `codex` | Responses `/responses` | `originator: codex_cli_rs`、`User-Agent: codex_cli_rs/<ver> (<OS> <ver>; <arch>) unknown`、`session_id` |
| `pi` | Chat Completions `/chat/completions` | `User-Agent: pi (<platform> <release>; <arch>)`、`X-Stainless-*`（openai SDK 7.19.0） |

```bat
python api_tester.py --base-url https://relay.example.com/v1 --api-key sk-xxx ^
  --profile claude-code --skip-models --test-model claude-sonnet-4-5 --stream --show-headers
```

- 优先级：基础头 < profile < `--ua` < `--header`。profile 永远不会碰鉴权头；只有显式 `--header` 才能覆盖。
- 字面量来源（本机已装客户端）：Claude Code 2.1.288 `claude.exe` 中的 `ZO()` / `kw()` 与 SDK 头构造；Codex 0.160.0 `codex.exe` 中 `login/src/auth/default_client.rs` 的 UA 模板；pi 1.0.3 的 `dist` 源码。客户端升级后用 `--client-version` 覆盖版本号即可。
- 每次启动生成新的 session UUID。
- 获取模型、Embedding 不分协议，始终走 `/models`、`/embeddings`，只是带上伪装头。

已知局限（只伪装头 + 协议，不复刻完整客户端指纹）：

- 请求体是最小报文，不带客户端固定的 system 提示词、工具列表、`metadata.user_id`；检查请求体特征的中转站仍能识别。
- `anthropic-beta` 取的是二进制中确认存在的两个值，实际客户端会按登录方式 / 模型动态组合。
- Codex UA 末尾的终端段固定为 `unknown`（真实值随终端变化）；`x-codex-*` 等会话元数据头未发送。
- Python `urllib` 会把头名规范成首字母大写（`session_id` → `Session_Id`），并自带 `Connection: close`、`Accept-Encoding: identity`。HTTP 头名不区分大小写，但做严格指纹的服务能区分。
- 视觉测试图是内置的 1×1 PNG，只验证「请求里带得上图、接口认这个结构」。个别视觉模型会拒收过小的图，那是模型限制，不是连通性失败。
- 仅用于测试你自己有权测试的服务。

## GUI 用法

1. 左上「连接」卡片：填 `Base URL` 和 `API Key`。右侧下拉框是预设地址，**只有真正选中某一项才会填入**；手输的地址会自动归到「自定义中转站」，再点开下拉框不会把地址覆盖回默认值。
2. 在左侧模型列表上方填写 **手动模型 ID**，点「添加模型」或回车即可添加并选中，不会开始测试；支持多个 ID 用中英文逗号、分号或空白分隔并去重。也可点 **获取模型**（`F5`）合并接口返回的模型，不覆盖手动添加项。可用「筛选」按关键字过滤。鼠标移到模型 ID 上显示 I 形光标，按住拖动选中文字，按 `Ctrl+C` 或 `Ctrl+Insert` 复制选中的部分；右键「复制此模型 ID」仍可复制完整 ID。列表由连通性、批量测速、Embedding、鹈鹕测试共用。
3. 选中模型后双击，或点 **测试连通性**（`Ctrl+Enter`）。勾「流式 SSE」可以看首字延迟和逐字输出。
4. 右上「客户端伪装」卡片：选 profile / 协议 / 鉴权方式 / 版本 / UA；「自定义头」每行一个 `Key: Value`，`#` 开头为注释。每次请求前，日志会打印实际发出的请求头。
5. **批量测速**：并发测试选中的模型；不选则测试当前筛选结果，最多 40 个。勾了「附带测试图」时批量请求也会带图。
6. **Embedding**：测试向量模型，结果列显示维度。
7. **鹈鹕测试**：使用主界面当前供应商，在左侧选中一个或多个模型，点操作栏「鹈鹕测试」只打开内嵌页面并带入所选模型；确认参数后，点击页面内「开始生成」（多选时显示「生成 N 个模型」）才开始请求。「鹈鹕测试」标签集中显示记录、字符数/状态、缩略图与预览；选中记录后显示作品、失败原因或原始回复。手动模型统一在主列表添加，鹈鹕页直接使用主列表选择。所有批次共享最多 4 个并发请求；供应商与 Key 直接使用主界面配置。
8. **导出结果**：存 CSV / JSON，包含全部模型（不受筛选影响）。

**模型列表保存与删除：** 主列表的「保存模型列表」可选，按 **供应商 + 命名 Key** 分别记住，兼容旧配置默认勾选。同一供应商切换不同 Key 时，只显示该 Key 自己获取或手动添加的模型，避免把 A Key 的模型拿去请求 B Key；获取模型也只写入当前 Key 的列表。勾选时模型及列表测试结果会自动保存，重启后直接使用，无需再次获取；取消勾选立即清除当前 Key 配置中的列表与结果，本次仍可使用，关闭后不恢复。选中一个或多个模型后可点「删除选中模型」、按 Delete 或右键删除；保存开启时删除立即保存。删除列表项保留鹈鹕历史作品，正在生成的作品完成后也不会将已删除的 ID 加回列表。

**供应商保存与命名多 Key：** 填好接口后点「保存供应商」并命名；在「API Key」输入一个 Key 后点「保存/新增 Key」并命名，可重复添加。通过「供应商档案」切换时，URL、协议、伪装、自定义头、代理、命名 Key 列表和上次选中的 Key 一起恢复；每个命名 Key 还有独立的模型列表、测试结果和保存开关。不同供应商可各自拥有同名 Key；「命名 Key」下拉框切换使用的 Key，也支持重命名和删除，重命名会同步迁移该 Key 的模型槽位。保存后立即写入 `settings.json`，重启恢复。修改 URL 会开始一份新供应商草稿，并清空旧 Key 和自定义头；原有供应商档案保留。

**自定义 SVG 提示词：** 展开鹈鹕页中的「提示词」，可选择「固定鹈鹕题目」或「自定义 SVG」。固定题目保持原文；自定义模式原样发送输入的完整提示词，支持 SVG 或含 SVG 的 HTML。模式和自定义内容自动保存，切回固定模式也保留自定义草稿。编辑或切换模式不会开始测试，仍需点「开始生成」。每条记录标记题目模式，并在 `prompt.txt` 和 `run.json` 保存当次实际题目；悬停记录的题目标签可查看提示词。只有自定义模式会使用自定义文本。

**鹈鹕测试隔离：** 每次模型只收到所选题目的一条用户消息，不上传同目录文件、参考 HTML、历史作品或其他模型回复，也不提供文件读取或工具调用。请求走所选文本协议的 Chat / Messages / Responses 接口，独立于 `/images/generations`；「附带测试图」不影响鹈鹕测试。内嵌预览区域在沙箱中渲染 SVG/CSS 动画，禁止生成内容执行脚本、读取文件或请求外部资源。

鹈鹕测试单独使用默认 `32768` 的输出上限和 `180` 秒超时，可在连接区或鹈鹕页顶部调整；同一行可选择推理强度「默认/低/中/高」。输出上限不等于模型总上下文容量。普通测试的超时、max_tokens 和鹈鹕测试的输出上限、推理强度、超时均记住最后一次手动设置，自动保存并在下次打开时恢复；仅没有保存值时使用初始默认值，不根据旧数值强制升级。内嵌页的三项参数修改后会显示保存状态，并同步到主界面，点生成前会确认保存成功。默认不额外发送推理参数；选择后，OpenAI Chat 使用 `reasoning_effort`，Responses 使用 `reasoning.effort`，Anthropic 使用 extended thinking budget。推理模型可能将输出预算用于构思；达到上限但没有最终代码时应提高输出上限或降低推理强度。程序现在识别三种协议的输出上限、中断和未完成事件，不把思考草稿算成完整作品。只有渐变/定义、没有实际图形的 SVG 也会明确报错，不再显示白屏。旧记录会按此规则重新校验，原始回复保持不变。

每次结果保存到 `pelican/results/<独立编号>/`：`prompt.txt`、`response.txt`、`artwork.html`、`run.json`。失败时保留原始回复和具体原因，不使用预制鹈鹕代替。点「查看作品」或切换「鹈鹕测试」标签只浏览记录，不发送模型请求。进入预览时自动收起上方设置区，右上「展开设置」可恢复；切回日志/原始响应也会恢复设置。列表包含不同供应商的历史作品；记录上的 × 将其从列表移除，保留本地原始文件，不取消已发出的请求。`pelican/examples/pelican.html` 是已有手工作品，测试不会读取或上传它。

连接设置与 Key 保存在程序同目录的 `settings.json`。API Key 以明文存储，不要外传。旧版本的全局命名 Key 会兼容恢复；存在上次活动供应商时会迁移到该档案。

右键模型可以测试、复制 ID 或结果（也可以用 `Ctrl+C`）。右侧有两个标签页：**日志**（带时间戳和颜色等级）、**原始响应**（完整 JSON / 流式正文）。底部状态栏显示当前状态和通过/失败计数。在 Windows 上已声明 DPI 感知，高分屏下文字不会发糊。

## 自测（无需真 Key）

```bat
:: 终端 1
python mock_server.py 18080

:: 终端 2
python api_tester.py --base-url http://127.0.0.1:18080/v1 --api-key sk-test --all

:: 伪装测试: mock 终端会打印收到的全部请求头 (--quiet 关闭)
python api_tester.py --base-url http://127.0.0.1:18080/v1 --api-key sk-test --profile codex --test-model mock-small --stream
```

mock 同时实现 `/chat/completions`、`/messages`（校验 `anthropic-version`）、`/responses`、`/images/generations`，`Bearer` 与 `x-api-key` 均可鉴权。带了视觉入参时回复里会多出「看到图片」。鹈鹕题目会返回标明 MOCK 的测试动画，`mock-small` 和 `mock-large` 的车架颜色不同，用于验证记录与预览对应。

`python -m unittest test_api_tester -v` 检查三种协议、请求隔离、流式中断/输出上限、拒绝空 SVG 草稿、旧白屏记录重新判定、主界面提交生成、记录移除，以及供应商/命名 Key 保存切换和重启恢复。`python verify_native_preview.py` 使用真正的主界面验证内嵌 WebView2 子控件、多份作品可显示且不会创建额外顶层窗口。两项验证只使用本机 mock 或临时文件，不读取真实 `settings.json`，不调用真实供应商。

## 判定逻辑

- **通**：HTTP 200 且响应体里能挖出正文：Chat `choices[0].message.content`、Anthropic `content[].text`、Responses `output[].content[].output_text`（兜底 `delta.content` / `text` / `output_text`）。绘图接口则要求 `data[0].url` 或 `b64_json`。
- **失败**：HTTP 4xx/5xx（Key 错、模型不存在、余额不足都会带出原始错误体）、网络拒绝、超时、200 但响应结构对不上。
- **流式**：额外记录 TTFB（首个 SSE 数据帧到达耗时），单位毫秒。






`node pelican/test_preview.cjs` 使用本地模拟检查内嵌页的自动保存、连续修改、主界面同步和无效输入；不调用供应商。

鹈鹕测试记录支持右键菜单：在文件资源管理器中显示、在浏览器中打开作品、查看原始回复、移除记录。成功记录定位 `artwork.html`，其他状态定位 `run.json`；浏览器通过本地预览服务打开作品，继续保留 CSP/sandbox 隔离，关闭测试台后该地址不再提供服务。
