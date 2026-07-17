# 模型仓库管理 repo.py

## 1. 本讲目标

在前两讲里，我们看完了 `common.py` 的「配置 + 上下文 + 输出」和「子进程执行」两套基础设施。本讲走进第一个**真正面向用户功能**的模块——`repo.py`，它实现的是 `openllm repo` 这一组命令。OpenLLM 不自己造模型，它把「有哪些模型可用」这件事外包给了 **git 仓库**：一个模型仓库就是一个 git 仓库，里面按目录约定摆放着一个个 Bento（可运行模型的打包）。本讲要回答三个问题：

- 一个仓库 URL（如 `https://github.com/bentoml/openllm-models@main`）是怎么被拆解成 `server/owner/repo/branch`，又怎么映射到本地缓存目录的？
- `repo add/remove/update/list/default` 这五条命令各自改了什么、没改什么？
- 「缓存 3 天过期」「别名补全」「git 不可用时回退 dulwich」这些机制是怎么协作的？

读完本讲你应当能够：

- 看懂 `parse_repo_url` 用两条正则把 HTTP/SSH 两种 git URL 统一拆解，并生成 `REPO_DIR/server/owner/repo/branch` 四级本地路径。
- 理解 `Config.repos`（一个 `name → url` 的字典）是仓库的「登记表」，`config.json` 是它的落盘形态；`add`/`remove` 只动登记表，`update` 才真正克隆。
- 掌握 `ensure_repo_updated` 的「从未更新则硬退出 / 过期则软提醒」非对称策略，以及 `cmd_update` 末尾用 `glob('*/*/*/*')` 清理孤儿缓存的设计。
- 明白 `_complete_alias` 为什么要在每次更新后「物化」别名：它把 `bento.yaml` 里的 `aliases` 标签写成普通文件，供下一讲 `list_bento` 的 glob 扫描解析。

本讲承接 u2-l1 的 `Config`/`load_config`/`save_config`/`RepoInfo`/`REPO_DIR`/`ContextVar`，以及 u1-l3 提到的 `OpenLLMTyper` 子命令注册，并为 u2-l4（`model.py` 的模型发现）提供「仓库从哪来」的前置依赖。

## 2. 前置知识

进入源码前，先建立四点直觉。

**第一，为什么用 git 仓库当「模型目录」？** 模型会不断新增、版本会迭代。如果把「支持哪些模型」写死在 OpenLLM 代码里，每加一个模型就要发一个新版本。OpenLLM 的做法是把这份目录放到一个**独立的 git 仓库**（默认是 `bentoml/openllm-models`），OpenLLM 只负责「拉取这个仓库、扫描里面的 Bento」。这样模型增删与 OpenLLM 发版彻底解耦，而且天然复用了 git 的版本（`@main`、`@nightly` 分支）与分发能力。

**第二，URL 里的 `@branch` 是什么？** 这是 OpenLLM 自己约定的写法，不是 git 原生语法。`https://github.com/bentoml/openllm-models@main` 表示「这个仓库的 `main` 分支」。OpenLLM 用正则把 `@main` 这段切出来作为 `branch`，再传给 `git clone -b main`。

**第三，登记表与缓存是两回事。** `config.json` 里只存 `name → url` 的映射（登记表：我订阅了哪些仓库），而真正克隆下来的仓库文件躺在 `REPO_DIR/server/owner/repo/branch/` 下（缓存）。`repo add` 只改登记表，不下载；`repo remove` 只改登记表，不删缓存（缓存的清理由下次 `update` 顺手做）。把这两层分开，是理解所有命令行为的关键。

**第四，什么是 dulwich？** `git clone` 依赖系统里装了 `git` 可执行文件。万一运行环境没装 git（`FileNotFoundError`）或 clone 失败，OpenLLM 会回退到 `dulwich`——一个**纯 Python 实现的 git 客户端**，作为兜底，保证「没有系统 git 也能拉仓库」。这是 `pyproject.toml` 里把 dulwich 列为依赖的原因（见 u1-l1）。

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 |
| --- | --- |
| [src/openllm/repo.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py) | 全部核心代码都在这里：五条命令、`parse_repo_url`、`_clone_repo`、`ensure_repo_updated`、`_complete_alias` |
| [src/openllm/common.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py) | 提供 `RepoInfo` 数据模型、`Config`/`load_config`/`save_config`、`REPO_DIR` 路径常量、`ContextVar`（`VERBOSE_LEVEL`/`INTERACTIVE`）、`output` |
| src/openllm/model.py | `_complete_alias` 的下游消费者：`list_bento` 用 glob 扫描仓库目录、解析别名文件（u2-l4 详解） |
| src/openllm/\_\_main\_\_.py | 第 25 行 `app.add_typer(repo_app, name='repo')` 把本模块的 `app` 挂成 `openllm repo` 子命令组 |

本讲的源码精读聚焦在 `repo.py`，`common.py` 只引用与仓库直接相关的数据模型和工具，`model.py` 仅作为「别名物化后被谁消费」的佐证。

## 4. 核心概念与源码讲解

### 4.1 RepoInfo 与 URL 解析

#### 4.1.1 概念说明

OpenLLM 内部描述一个仓库用 `RepoInfo`（定义在 `common.py`），它是一个 Pydantic 模型，字段含义如下：

| 字段 | 含义 | 示例 |
| --- | --- | --- |
| `name` | 仓库在登记表里的别名（用户自取） | `default` |
| `url` | 规范化后的 git URL（不含 `@branch`、不含 `.git`） | `https://github.com/bentoml/openllm-models` |
| `server` | 托管服务器域名 | `github.com` |
| `owner` | 仓库所有者 | `bentoml` |
| `repo` | 仓库名（去 `.git`） | `openllm-models` |
| `branch` | 分支名 | `main` |
| `path` | 本地缓存绝对路径 | `~/.openllm/repos/github.com/bentoml/openllm-models/main` |

