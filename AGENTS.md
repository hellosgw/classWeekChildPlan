# AGENTS.md — classWeekChildPlan

家园共育周计划生成器。上传幼儿园周活动计划 PDF，DeepSeek API 自动生成家庭同步计划，前端渲染为精美卡片并支持一键保存。

## Build / Test / Lint

```bash
# 安装依赖
pip install -r backend/requirements.txt

# 启动开发服务器（.env 中配置 DEEPSEEK_API_KEY）
cd backend && uvicorn app:app --reload --host 0.0.0.0 --port 8000

# 生产启动
cd backend && uvicorn app:app --host 0.0.0.0 --port 8000
```

未配置自动化测试 / lint 工具。

## Architecture

```
用户上传 PDF → FastAPI 解析文本 → DeepSeek API 生成计划 → 前端渲染卡片
```

| 层 | 技术 | 说明 |
|---|---|---|
| 前端 | 单页 HTML + vanilla JS | marked.js 渲染 Markdown，html2canvas 截图保存 |
| 后端 | Python FastAPI | 两个端点：`/` (静态前端) + `/api/generate-plan` (核心) |
| AI | DeepSeek API (OpenAI 兼容) | System prompt 注入 `prompt.py`，模型默认 `deepseek-chat` |
| PDF | PyPDF2 | 提取文本，不支持扫描件 OCR |

### 关键路由

- `GET /` — 返回 `frontend/index.html`
- `GET /api/health` — 健康检查
- `POST /api/generate-plan` — 接收 PDF 文件，返回 `{"plan": "markdown..."}`

### 数据流

1. 前端上传 PDF → FormData POST 到 `/api/generate-plan`
2. 后端 PyPDF2 提取纯文本
3. 拼接 `SYSTEM_PROMPT` + PDF 文本 → 调用 DeepSeek chat completions
4. 返回 Markdown → 前端 marked.js 解析 + CSS 卡片渲染
5. 用户点击保存 → html2canvas 截图 → `a.download` 触发下载（移动端额外新窗口长按保存）

## Key Files & Directories

```
backend/
  app.py            FastAPI 应用入口，路由 + PDF 解析 + DeepSeek 调用
  prompt.py         SYSTEM_PROMPT 常量（幼儿教育专家 AI 的系统提示词）
  requirements.txt  Python 依赖
frontend/
  index.html        完整前端：上传区、加载态、卡片渲染、保存按钮（内联 CSS/JS）
.env.example        API Key 配置模板，复制为 .env 后填入真实 key
```

## Coding Conventions

- **Python**: 类型注解（`list[str]`、`dict`），函数前 `_` 前缀表示内部辅助函数，英文注释配合中文用户提示
- **前端**: vanilla JS 无框架，CSS 变量做主题色（`--warm-orange` 等），移动端响应式 `@media (max-width: 480px)`
- **外部 CDN**: marked.js + html2canvas，无构建工具链
- **错误处理**: 后端返回 HTTP 4xx/5xx + JSON `detail`，前端 `catch` 后 `showError()` 渲染

## Git Workflow

_无提交历史。_ 建议 convention: `feat:` / `fix:` / `chore:` 前缀。

## CI/CD

未配置。

## Tips for AI Agents

- **API Key**: 必须设置环境变量 `DEEPSEEK_API_KEY`，否则 `/api/generate-plan` 返回 500。参考 `.env.example`。
- **PDF 限制**: PyPDF2 只提取可选中文字。扫描件 / 图片型 PDF 会返回空内容错误"PDF 内容为空"。
- **System Prompt**: 在 `backend/prompt.py` 中。修改 Prompt 模板时注意保持"Output Format"部分的格式约定，前端 `renderPlan()` 依赖 `18:30` 和 `20:00` 字符串做时间标签注入。
- **前端 CDN**: 离线环境需将 marked.js 和 html2canvas 替换为本地文件。
- **内存限制**: PDF 上限 10 MB，`MAX_PDF_BYTES` 在 `app.py` 顶部可调。
- **模型切换**: 通过 `DEEPSEEK_MODEL` 环境变量换模型（如 `deepseek-reasoner`）。注意 System prompt 较长，确保模型支持足够的 context。
