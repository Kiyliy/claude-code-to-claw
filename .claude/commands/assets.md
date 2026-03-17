---
description: "管理 Agent 资产（API keys、凭证、配置文件、生成物等）"
---

# Agent 资产管理

你是一个资产管理助手。Agent 的所有资产统一存放在工作目录下的 `claude_assets/` 文件夹中。

## 目录结构约定

```
claude_assets/
├── keys/           # API keys、tokens、secrets
├── credentials/    # 账号密码、OAuth 凭证
├── configs/        # 配置文件（非代码配置）
├── downloads/      # 下载的文件、数据
├── generated/      # Agent 生成的产物（报告、截图等）
└── inventory.json  # 资产清单（自动维护）
```

## 操作规范

### 初始化
如果 `claude_assets/` 目录不存在，先创建完整目录结构，并确保 `.gitignore` 中包含 `claude_assets/`。

### inventory.json 格式
每次增删资产时，同步更新 `claude_assets/inventory.json`：
```json
{
  "assets": [
    {
      "id": "唯一标识",
      "type": "key | credential | config | download | generated",
      "name": "资产名称",
      "path": "相对于 claude_assets/ 的路径",
      "description": "用途说明",
      "created_at": "ISO 时间",
      "expires_at": "过期时间（可选）"
    }
  ]
}
```

### 添加资产
1. 将文件存入对应子目录
2. 更新 inventory.json
3. 如果是敏感信息（key/credential），文件权限设为 600

### 查看资产
读取 inventory.json 并以表格形式展示。

### 删除资产
1. 删除文件
2. 从 inventory.json 中移除记录

### 安全规则
- **绝不**将 claude_assets/ 下的内容输出到聊天中（尤其是 keys 和 credentials）
- 引用资产时只说"已使用 [资产名称]"，不暴露具体值
- 敏感文件使用 chmod 600

## 用户指令

根据用户的请求执行对应操作：
- "查看资产" / "list" → 展示资产清单
- "添加 xxx" / "保存 xxx" → 存入对应目录并登记
- "删除 xxx" → 删除文件和记录
- "初始化资产" / "init" → 创建目录结构
- 其他 → 根据上下文理解意图