`parse_repo_url` 就是「把一个用户给的 URL 字符串 → 一个填好上述字段的 `RepoInfo`」的函数。它同时承担三件事：**校验** URL 是否合法、**拆解**出各字段、**计算**本地缓存路径。正因为校验和计算都在这里，`cmd_add` 才能复用它来做「添加前先验证 URL」。

#### 4.1.2 核心流程

`parse_repo_url` 用两条正则分别匹配 HTTP(S)/git/ssh 协议与 SSH 协议两种写法：

```text
输入: repo_url 字符串, 可选 repo_name
  ↓
先用 GIT_HTTP_RE 匹配 (schema://server/owner/repo[@branch])
  ├─ 命中 → schema = match.group('schema')
  └─ 未命中 → 再用 GIT_SSH_RE 匹配 (git@server:owner/repo[@branch])
       ├─ 命中 → schema = None
       └─ 未命中 → raise ValueError('Invalid git repo url')
  ↓
提取 server / owner / repo / branch
  ↓
repo 去掉末尾 '.git'；branch 为空则默认 'main'
  ↓
用 schema/server/owner/repo 重新拼出规范化的 url（干净、无 @branch、无 .git）
  ↓
path = REPO_DIR / server / owner / repo / branch   # 四级目录
  ↓
返回 RepoInfo(name=repo_name or repo, ...)
```

关键设计：**本地缓存路径是「URL 各字段」的确定性函数**。只要两个仓库 URL 的 `server/owner/repo/branch` 相同，它们就映射到**同一个本地目录**——这正是 `cmd_update` 后半段能用「目录是否在用」来判断「要不要清理」的前提。

两条正则的命名分组结构一致，只是前缀不同：

- HTTP 类：`schema://server/owner/repo[@branch]`
- SSH 类：`git@server:owner/repo[@branch]`

其中 `(@(?P<branch>.+))?` 是一个**可选**的分支段：有 `@xxx` 就解析出 `branch`，没有则 `branch` 为 `None`（随后兜底成 `'main'`）。

#### 4.1.3 源码精读

先看 `RepoInfo` 这个数据模型本身，注意它的 `tolist()` 会根据 `VERBOSE_LEVEL` 返回不同详细度——这决定了 `openllm repo list` 在不同 verbose 下显示什么：

[src/openllm/common.py:130-153](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L130-L153) —— `RepoInfo`：七个字段 + 分档 `tolist()`。verbose ≤ 0 只给一行 `name (url@branch)`；≤ 10 加上 `path`；≤ 20 再加上 `server/owner/repo`。

```python
class RepoInfo(pydantic.BaseModel):
  name: str
  path: pathlib.Path
  url: str
  server: str
  owner: str
  repo: str
  branch: str
```

再看两条正则。它们是模块级常量，用命名分组 `(?P<name>...)` 提取字段：

[src/openllm/repo.py:210-215](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L210-L215) —— `GIT_HTTP_RE` 与 `GIT_SSH_RE`，分别处理 `schema://...` 与 `git@...:...` 两种写法。

```python
GIT_HTTP_RE = re.compile(
  r'(?P<schema>git|ssh|http|https):\/\/(?P<server>[\.\w\d\-]+)\/(?P<owner>[\w\d\-]+)\/(?P<repo>[\w\d\-\_\.]+)(@(?P<branch>.+))?(\/)?$'
)
GIT_SSH_RE = re.compile(
  r'git@(?P<server>[\.\w\d-]+):(?P<owner>[\w\d\-]+)\/(?P<repo>[\w\d\-\_\.]+)(@(?P<branch>.+))?(\/)?$'
)
```

几个阅读细节：

- `repo` 分组是 `[\w\d\-\_\.]+`，**允许包含点**，所以能匹配到 `openllm-models.git` 里的 `.git`，再由后面代码切掉。
- `(@(?P<branch>.+))?` 整段可选：括号包住 `@` 和分支名，因此 `branch` 命中时不带 `@`。
- HTTP 版多了 `(?P<schema>...)`，SSH 版没有 schema（写死 `git@`），这是后面「`schema is not None` 走 HTTP 拼接、否则走 SSH 拼接」分支的依据。

接着看 `parse_repo_url` 主体，分三段读：

[src/openllm/repo.py:233-240](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L233-L240) —— 先试 HTTP 正则，不命中再试 SSH，都不命中就抛 `ValueError`。

```python
match = GIT_HTTP_RE.match(repo_url)
if match:
  schema = match.group('schema')
else:
  match = GIT_SSH_RE.match(repo_url)
  if not match:
    raise ValueError(f'Invalid git repo url: {repo_url}')
  schema = None
```

[src/openllm/repo.py:245-255](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L245-L255) —— 字段清洗：去掉 `.git` 后缀、branch 兜底 `main`，并用各字段**重新拼接**出规范化 `url`（因此输入里多余的 `@branch`、`.git`、末尾 `/` 都被干净地丢弃）。

```python
server = match.group('server')
owner = match.group('owner')
repo = match.group('repo')
if repo.endswith('.git'):
  repo = repo[:-4]
branch = match.group('branch') or 'main'

if schema is not None:
  repo_url = f'{schema}://{server}/{owner}/{repo}'
else:
  repo_url = f'git@{server}:{owner}/{repo}'
```

[src/openllm/repo.py:257-266](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L257-L266) —— 计算本地路径并组装 `RepoInfo`。`name` 缺省取仓库名 `repo`，调用方可显式传入登记表里的别名覆盖。

```python
path = REPO_DIR / server / owner / repo / branch
return RepoInfo(
  name=repo if repo_name is None else repo_name,
  url=repo_url,
  server=server,
  owner=owner,
  repo=repo,
  branch=branch,
  path=path,
)
```

