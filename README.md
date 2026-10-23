# sie-atlas-ops

SIE Atlas 运维命令行工具（CLI）。

## 环境要求

- [uv](https://docs.astral.sh/uv/)
- Python 3.10（由 uv 按 `.python-version` 自动选用）

## 快速开始

```bash
# 创建 .venv 并安装运行 + 开发依赖
uv sync

# 运行 CLI
uv run sie-atlas-ops --help
```

## 配置

Atlas API 凭据放在 `.env` 中（Atlas Organization Access Manager 中的 API Key）：

```bash
cp .env.example .env
# 编辑 .env，填入 ATLAS_PUBLIC_KEY / ATLAS_PRIVATE_KEY
```

工具启动时会从当前工作目录向上查找最近的 `.env` 并加载。已经通过 shell 导出的变量优先于文件内容，因此临时覆盖可以这样写：

```bash
ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=... uv run sie-atlas-ops index ./examples/indexes cluster0
```

`.env` 已被 git 忽略，不会误提交凭据。也可以完全不使用 `.env`，直接 `export ATLAS_PUBLIC_KEY=...` / `ATLAS_PRIVATE_KEY=...`。

## 命令

### `index`

读取目录下每个 JSON 文件（一个文件描述一个集合的索引），通过 Atlas API 按集群 ID 或
名称定位集群，再从该集群的连接串中选择一个使用。

```bash
uv run sie-atlas-ops index ./examples/indexes cluster0

# 24 位十六进制会被当作集群 ID，否则按名称查找
uv run sie-atlas-ops index ./examples/indexes 64f1a2b3c4d5e6f7a8b9c0d1
```

参数：

- `DIRECTORY`：存放 `*.json` 的目录（不递归子目录）
- `CLUSTER`：集群 ID（24 位十六进制）或集群名称；名称先精确匹配，再尝试不区分大小写

JSON 格式：

```json
{
  "database": "exampleDB",
  "collection": "example",
  "indexes": [
    {
      "keys": { "field": 1 },
      "options": { "name": "field_index" },
      "commitQuorum": 1
    }
  ]
}
```

规则：

- `database`、`collection`、`indexes` 必填；`indexes` 不能为空数组
- 每个索引的 `keys` 必填；`options`、`commitQuorum` 可选
- 未知字段会被拒绝，便于尽早发现拼写错误
- 多个文件的校验错误会一次性全部报告

执行流程：

1. 读取并校验目录下所有 JSON 文件
2. 加载最近的 `.env`，再读取 Atlas API 凭据（shell 变量优先）
3. 遍历可访问的项目查找集群；多个同名集群时提示选择
4. 拉取集群详情并列出全部连接串（standard / standardSrv / private / privateSrv /
   awsPrivateLink / privateEndpoint）；多个时提示选择
5. 输出集群、项目和已加载的索引定义摘要

索引创建本身尚未实现，命令在选定连接串后结束。

## 日志

执行日志使用标准库 `logging` + `rich.logging.RichHandler`（Rich 已随 Typer 安装，无需新增依赖）。日志写入 stderr，stdout 保留给命令结果，重定向时互不干扰。Rich 会在终端支持时着色，并遵循 `NO_COLOR`（设置为 `NO_COLOR=1` 即可关闭颜色）。

所有日志为英文，默认级别 INFO。典型输出：

```text
12:00:00 INFO     Found cluster cluster0 (64f1…) by name in project Project Atlas (…)
         INFO     Found 2 connection string(s) for cluster cluster0
         INFO       1. standard: mongodb://…
         INFO       2. standard_srv: mongodb+srv://…
         INFO     Selected connection string standard_srv: mongodb+srv://…
```

集群名显示为绿色，连接串标签显示为青色。

## 开发

```bash
uv run pytest        # 跑测试
uv run ruff check .  # 代码检查
uv run ruff format . # 格式化
```

首次手动激活虚拟环境的写法：

```bash
source .venv/bin/activate
```

## 项目结构

```
.
├── pyproject.toml                    # 项目元数据、依赖、工具配置
├── .python-version                   # 固定 Python 3.10
├── .env.example                      # 环境变量模板（复制为 .env）
├── examples/indexes/example.json     # 索引定义示例
├── src/
│   └── sie_atlas_ops/
│       ├── __init__.py               # 版本号
│       ├── __main__.py               # python -m sie_atlas_ops 入口
│       ├── cli.py                    # Typer 应用与命令定义
│       ├── atlas.py                  # 凭据、集群查找、连接串
│       ├── env.py                    # .env 加载（向上查找，不覆盖已有变量）
│       ├── index_definitions.py      # 索引定义 JSON 的加载与校验
│       ├── log.py                    # 日志配置（RichHandler，stderr）
│       └── errors.py                 # 面向用户的错误类型
└── tests/
    ├── conftest.py                   # 隔离工作目录，避免读到真实 .env
    ├── test_cli.py                   # CLI 端到端测试
    ├── test_atlas.py                 # Atlas 辅助逻辑测试
    ├── test_env.py                   # .env 加载测试
    ├── test_index_definitions.py     # 定义文件校验测试
    └── test_log.py                   # 颜色/日志配置测试
```

## 依赖说明

`pyatlassdk` 通过 `[tool.uv.sources]` 以 editable 方式指向本地 checkout：

```toml
pyatlassdk = { path = "../mongo-atlas-sdk/pyatlassdk", editable = true }
```

仓库布局不同时改这一行即可。SDK 发布到 PyPI 后可以换成版本号。

## 添加新命令

在 `src/sie_atlas_ops/cli.py` 中新增：

```python
@app.command()
def hello(name: str = typer.Argument(..., help="Name to greet.")) -> None:
    """Say hello."""
    typer.echo(f"Hello, {name}!")
```

命令分组（子应用）：

```python
users_app = typer.Typer(help="Manage users.", no_args_is_help=True)
app.add_typer(users_app, name="users")


@users_app.command("list")
def list_users() -> None: ...
```

新增入口后即可通过 `uv run sie-atlas-ops hello World` 调用；在本地已安装（editable）的情况下无需重新安装。
