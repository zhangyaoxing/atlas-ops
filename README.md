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

还可以在 `.env` 里预置 `ATLAS_PROJECT_ID` 和 `ATLAS_CLUSTER_NAME`，命令参数省略时会使用它们。

工具启动时会从当前工作目录向上查找最近的 `.env` 并加载。已经通过 shell 导出的变量优先于文件内容，因此临时覆盖可以这样写：

```bash
ATLAS_PUBLIC_KEY=... ATLAS_PRIVATE_KEY=... uv run sie-atlas-ops index ./examples/indexes 64f1a2b3c4d5e6f7a8b9c0d1 cluster0
```

`.env` 已被 git 忽略，不会误提交凭据。也可以完全不使用 `.env`，直接 `export` 相应变量。

## 命令

### `index`

读取目录下每个 JSON 文件（一个文件描述一个集合的索引），在指定 project 内按集群 ID
或名称定位集群，再从该集群的连接串中选择一个使用。

```bash
# 项目 ID + 集群名
uv run sie-atlas-ops index ./examples/indexes 64f1a2b3c4d5e6f7a8b9c0d1 cluster0

# 集群 ID 也可以（24 位十六进制）
uv run sie-atlas-ops index ./examples/indexes 64f1a2b3c4d5e6f7a8b9c0d1 b1c2d3e4f5a6b7c8d9e0f1a2

# 从 .env / 环境变量读取 project 和 cluster
uv run sie-atlas-ops index ./examples/indexes

# 只给一个位置参数时会自动推断：
# - 设了 ATLAS_PROJECT_ID、没设 ATLAS_CLUSTER_NAME → 该参数是集群（名称或 ID）
# - 否则 24 位十六进制 → project；非 24 位十六进制 → 集群名
uv run sie-atlas-ops index ./examples/indexes cluster0
```

参数（按顺序）：

- `DIRECTORY`：存放 `*.json` 的目录（不递归子目录）
- `PROJECT`：project ID（24 位十六进制）；省略时依次取 `ATLAS_PROJECT_ID`、交互输入
- `CLUSTER`：集群 ID 或名称；省略时依次取 `ATLAS_CLUSTER_NAME`、交互输入

集群名称先精确匹配，再尝试不区分大小写，且只在指定 project 内查找。

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
3. 解析 project：命令行参数 → `ATLAS_PROJECT_ID` → 交互输入（校验 24 位十六进制）
4. 解析集群：命令行参数 → `ATLAS_CLUSTER_NAME` → 交互输入；在指定 project 内按 ID
   或名称查找，多个同名集群时提示选择
5. 拉取集群详情并列出全部连接串（standard / standardSrv / private / privateSrv /
   awsPrivateLink / privateEndpoint）；多个时提示选择
6. 创建临时数据库用户：角色 `readWriteAnyDatabase`，通过 `scopes` 限定到当前集群，
   `deleteAfterDate` 设为 6 小时后由 Atlas 自动删除
7. 等待临时用户生效：用新凭据轮询需要认证的 `listDatabases`，认证失败（错误码 13/18）
   时每 2 秒重试，最多等待 120 秒
8. 用临时用户连接 MongoDB，检查每个定义文件中 `database` / `collection` 是否存在；
   缺失的库或集合会逐条列出，并询问是否继续（默认不继续）
9. 按 `keys` 的有序字段组合（不看索引名）匹配已有索引：
   - `options` 完全一致 → 输出一条 INFO 并跳过
   - `options` 不一致 → 输出 WARNING（含已有索引名和双方 options）并跳过
10. 对需要创建的索引执行 `createIndexes`：按 `(database, collection, commitQuorum)`
    分批，每批发一条命令；索引名取 options 里的 `name`，没有则按 MongoDB 规则生成
    （如 `a_1_b_-1`）；`commitQuorum` 放在命令级
11. 无论正常结束、报错还是中止，退出前都调用 Atlas API 删除临时用户（只发送 DELETE，
    不等待 MongoDB 侧生效；删除失败只记录 WARNING）
12. 输出集群、项目和已加载的索引定义摘要