这正是「URL → 四级目录」映射的落点：`REPO_DIR` 在 `common.py` 里是 `OPENLLM_HOME / 'repos'`（默认 `~/.openllm/repos`），见 [common.py:16-24](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L16-L24)，且在模块导入时就被 `mkdir` 创建（u2-l1 讲过的「导入即建目录」）。

> docstring 里的例子可以印证行为（[repo.py:219-232](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L219-L232)）：`...bentovllm.git@main` 与 `...bentovllm@main` 与 `...bentovllm` 三种写法解析结果相同，分支缺省都落到 `main`。

#### 4.1.4 代码实践

**实践目标**：直接调用 `parse_repo_url`，验证不同写法解析成相同的 `server/owner/repo/branch` 与本地路径。

**操作步骤**：

```python
# 文件名: play_parse_repo.py
from openllm.repo import parse_repo_url
from openllm.common import REPO_DIR

cases = [
    'https://github.com/bentoml/openllm-models@main',
    'https://github.com/bentoml/openllm-models.git@main',
    'https://github.com/bentoml/openllm-models',          # 缺省分支
    'git@github.com:bentoml/openllm-models.git',          # SSH 写法
]

for url in cases:
    info = parse_repo_url(url, 'default')
    print(f'{url}')
    print(f'   → server={info.server} owner={info.owner} repo={info.repo} branch={info.branch}')
    print(f'   → url={info.url}')
    print(f'   → path={info.path}')
    print(f'   → path 相对 REPO_DIR: {info.path.relative_to(REPO_DIR)}')
    print()

# 非法 URL 应抛 ValueError
try:
    parse_repo_url('not-a-git-url')
except ValueError as e:
    print('非法 URL 正确报错:', e)
```

**需要观察的现象**：

1. 前三个 HTTP 写法的 `server/owner/repo/branch` 与 `path` **完全一致**，证明 `.git`、`@main`、缺省分支被归一化。
2. SSH 写法（第 4 个）解析出的字段与前三者也一致（只是内部 `url` 拼成 `git@...` 形式）。
3. `path.relative_to(REPO_DIR)` 都是 `github.com/bentoml/openllm-models/main`——四级目录。
4. 非法 URL 抛出 `ValueError`，这正是 `cmd_add` 用来校验输入的异常。

**预期结果**：四条合法 URL 的 `path` 全部指向同一个 `…/github.com/bentoml/openllm-models/main`；非法输入被 `ValueError` 拒绝。

#### 4.1.5 小练习与答案

**练习 1**：如果用户执行 `openllm repo add foo https://github.com/a/b@main` 和 `openllm repo add bar https://github.com/a/b.git`（同一仓库、不同分支/写法、不同别名），它们会克隆到同一个本地目录吗？

**参考答案**：不会。第一个 `branch=main`，第二个 `branch=main` 但别名不同——本地路径由 `server/owner/repo/branch` 决定，这两条**恰好**都是 `github.com/a/b/main`，所以会指向同一目录（同一份缓存被两个别名共享）。但若分支不同（如 `@main` 与 `@nightly`），则路径不同、各自独立缓存。关键在于路径只看 URL 字段，与别名 `name` 无关。

**练习 2**：为什么 `parse_repo_url` 把「校验」也扛下来了，而不是单独写一个 `is_valid_url`？

**参考答案**：因为 `parse_repo_url` 既要拆字段又要算路径，过程中必然得跑一遍正则；正则不命中本身就是「非法」的唯一判据。让它在「不合法时抛 `ValueError`」、`cmd_add` 直接 `try/except ValueError`，是把「解析」和「校验」合二为一的简洁做法，避免维护两套判断逻辑。

---

### 4.2 仓库增删改查与克隆

#### 4.2.1 概念说明

`openllm repo` 一共有五条命令：`list`、`add`、`remove`、`update`、`default`。它们都注册在一个 `OpenLLMTyper` 实例上，再由 `__main__.py` 挂成子命令组：

[src/openllm/repo.py:21](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L21) —— `app = OpenLLMTyper(help='manage repos')`，本模块所有 `@app.command` 都挂在它上面。

[src/openllm/\_\_main\_\_.py:25](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L25) —— `app.add_typer(repo_app, name='repo')`，挂成 `openllm repo ...`（u1-l3 讲过的子命令注册）。

五条命令的职责一句话总结：

| 命令 | 动登记表？ | 动缓存？ | 说明 |
| --- | --- | --- | --- |
| `list` | 否 | 否 | 打印所有仓库（默认调用 `list_repo()`） |
| `add <name> <url>` | 是（增/覆盖） | 否 | 校验 URL + 写入 `config.repos[name]` |
| `remove <name>` | 是（删） | 否 | 从 `config.repos` 删除（缓存留给下次 update 清） |
| `update` | 否 | 是（重克隆全部） | 删旧缓存 → 重新克隆 → 清孤儿 → 刷新别名 |
| `default` | 否 | 否 | 打印并返回 `default_repo` 对应的本地路径 |

注意一个贯穿全局的设计：**`add`/`remove` 只动登记表，`update` 才动缓存**。这意味着刚 `add` 的仓库在下一次 `update`（或被 `list_bento` 触发的 `ensure_repo_updated`）之前，本地其实还没有文件。

#### 4.2.2 核心流程

`cmd_add` 的流程：

```text
cmd_add(name, repo):
  1. name = name.lower()；若不是合法标识符(isidentifier) → 报错返回
  2. parse_repo_url(repo) 校验 URL；失败 → 报错返回
  3. load_config()；若 name 已存在 → 交互确认是否覆盖
  4. config.repos[name] = repo
  5. save_config(config)   # 落盘 config.json
```

`cmd_remove` 的流程更简单：

```text
cmd_remove(name):
  1. load_config()；若 name 不存在 → 报错返回
  2. del config.repos[name]
  3. save_config(config)
```

`cmd_update` 是最重的一条，它要保证「登记表里的仓库」与「本地缓存」一致：

