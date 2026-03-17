# 研究课题：Claude Code stream-json 模式下的消息中断机制

## 背景

我们在做一个项目（claude-code-to-claw），通过 `claude -p --input-format stream-json --output-format stream-json` 启动 Claude Code 子进程，实现 Telegram/飞书 Bot 与 Claude Code 的桥接。

## 当前问题

在 CLI 交互模式下，用户在 Claude 忙碌时输入新消息，Claude Code 会：
1. 将新消息包装成 `<system-reminder>The user sent a new message while you were working:\n{内容}\nIMPORTANT: After completing your current task, you MUST address the user's message above.</system-reminder>`
2. 在工具调用间隙注入到当前 turn 的上下文中
3. Claude 在同一个 turn 里看到并处理，只产生一次回复

但在 stream-json 模式下，我们往 stdin 写入新消息时：
- 消息不会被注入到当前 turn
- 而是被当成下一个 turn 的输入排队
- 导致每条消息都触发一个独立的 turn 和回复

## 已发现的源码线索

通过 strings 分析 Claude Code 二进制（Node.js SEA），发现：

### 内部消息系统
- `hasPendingMessage` / `pendingMessages` / `processPendingMessage` — 内部有消息队列
- `hasInterruptibleToolInProgress` — 判断当前工具是否可打断
- `getToolInterruptBehavior` — 获取工具的打断策略（`"block"` 或 `"cancel"`）

### system-reminder 注入
- 函数 `S58(H, $)` 根据消息来源类型生成不同的 system-reminder：
  - `"human"` → `The user sent a new message while you were working: {内容}`
  - `"coordinator"` → `The coordinator sent a message...`
  - `"channel"` → `A message arrived from {server}...`
  - `"task-notification"` → `A background agent completed a task...`

### Bridge 模式
- 存在 `CLAUDE_CODE_ENVIRONMENT_KIND: "bridge"` 环境变量
- 有 `bridge_status` 和 `remote-control` 相关代码
- 启动命令中包含 `--replay-user-messages` 参数

## 需要研究的问题

1. **stream-json 模式下有没有办法触发 interrupt？**
   - 往 stdin 写消息时，Claude Code 是直接排队还是也有 interrupt 路径？
   - 有没有特殊的消息格式可以触发 interrupt（比如 `{"type": "interrupt", ...}`）？

2. **`--replay-user-messages` 参数是什么？**
   - bridge 模式启动时带了这个参数
   - 是否意味着 stream-json 下有特殊的消息重放机制？

3. **`CLAUDE_CODE_ENVIRONMENT_KIND: "bridge"` 模式**
   - bridge 模式和普通 -p 模式有什么区别？
   - bridge 模式下是否有不同的 stdin 处理逻辑？

4. **Claude Code 是开源的吗？**
   - 有没有官方的 SDK 或 API 文档描述 stream-json 的完整协议？
   - 有没有第三方逆向文档？

5. **有没有其他接入方式？**
   - 除了 stream-json，还有没有其他编程接口可以实现 mid-turn 消息注入？
   - Claude Code 的 Agent SDK（如果有的话）是怎么处理这个的？

## 当前的 workaround

我们自己维护了一个 pending 消息队列：
- Claude 忙时，新消息入队
- turn 结束后，合并 pending 消息发给 Claude
- 缺点：会触发新的 turn，产生额外的回复

## 研究结论

### ~~stream-json 模式下 interrupt 不可用~~ → 可用！

**更正**：深入分析 cli.js 源码后发现 stream-json 模式**完全支持 interrupt 和 pending message**。
- **GitHub Issue #29224**: 社区请求 `QueuedMessage` hook 事件，尚未实现
- **`--replay-user-messages`**: 用于 file checkpointing（回显 user message UUID），跟 interrupt 无关
- **bridge 模式**: 是 Remote Control（claude.ai 远程控制本地），通过 WebSocket，不适用于我们

### Python Agent SDK 有 interrupt() 方法（推荐方案）

```python
from claude_agent_sdk import ClaudeSDKClient

async with ClaudeSDKClient(options=options) as client:
    await client.query("搜索今日新闻")
    # 用户发了新消息，Claude 还在忙...
    await client.interrupt()  # 优雅中断，保留 session 上下文
    await client.query("算了，帮我看部署状态")  # 同一 session 继续
```

- 优雅中断当前 turn，不丢上下文
- 中断后可以立即发新消息
- SDK 底层也是 stream-json，但封装了 interrupt 逻辑
- TypeScript V2 SDK 尚不支持 interrupt（Issue #120）

### 方案对比

| 方案 | mid-turn interrupt | 复杂度 | 推荐 |
|------|-------------------|--------|------|
| stream-json + pending 队列 (当前) | ❌ 模拟 | 低 | workaround |
| Python Agent SDK | ✅ `interrupt()` | 中 | ⭐ 推荐 |
| 直接用 Anthropic API | ✅ 完全自控 | 高 | 过度 |

### 源码分析：stream-json 下的 interrupt 机制（cli.js 深度分析）

#### 1. control_request interrupt

往 stdin 写以下 JSON 可以立刻中断当前 turn：
```json
{"type":"control_request","request_id":"unique-uuid","request":{"subtype":"interrupt"}}
```

源码路径：`structuredInput` 循环 → 检测 `control_request` → `subtype === "interrupt"` → `W.abort()` 中断 AbortController

#### 2. user message priority 字段

stdin 的 user message 支持 `priority` 字段：
```json
{"type":"user","message":{"role":"user","content":"新消息"},"priority":"now"}
```

- `"now"` (priority 0) — 立刻触发 abort，中断当前 turn
- `"next"` (priority 1, 默认) — 当前 tool 执行完后在下一轮 API 调用前注入
- `"later"` (priority 2) — 所有 tool 完成后处理

#### 3. pending 消息处理

在 `gzz` 函数（内部 query 循环）中，tool 执行完成后：
```js
let a = $.startsWith("repl_main_thread") || $ === "sdk"
  ? _Z1(z6 ? "later" : "next")  // 从 Hz 队列取 pending 消息
  : [];
```

`$ === "sdk"` 就是 `-p` 模式。所以 **-p 模式本身就支持 pending 消息注入**。

取出的消息通过 `Xf6` 函数转换为 attachment，附加到下一轮 API 调用的上下文中（作为 system-reminder）。

#### 4. 架构关键组件

- `Ve6` 类：stdin IO handler，读 NDJSON，解析分发
- `Hz` 数组：全局消息队列，支持优先级
- `HW()` / `zZ1()`：入队/出队函数
- `eG6()`：队列变化订阅，`priority:"now"` 的消息触发 abort
- `Da6` 类：工具执行器，每个工具有 `interruptBehavior()`（`"block"` 或 `"cancel"`）
- `gzz` 函数：内部 query 循环，检查 pending 消息

### 下一步

1. **修改 bridge 的消息格式**：给 stdin 写的 user message 加上 `priority` 字段
2. **普通消息用 `"next"`**：在工具间隙注入，不打断当前操作
3. **紧急消息用 `"now"`**：立刻中断（比如用户发了 /reset）
4. **不需要自己做 pending 队列**：Claude Code 内部的 Hz 队列就是 pending 队列