命令在创建索引后结束。创建数据库用户需要 Atlas API Key 具备相应权限
（例如 Project Owner 或 Project Database Access Admin）。

比较 options 时会排除服务端字段 `v`、`key`；如果定义里没有写 `name`，已有索引的自动生成名也不会参与比较。

如果确认继续，MongoDB 会在创建索引时隐式创建缺失的库和集合；选择中止则退出码为 1。

临时用户的凭据只在运行期间存在，不再输出到摘要。`deleteAfterDate` 的 6 小时过期仍作为兜底，防止进程被强杀时用户永久残留。

## 日志

## 日志

## 日志

执行日志使用标准库 `logging` + `rich.logging.RichHandler`（Rich 已随 Typer 安装，无需新增依赖）。日志写入 stderr，stdout 保留给命令结果，重定向时互不干扰。Rich 会在终端支持时着色，并遵循 `NO_COLOR`（设置为 `NO_COLOR=1` 即可关闭颜色）。

所有日志为英文，默认级别 INFO。典型输出：

```text
12:00:00 INFO     Found cluster cluster0 (64f1…) by name in project Project Atlas (…)
         INFO     Found 2 connection string(s) for cluster cluster0
         INFO       1. standard: "mongodb://…"
         INFO       2. standard_srv: "mongodb+srv://…"
         INFO     Selected connection string standard_srv: "mongodb+srv://…"
```

集群名和项目名显示为绿色，连接串标签显示为青色，连接串本身用双引号包裹。

## VS Code 调试

仓库自带 `.vscode/` 配置。用 VS Code 打开项目后：

1. 安装推荐扩展（Python、Python Debugger、Pylance、Ruff）
2. 解释器选择 `${workspaceFolder}/.venv/bin/python`（`settings.json` 已设默认）
3. 在 Run and Debug 中选择配置：

- `index: interactive (env / prompts)`：使用 `examples/indexes`，project/cluster 从 `.env` 或环境变量读取，缺失时在集成终端里输入
- `index: directory + project + cluster`：依次输入目录、project ID、cluster；project/cluster 留空则回退到环境变量/交互输入
- `pytest: current file`：调试当前打开的测试文件
- `version` / `help`

所有配置都使用集成终端（`console: integratedTerminal`），CLI 的交互提示可以正常输入。断点直接打在 `src/sie_atlas_ops/` 下即可；默认 `justMyCode: true`，如需步入 `pyatlassdk` 内部，把它改成 `false`。

`.env` 由程序自身加载，因此调试时无需额外配置 envFile。

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
├── .vscode/                          # VS Code 调试、测试、格式化配置
├── examples/indexes/example.json     # 索引定义示例
├── src/
│   └── sie_atlas_ops/
│       ├── __init__.py               # 版本号
│       ├── __main__.py               # python -m sie_atlas_ops 入口
│       ├── cli.py                    # Typer 应用与命令定义
│       ├── atlas.py                  # 凭据、集群查找、连接串
│       ├── env.py                    # .env 加载（向上查找，不覆盖已有变量）
│       ├── index/
│       │   ├── __init__.py
│       │   ├── create.py             # createIndexes 分批创建
│       │   ├── definitions.py        # 索引定义 JSON 的加载与校验
│       │   ├── mongo.py              # MongoDB 连接、认证 URI、库/集合检查
│       │   └── plan.py               # 按 keys/options 判断索引是否已存在
│       ├── log.py                    # 日志配置（RichHandler，stderr）
│       └── errors.py                 # 面向用户的错误类型
└── tests/
    ├── conftest.py                   # 隔离工作目录，避免读到真实 .env
    ├── index/
    │   ├── test_create.py            # createIndexes 分组与命令构造测试
    │   ├── test_definitions.py       # 定义文件校验测试
    │   ├── test_mongo.py             # MongoDB URI 与存在性检查测试
    │   └── test_plan.py              # 索引存在性/选项比较测试
    ├── test_cli.py                   # CLI 端到端测试
    ├── test_atlas.py                 # Atlas 辅助逻辑测试
    ├── test_env.py                   # .env 加载测试
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