```text
cmd_update():
  repos_in_use = set()
  for repo in list_repo():
      repos_in_use.add((server, owner, repo, branch))   # 记下「在用」路径
      rmtree(repo.path, ignore_errors=True)             # 删旧缓存
      repo.path.parent.mkdir(parents=True, exist_ok=True)
      try: _clone_repo(repo)                            # 重新克隆
      except: rmtree(repo.path); 报错
  for c in REPO_DIR.glob('*/*/*/*'):                    # 四级深度的所有目录
      if c 对应的 (server,owner,repo,branch) 不在 repos_in_use:
          rmtree(c)                                     # 清孤儿缓存
  写当前时间到 REPO_DIR/last_update
  for repo in list_repo(): _complete_alias(repo.name)   # 物化别名
```

#### 4.2.3 源码精读

先看 `cmd_add`，注意它把校验、覆盖确认、落盘串在一起：

[src/openllm/repo.py:82-110](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L82-L110) —— `cmd_add`：小写化名称、`isidentifier()` 校验、`parse_repo_url` 校验 URL、已存在时 `questionary.confirm` 询问覆盖、写入并 `save_config`。

```python
name = name.lower()
if not name.isidentifier():
  output(f'Invalid repo name: {name}, ...', style='red')
  return

try:
  parse_repo_url(repo)
except ValueError:
  output(f'Invalid repo url: {repo}', style='red')
  return

config = load_config()
if name in config.repos:
  override = questionary.confirm(
    f'Repo {name} already exists({config.repos[name]}), override?'
  ).ask()
  if not override:
    return

config.repos[name] = repo
save_config(config)
```

两个要点：

- `name.isidentifier()` 限制别名只能含字母/数字/下划线且不以数字开头——这与「别名要能安全地拼进命令行、路径、模型 tag」有关。
- 这里**只写 `config.repos[name] = repo`**，完全没有克隆动作。所以 `repo add` 之后立刻 `repo list` 能看到它，但本地 `REPO_DIR` 下还没有对应目录。

再看 `cmd_remove` 与 `default`，二者都很短：

[src/openllm/repo.py:31-42](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L31-L42) —— `cmd_remove`：从字典删 key 后 `save_config`，**不碰缓存目录**。

[src/openllm/repo.py:113-118](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L113-L118) —— `default`：解析 `default_repo` 对应的 URL，打印并返回其本地 `path`。

```python
@app.command(name='default', help='get default repo path')
def default() -> typing.Optional[pathlib.Path]:
  if TEST_REPO:
    return None
  output((info := parse_repo_url(load_config().repos['default'], 'default')).path)
  return info.path
```

> 这里的海象运算符 `(info := ...)` 在 `output` 的参数里完成「赋值 + 打印」，随后 `return info.path` 复用同一个变量，是 Python 3.8+ 的简洁写法。

接着看真正干重活的 `cmd_update`，分段读。先是「逐仓库删旧克隆新」：

[src/openllm/repo.py:45-69](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L45-L69) —— 遍历 `list_repo()`，记录在用路径、删旧、重新 `_clone_repo`，失败则清掉半成品缓存。

```python
repos_in_use = set()
for repo in list_repo():
  if VERBOSE_LEVEL.get() <= 0:
    output(f'updating repo {repo.name}', style='green')
  repos_in_use.add((repo.server, repo.owner, repo.repo, repo.branch))
  if repo.path.exists():
    shutil.rmtree(repo.path, ignore_errors=True)
  repo.path.parent.mkdir(parents=True, exist_ok=True)
  try:
    _clone_repo(repo)
  except Exception as e:
    shutil.rmtree(repo.path, ignore_errors=True)
    if VERBOSE_LEVEL.get() > 0:
      output(f'Failed to clone repo {repo.name}', style='red')
      output(e)
```

注意 `repos_in_use` 存的是**四级元组**而不是别名 `name`——这与本地路径的生成规则（4.1）严格对应，是后面判断「孤儿缓存」的依据。

然后是「清孤儿 + 记录更新时间 + 刷新别名」：

[src/openllm/repo.py:70-79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L70-L79) —— `glob('*/*/*/*')` 枚举所有四级深度的缓存目录，删掉不在 `repos_in_use` 里的；写 `last_update` 时间戳；最后对每个仓库跑 `_complete_alias`。

```python
for c in REPO_DIR.glob('*/*/*/*'):
  repo_spec = tuple(c.parts[-4:])
  if repo_spec not in repos_in_use:
    shutil.rmtree(c, ignore_errors=True)
    if VERBOSE_LEVEL.get() > 0:
      output(f'Removed unused repo cache {c}')
with open(REPO_DIR / 'last_update', 'w') as f:
  f.write(datetime.datetime.now().isoformat())
for repo in list_repo():
  _complete_alias(repo.name)
```

精妙之处在 `c.parts[-4:]`：用路径组件的「最后四段」还原出 `(server, owner, repo, branch)` 元组，与 `repos_in_use` 比较。这样**即使某个仓库被 `remove` 了（登记表里没了）但缓存还在，也会在这里被清掉**——这就解释了为什么 `cmd_remove` 不必自己删缓存。

最后看 `_clone_repo`，它体现了「git 优先 + dulwich 兜底」的容错策略：

[src/openllm/repo.py:155-174](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L155-L174) —— 优先 `git clone --depth=1 -b branch`，捕获 `CalledProcessError`/`FileNotFoundError` 后回退到 `dulwich.porcelain.clone`。

```python
try:
  if VERBOSE_LEVEL.get() <= 0:
    subprocess.run(
      ['git', 'clone', '--depth=1', '-b', repo.branch, repo.url, str(repo.path)],
      check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
  else:
    subprocess.run(
      ['git', 'clone', '--depth=1', '-b', repo.branch, repo.url, str(repo.path)], check=True
    )
except (subprocess.CalledProcessError, FileNotFoundError):
  import dulwich
  import dulwich.porcelain
  dulwich.porcelain.clone(repo.url, str(repo.path), checkout=True, depth=1, branch=repo.branch)
```

三个要点：

- `--depth=1` 是**浅克隆**：只取最新一次提交，不拉整条历史。模型仓库只需要最新文件，浅克隆大幅省时省流量。
- `-b branch` 直接切到目标分支，与 `parse_repo_url` 解析出的 `branch` 对应。
- `except (CalledProcessError, FileNotFoundError)`：前者是「git 命令执行但失败」（如网络问题、分支不存在），后者是「系统根本没装 git」。两种情况都回退到纯 Python 的 dulwich。注意 dulwich 分支**延迟 import**（`import` 写在 except 内），避免在 git 正常时也加载这个不太小的库。

#### 4.2.4 代码实践

**实践目标**：用 `openllm repo add` 注册一个仓库，对照 `config.json` 观察「只动登记表、不动缓存」，再用 `openllm repo list --verbose` 验证登记生效。

**操作步骤**：

```bash
# 1. 先看一下当前登记表（首次运行可能触发 ensure_repo_updated，按提示选 yes 或先跑 openllm repo update）
openllm repo list

# 2. 记录 add 之前的 config.json
cat ~/.openllm/config.json

# 3. 注册一个公开仓库（用一个真实可访问的公开 git url）
openllm repo add myrepo https://github.com/bentoml/openllm-models@main

# 4. 再看 config.json，确认多了 "myrepo" 这一项
cat ~/.openllm/config.json

# 5. 看 REPO_DIR 下是否立刻出现了 myrepo 对应的克隆目录
ls ~/.openllm/repos/github.com/bentoml/openllm-models/main   # 这个目录可能因 default 已存在

# 6. 列出仓库（verbose 模式能看到 path）
openllm repo list --verbose
```

**需要观察的现象**：

1. 步骤 4 的 `config.json` 比步骤 2 多了一行 `"myrepo": "https://github.com/bentoml/openllm-models@main"`——证明 `add` 只改了登记表。
2. 步骤 5：`add` 本身**不会**新建克隆目录（如果该 URL 此前没被克隆过）。只有在随后执行 `openllm repo update`（或被 `list_bento` 触发）时，目录才会出现。
3. 步骤 6：`repo list --verbose` 把 `default`、`nightly`、`myrepo` 都列出来，`myrepo` 与 `default` 的 `path` 字段相同（因为 URL 字段一致），印证「路径只看 URL 不看别名」。

**预期结果**：`config.json` 中新增 `myrepo` 条目；本地缓存目录在 `add` 后不立即生成，需 `update` 才克隆。

> 待本地验证：步骤 5 是否立即有目录，取决于该 URL 是否已被其他别名（如 `default`）克隆过。若 `default` 与 `myrepo` 指向同一 URL，则共享同一缓存目录。

#### 4.2.5 小练习与答案

**练习 1**：`cmd_remove` 删掉一个仓库后，它的本地缓存目录还在。如果不再次运行 `repo update`，这个孤儿缓存会一直占着磁盘吗？

**参考答案**：会一直占着，直到下一次 `repo update`。`cmd_update` 末尾的 `glob('*/*/*/*')` 扫描会把所有「不在 `repos_in_use`」的四级目录清掉，孤儿缓存在那时才会被回收。所以「彻底清理一个仓库」的标准操作是 `repo remove <name>` 之后再 `repo update`。

**练习 2**：为什么 `_clone_repo` 用 `--depth=1` 浅克隆？如果不用会有什么后果？

**参考答案**：模型仓库只关心当前分支的最新文件，不需要历史提交。浅克隆只取最新一次 commit，能显著减少网络传输和磁盘占用、加快 `repo update`。若用完整克隆，每次 update 都拉整条 git 历史，对上百个模型的目录会很慢且浪费空间。

---

### 4.3 缓存更新策略与别名补全

#### 4.3.1 概念说明

前两模块讲的是「用户主动操作仓库」。但 OpenLLM 还有一个更常见的入口：用户敲 `openllm model list` 或 `openllm serve xxx` 时，系统会**隐式**检查「仓库缓存是否新鲜」，这就是 `ensure_repo_updated` 的职责。它由 `list_bento`（model.py）在扫描模型前调用，保证用户看到的模型列表不会是几个月前的陈旧缓存。

`ensure_repo_updated` 的核心是一份**非对称**策略，依据两个条件：「是否从未更新过」与「是否交互模式」：

| 缓存状态 \ 模式 | 交互（`INTERACTIVE=True`） | 非交互 |
| --- | --- | --- |
| 从未更新（无 `last_update` 文件） | 询问是否更新，同意则跑 `cmd_update` | **红色报错 + `typer.Exit(1)` 硬退出** |
| 过期（超过 3 天） | 询问是否更新 | 黄色提醒，**继续运行**（不退出） |
| 新鲜（3 天内） | 什么都不做 | 什么都不做 |

注意两种「异常态」的严重程度不同：**从未更新**意味着本地根本没有模型列表，后续一切操作都无意义，所以非交互下直接退出；**过期**只是「可能不是最新」，多数情况下旧列表仍可用，所以非交互下只给提醒、放行。这个区分体现了对 CLI 脚本化场景（非交互、需要稳定退出码）与人工使用场景（交互、可询问）的兼顾。

「别名补全」(`_complete_alias`) 是 `cmd_update` 的收尾步骤，它解决的是「模型别名」问题。一个 Bento 在 `bento.yaml` 的 `labels.aliases` 里可以声明别名（如 `llama3.2:1b` 的别名是 `latest`）。但 OpenLLM 的模型发现（`list_bento`）是基于**文件系统 glob** 的（扫 `bentos/*/*`），它不读 `bento.yaml` 里每个文件的别名。为了让 `openllm model get llama3.2:latest` 这种别名引用能生效，`_complete_alias` 在每次更新后把别名「物化」成一个**普通文件**：文件名是别名，内容是它指向的版本号。这样 glob 扫描时遇到这个普通文件，就知道它是个别名、该指向哪个真实版本。

#### 4.3.2 核心流程

`ensure_repo_updated` 的判定流程：

```text
ensure_repo_updated():
  if TEST_REPO: return                      # 测试桩直接放行
  last_update_file = REPO_DIR / 'last_update'
  if not last_update_file.exists():         # 从未更新
      if INTERACTIVE: 询问 → 同意则 cmd_update()
      else: 红色报错 → typer.Exit(1)
      return
  last_update = fromisoformat(last_update_file 内容)
  if now - last_update > UPDATE_INTERVAL(3 天):   # 过期
      if INTERACTIVE: 询问 → 同意则 cmd_update()
      else: 黄色提醒（放行）
  # else: 新鲜，什么都不做
```

`_complete_alias` 的流程：

```text
_complete_alias(repo_name):
  for bento in list_bento(repo_name=repo_name):
      alias = bento.labels['aliases'].strip()        # 如 "latest,stable"
      for a in alias.split(','):
          写文件 bento.path.parent / a，内容为 bento.version
```

效果示意（以 `llama3.2` 的 `1b` 版本，别名 `latest` 为例）：

```text
更新前:
  bentos/llama3.2/1b/bento.yaml        (labels.aliases="latest")
  bentos/llama3.2/1b/...其他文件

_complete_alias 后:
  bentos/llama3.2/1b/bento.yaml
  bentos/llama3.2/latest               ← 新增的普通文件，内容是字符串 "1b"

list_bento('llama3.2') 用 glob 'bentoml/bentos/llama3.2/*' 扫描时:
  - 命中目录 1b (含 bento.yaml) → BentoInfo(tag=llama3.2:1b)
  - 命中文件 latest (普通文件)  → 读出 "1b" → 指向 1b → BentoInfo(alias=latest, tag=llama3.2:latest)
```

下游 `list_bento` 怎么消费这些别名文件，详见 [model.py:156-167](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L156-L167)：目录（含 `bento.yaml`）当真实版本，普通文件当别名（读取内容还原真实路径）。这是下一讲 u2-l4 的核心，本讲只需理解「`_complete_alias` 负责生产别名文件，`list_bento` 负责消费它们」。

#### 4.3.3 源码精读

先看两个模块级常量，它们定义了「过期阈值」和「测试开关」：

[src/openllm/repo.py:17-18](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L17-L18) —— `UPDATE_INTERVAL = timedelta(days=3)`，缓存新鲜期 3 天；`TEST_REPO` 读取环境变量 `OPENLLM_TEST_REPO`，供测试时绕过真实克隆。

```python
UPDATE_INTERVAL = datetime.timedelta(days=3)
TEST_REPO = os.getenv('OPENLLM_TEST_REPO', None)  # for testing
```

`TEST_REPO` 贯穿所有命令：当它非空时，`cmd_list`/`cmd_remove`/`cmd_update`/`cmd_add`/`default`/`ensure_repo_updated` 都会走「短路返回」或返回一个指向 `TEST_REPO` 路径的合成 `RepoInfo`（见 [list_repo 的 TEST_REPO 分支](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L122-L133)），让测试不必真的去克隆 `bentoml/openllm-models`。

接着读 `ensure_repo_updated`，分「从未更新」和「过期」两段：

[src/openllm/repo.py:177-194](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L177-L194) —— 无 `last_update` 文件时：交互则询问、非交互则红色报错并 `typer.Exit(1)`。

```python
def ensure_repo_updated() -> None:
  if TEST_REPO:
    return
  last_update_file = REPO_DIR / 'last_update'
  if not last_update_file.exists():
    if INTERACTIVE.get():
      choice = questionary.confirm(
        'The repo cache is never updated, do you want to update it ...?'
      ).ask()
      if choice:
        cmd_update()
      return
    else:
      output('The repo cache is never updated, please run `openllm repo update` ...', style='red')
      raise typer.Exit(1)
```

[src/openllm/repo.py:195-207](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L195-L207) —— 有 `last_update` 但超过 `UPDATE_INTERVAL` 时：交互则询问、非交互则黄色提醒**但不退出**。

```python
  last_update = datetime.datetime.fromisoformat(last_update_file.read_text().strip())
  if datetime.datetime.now() - last_update > UPDATE_INTERVAL:
    if INTERACTIVE.get():
      choice = questionary.confirm('The repo cache is outdated, do you want to update it ...?').ask()
      if choice:
        cmd_update()
    else:
      output('The repo cache is outdated, please run `openllm repo update` ...', style='yellow')
```

对比这两段就能看清「非对称」设计：从未更新走 `raise typer.Exit(1)`（硬失败），过期只 `output(..., style='yellow')`（软提醒，函数自然返回，调用方继续）。`INTERACTIVE` 这个上下文变量（u2-l1 讲过，默认 `False`）正是 `openllm hello` 在进入交互流程前用 `INTERACTIVE.set(True)` 打开的（见 u1-l4），让交互式引导能弹询问框、而脚本化命令保持静默。

> 这也解释了为什么 `last_update` 文件写在 `REPO_DIR / 'last_update'`（一个目录里的普通文件）而不是 `config.json`：更新时间是「缓存层」的元数据，与「用户登记表」语义不同，分开存放更干净。它由 `cmd_update` 末尾写入（[repo.py:76-77](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L76-L77)）。

最后看 `_complete_alias`，它很简短却打通了 repo 与 model 两个模块：

[src/openllm/repo.py:144-152](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L144-L152) —— 遍历仓库内所有 Bento，把 `labels.aliases` 里每个别名写成「文件名=别名、内容=版本号」的普通文件。

```python
def _complete_alias(repo_name: str) -> None:
  from openllm.model import list_bento

  for bento in list_bento(repo_name=repo_name):
    alias = bento.labels.get('aliases', '').strip()
    if alias:
      for a in alias.split(','):
        with open(bento.path.parent / a, 'w') as f:
          f.write(bento.version)
```

三个要点：

- `from openllm.model import list_bento` **延迟导入**：因为 `model.py` 又会反过来 `from openllm.repo import ...`（见 model.py 顶部），若放在模块顶层会形成循环导入。延迟到函数体内执行就避开了。
- `bento.labels` 来自 `bento.yaml` 的 `labels` 段（u2-l4 详解），`aliases` 是逗号分隔的字符串（如 `"latest,stable"`）。
- 写出的文件路径是 `bento.path.parent / a`，即「模型目录」下、与「版本目录」平级的一个普通文件。它和真实版本目录（如 `1b/`）处在同一层，所以 `list_bento` 的 glob `bentos/llama3.2/*` 能同时命中它和版本目录。

#### 4.3.4 代码实践

**实践目标**：验证 `ensure_repo_updated` 的非对称行为——观察「过期」只提醒不退出，并亲手模拟 `_complete_alias` 的别名物化效果。

**操作步骤**：

```bash
# 实践 A：观察 ensure_repo_updated 的软提醒
# 1. 先确保有过一次 update（生成 last_update 文件）
openllm repo update

# 2. 查看上次更新时间
cat ~/.openllm/repos/last_update

# 3. 把 last_update 文件内容改成 30 天前的日期，人为制造「过期」
#    （把下面的日期改成「今天减 30 天」的一个 ISO 字符串，如 2025-01-01T00:00:00）
echo '2025-01-01T00:00:00.000000' > ~/.openllm/repos/last_update

# 4. 跑一条会触发 ensure_repo_updated 的命令（非交互）
openllm model list 2>&1 | head -5
#    预期：开头出现一行黄色 "The repo cache is outdated, please run `openllm repo update` ..."
#    然后命令继续执行、仍能列出模型（旧缓存可用）

# 5. 再人为制造「从未更新」：删掉 last_update 文件
rm ~/.openllm/repos/last_update

# 6. 再跑（非交互）
openllm model list; echo "退出码: $?"
#    预期：红色 "The repo cache is never updated ..." 然后 typer.Exit(1)，退出码非 0
```

```python
# 实践 B：手工模拟 _complete_alias 的别名物化（不依赖真实仓库）
# 文件名: play_alias.py
import pathlib, tempfile
from openllm.common import RepoInfo

root = pathlib.Path(tempfile.mkdtemp())
# 模拟一个模型目录：版本目录 + bento.yaml
model_dir = root / 'llama3.2' / '1b'
model_dir.mkdir(parents=True)
(model_dir / 'bento.yaml').write_text('dummy')   # 真实情况这里有 labels.aliases

# 模拟 _complete_alias 的写文件动作（aliases 假设为 "latest,stable"）
aliases = 'latest,stable'
for a in aliases.split(','):
    (model_dir.parent / a).write_text('1b')      # 文件名=别名，内容=版本号

# 列出 model 目录同层，观察别名文件
for p in sorted(model_dir.parent.iterdir()):
    if p.is_dir():
        print(f'目录: {p.name}/  (真实版本)')
    else:
        print(f'文件: {p.name}   内容={p.read_text()}  (别名 → {p.read_text()})')
```

**需要观察的现象**：

- 实践 A 步骤 4：过期时**只**有一行黄色提醒，命令随后照常列出模型（退出码 0）。
- 实践 A 步骤 6：从未更新时红色报错并**立即退出**，退出码非 0，不再列出模型。
- 实践 B：`llama3.2/` 目录下既有版本目录 `1b/`，又有两个普通文件 `latest`、`stable`，后者内容都是 `1b`——这正是 `list_bento` glob 扫描时区分「目录=版本」「文件=别名」所依赖的形态。

**预期结果**：过期=软提醒放行；从未更新=硬退出；别名物化后「别名文件内容=版本号」。

> 待本地验证：实践 A 改写 `last_update` 需要对 `~/.openllm/repos` 有写权限；步骤 4/6 的具体提示文案以源码 [repo.py:190-207](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L190-L207) 为准。实践 B 是纯文件操作，可在任意环境运行。

#### 4.3.5 小练习与答案

**练习 1**：为什么「从未更新」在非交互下要硬退出，而「过期」只是软提醒？

**参考答案**：从未更新意味着本地完全没有模型列表，后续 `list_bento` 扫描不到任何 Bento，`serve`/`run` 都无从下手，继续执行只会得到无意义的空结果或误导性错误，不如直接 `typer.Exit(1)` 让用户先 `repo update`。而过期只是「可能不是最新」，本地仍有可用（虽略旧）的模型列表，多数命令能正常工作，硬退出反而打断用户；所以只给黄色提醒、放行。这是按「能否继续提供有效结果」来分级。

**练习 2**：如果删掉 `cmd_update` 末尾对 `_complete_alias` 的调用，`openllm model get llama3.2:latest`（用别名）还能工作吗？

**参考答案**：不能（或不可靠）。别名引用依赖 `list_bento` 在 glob 扫描时找到「别名文件」并读取其内容还原真实版本；而这些别名文件正是 `_complete_alias` 物化出来的。不调用它，仓库里就不会有 `latest` 这样的普通文件，glob 扫不到别名，`llama3.2:latest` 也就解析不到 `llama3.2:1b`。这也说明 `_complete_alias` 必须在**每次 `update` 之后**重跑——因为重新克隆会覆盖整个目录、别名文件随之丢失，需要重新生成。

---

## 5. 综合实践

把本讲三块内容串起来，完成一次「注册自定义仓库 → 验证 URL 解析 → 观察登记表与缓存分离 → 触发更新」的完整链路。本任务也是 u3-l5「自定义仓库与 Bento 实践」的预热。

```bash
# 1. 记录初始状态
echo '--- 初始 config.json ---'
cat ~/.openllm/config.json
echo '--- 初始 REPO_DIR ---'
ls ~/.openllm/repos

# 2. 用 parse_repo_url 预测「add 之后，该仓库的本地路径应该是什么」
python -c "
from openllm.repo import parse_repo_url
info = parse_repo_url('https://github.com/bentoml/openllm-models@main', 'myrepo')
print('预测的本地路径:', info.path)
print('path 四级组件:', info.path.parts[-4:])
"

# 3. 注册一个公开仓库
openllm repo add myrepo https://github.com/bentoml/openllm-models@main

# 4. 确认登记表变化（多了 myrepo），但 REPO_DIR 下并不一定立刻有新克隆
echo '--- add 后 config.json ---'
cat ~/.openllm/config.json

# 5. 列出仓库，verbose 模式对照 path
openllm repo list --verbose

# 6. 主动更新，观察缓存生成与别名物化
openllm repo update --verbose

# 7. 对照步骤 2 的预测路径，确认缓存目录已生成
ls "$(python -c "from openllm.repo import parse_repo_url, list_repo; print(parse_repo_url('https://github.com/bentoml/openllm-models@main','myrepo').path)")"
#    在该仓库的 bentos 目录下挑一个模型，观察是否存在「别名文件」(普通文件)
#    例如找某个模型目录，看同层是否有内容为版本号的普通文件

# 8. 清理：移除刚加的仓库，并 update 让孤儿缓存被回收
openllm repo remove myrepo
openllm repo update --verbose   # 观察是否打印 "Removed unused repo cache ..."
```

**验收点**：

- 步骤 2 预测的路径，与步骤 5 `repo list --verbose` 显示的 `myrepo` 的 `path` **完全一致**，印证 `parse_repo_url` 的「URL → 四级路径」确定性映射。
- 步骤 3/4：`config.json` 新增 `myrepo`，而 `REPO_DIR` 在 `add` 后**不一定**新增目录（登记表与缓存分离）。
- 步骤 6 后：预测路径下确实生成了克隆内容；某个模型目录同层能找到「内容为版本号」的别名普通文件（即 `_complete_alias` 的产物）。
- 步骤 8：`remove` 后再 `update`，由于 `myrepo` 与 `default` 指向同一 URL（共享缓存），是否触发 `Removed unused repo cache` 取决于是否还有其他别名引用该 URL——据此体会「孤儿清理以 URL 元组为粒度，而非别名」。

> 待本地验证：步骤 6/7/8 涉及真实网络克隆，需能访问 GitHub；若网络受限，可设置 `OPENLLM_TEST_REPO` 指向一个本地路径来观察 `list_repo` 的合成分支（[repo.py:122-133](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L122-L133)）。

## 6. 本讲小结

- OpenLLM 用 **git 仓库**作为模型目录：`Config.repos`（`name → url` 字典）是登记表，落盘为 `config.json`；真正克隆的文件在 `REPO_DIR/server/owner/repo/branch/` 四级缓存目录下。
- `parse_repo_url` 用 `GIT_HTTP_RE`/`GIT_SSH_RE` 两条正则统一拆解 HTTP 与 SSH 两种 URL，清洗掉 `.git`/`@branch`/末尾 `/`，并计算出确定性的本地路径；URL 不合法时抛 `ValueError`，`cmd_add` 借此做输入校验。
- 五条命令分工明确：`add`/`remove` **只动登记表**（`save_config`），`update` **才动缓存**（删旧→`_clone_repo`→清孤儿→写 `last_update`→`_complete_alias`），`list`/`default` 只读。
- `_clone_repo` 优先 `git clone --depth=1 -b branch` 浅克隆，失败或系统无 git 时回退纯 Python 的 `dulwich`，保证环境鲁棒。
- `ensure_repo_updated` 采用非对称策略：「从未更新」非交互下硬退出（`typer.Exit(1)`），「超过 3 天过期」只黄色提醒放行；它由 `list_bento` 隐式调用，是模型发现的 freshness 闸门。
- `_complete_alias` 在每次 update 后把 `bento.yaml` 的 `aliases` 标签物化成「文件名=别名、内容=版本号」的普通文件，供 `list_bento` 的 glob 扫描解析别名——这是 repo.py 与下一讲 model.py 的接缝。

## 7. 下一步学习建议

- **接着读 u2-l4（model.py：模型发现与 Bento 解析）**：那里会详讲 `list_bento` 如何用 `bentoml/bentos/*/*` 这个 glob 模式扫描本讲产出的缓存目录、如何区分「版本目录」与「别名文件」、以及 `BentoInfo` 如何从 `bento.yaml` 派生各属性。本讲的 `_complete_alias` 正是为它铺路。
- **回顾 u2-l1（common.py 配置层）**：本讲频繁用到 `Config`/`load_config`/`save_config`/`RepoInfo`/`REPO_DIR`/`ContextVar`，若对「栈式 ContextVar」「config.json 三态降级」印象模糊，可回去对照。
- **延伸到 u2-l6（venv.py）与 u3-l1（local.py）**：本讲的 `RepoInfo.path`（仓库缓存路径）是 `list_bento` 找到 `BentoInfo.path` 的前提，而后者又是 serve 时 `cwd` 的来源；这条 `RepoInfo.path → BentoInfo.path → serve cwd` 的链路会在 u3-l1 闭环。
- **进阶到 u3-l5（自定义仓库与 Bento）**：本讲综合实践已预热「自定义仓库注册」，u3-l5 会完整讲如何按 `bentos` 目录约定搭建自己的模型仓库、用 BentoML 构建 Bento 并提交，让 `openllm` 发现自有模型。
- 想了解 dulwich 的细节，可阅读其文档中 `porcelain.clone` 一节，对照本讲 `_clone_repo` 的回退分支理解「纯 Python git 客户端」的用法与限制。
