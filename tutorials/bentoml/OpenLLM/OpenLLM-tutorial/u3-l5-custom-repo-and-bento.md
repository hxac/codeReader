# 二次开发：自定义模型仓库与 Bento 实践

## 1. 本讲目标

本讲是专家层最后一篇，也是整个学习手册的收尾。前面你已经分别读懂了「仓库管理」(`repo.py`) 与「模型发现」(`model.py`) 两条链路，本讲要把它们**缝合**起来，回答一个二次开发的核心问题：

> 我想让 `openllm` 能列出、别名解析、并最终运行**我自己**的模型，该怎么做？

学完本讲你应当能够：

1. 说清一个「自定义模型仓库」在磁盘上的确切目录约定（`bentoml/bentos/<name>/<version>/bento.yaml`），并能解释为什么是这个形状。
2. 画出从 `openllm repo add` 注册、到 `openllm repo update` 克隆、到 `_complete_alias` 物化别名、再到 `openllm model list` 扫描发现的完整联动时序。
3. 掌握结合 BentoML 构建 Bento 并提交到自有仓库的端到端流程，并知道 `DEVELOPMENT.md` 里提到的 `make.py` / `recipe.yaml` / `vllm-chat` 到底在哪里。
4. 在没有 GPU 的机器上，至少把自定义模型验证到「被发现」这一阶段。

---

## 2. 前置知识

本讲默认你已经读完 **u2-l3（仓库管理 repo.py）** 和 **u2-l4（模型发现与 Bento 解析 model.py）**。这里用一句话复习两条已建立的认知，不再展开：

- **仓库层（u2-l3）**：OpenLLM 把「可用模型目录」外包给 git 仓库；`Config.repos`（`name→url` 字典，落盘 `config.json`）是登记表，`parse_repo_url` 把 URL 确定性映射到 `REPO_DIR/<server>/<owner>/<repo>/<branch>` 四级缓存目录，`_complete_alias` 在每次 `repo update` 后把 `bento.yaml` 的 `aliases` 物化成普通文件。
- **发现层（u2-l4）**：`list_bento` 通过 glob 扫描每个仓库的 `bentoml/bentos/*/*`，把「含 `bento.yaml` 的真实版本目录」与「文件名即别名、内容即版本号的别名文件」统一成 `BentoInfo`；`ensure_bento` 把用户输入的名字解析为唯一 Bento。

本讲要补的两个新概念：

- **目录约定的「代码真相」**：README 用了「一个 `bentos` 目录」这种宽松说法，但代码里 glob 的模式是 `bentoml/bentos/*/*`，二者差一层 `bentoml/`。本讲以代码为准。
- **工具链的归属**：`DEVELOPMENT.md` 描述了 `make.py` + `recipe.yaml` + `vllm-chat` 模板的构建流程，但这些文件**不在本仓库**，它们属于 `bentoml/openllm-models`（即默认仓库）项目。这是初学者最容易被绊倒的点，本讲会专门澄清。

> 术语速查：**Bento** 是 BentoML 打包的可部署产物（一个目录 + `bento.yaml`）；**bento.yaml** 是 Bento 的元数据清单；**别名（alias）** 是给某个 `<name>:<version>` 起的更好记的名字，如 `llama3.2:latest`。

---

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
| --- | --- |
| `src/openllm/repo.py` | `parse_repo_url`（URL→本地路径）、`cmd_add`（注册仓库）、`cmd_update`+`_complete_alias`（克隆并物化别名）。 |
| `src/openllm/model.py` | `list_bento`（glob 扫描与去重）、`list_model`（`model list` 命令与表格列）、`ensure_bento`（名字→Bento 解析）。 |
| `src/openllm/common.py` | `Config`（默认仓库登记表）、`RepoInfo`（仓库身份）、`BentoInfo`/`BentoMetadata`（Bento 元数据形状）。 |
| `DEVELOPMENT.md` | 官方对「添加模型 / 添加 Bento / 添加仓库」流程的描述，也是本讲澄清 `make.py` 归属的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 自定义仓库目录约定

#### 4.1.1 概念说明

OpenLLM 的「模型仓库」本质上就是**一个 git 仓库**。OpenLLM 自身不存储模型权重，它只做一件事：把这个 git 仓库 clone 到本地，然后扫其中的 Bento 目录，把「有哪些模型、哪些版本」列给你看。

所以「自定义仓库」=「按 OpenLLM 能识别的目录约定，组织一个你自己的公开 git 仓库」。这个约定只有一条核心规则，但它藏在代码里，README 只说了一半：

> 仓库根目录下要有 `bentoml/bentos/<模型名>/<版本>/bento.yaml` 这样的结构。

注意是 **`bentoml/bentos/`**，不是 `bentos/`。这是因为 OpenLLM 把 Bento 目录的存储复用了 BentoML 的 `BENTOML_HOME/bentos` 惯例——`DEVELOPMENT.md` 里那条构建命令 `BENTOML_HOME=$(openllm repo default)/bentoml/bentos` 就是把 BentoML 的本地仓库指到了自定义仓库的 `bentoml/bentos` 子目录，构建产物因此直接落位到 OpenLLM 能扫到的位置。

#### 4.1.2 核心流程

一个最小可用的自定义仓库长这样（**示例目录结构**）：

```
my-openllm-models/                 # 仓库根（一个公开 git 仓库）
└── bentoml/
    └── bentos/
        └── myllm/                 # <模型名> = 目录名
            └── 1b-instruct-fp16/  # <版本> = 目录名
                └── bento.yaml     # 版本目录里必须有 bento.yaml
```

对应的 glob 视角（与代码一一对应）：

| glob 模式 | 匹配到的路径 | 代码中的含义 |
| --- | --- | --- |
| `bentoml/bentos/*/*` | `bentoml/bentos/myllm/1b-instruct-fp16` | 「模型名 + 版本」这一层 |
| 该路径是**目录**且含 `bento.yaml` | 同上 | → 真实版本，`BentoInfo(path=该目录)` |
| 该路径是**文件** | `bentoml/bentos/myllm/latest` | → 别名文件，内容是版本字符串 |

因此 `bento.yaml` 里写的 `name`/`version` 字段其实**不会被 OpenLLM 用来命名**——OpenLLM 直接拿目录名当模型名、拿版本目录名当版本号。这一点很关键，详见 4.2 的源码。

#### 4.1.3 源码精读

目录约定的「代码真相」全部浓缩在 `list_bento` 选择 glob 模式这一小段里：

[src/openllm/model.py:141-147](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L141-L147)：根据用户传没传 tag、tag 里有没有冒号，决定 glob 深度。注意三段都以前缀 `bentoml/bentos/` 开头——这就是「必须多一层 `bentoml/`」的代码出处。

随后 `list_bento` 用「目录 vs 文件」二分判断每条 glob 命中是什么：

[src/openllm/model.py:156-167](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L156-L167)：是目录且含 `bento.yaml` 就是真实 Bento；是文件就把它的内容当版本号、回溯到真实版本目录构造带别名的 `BentoInfo`。

而 `BentoInfo` 的 `name`/`version` 完全派生自路径，与 `bento.yaml` 内字段无关：

[src/openllm/common.py:181-187](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L181-L187)：`name = path.parent.name`（模型名目录）、`version = path.name`（版本目录）。这解释了为什么「目录名才是 OpenLLM 看到的模型名」。

最后看 `bento.yaml` 该长什么样。`BentoInfo` 只从它读取这几个键，所以你的 `bento.yaml` 至少要满足下面的形状（派生自 `BentoMetadata` 类型与各 `cached_property`）：

[src/openllm/common.py:102-108](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L102-L108)：`BentoMetadata` 是 `bento.yaml` 的字段契约——`name/version/labels/envs/services/schema`。

[src/openllm/common.py:189-200](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L189-L200)：`labels`/`envs` 直接取自 `bento_yaml`；`bento_yaml` 由 `(path / 'bento.yaml').read_text()` 懒加载。

据此，一个最小 `bento.yaml`（**示例代码**，非项目原有文件，字段参照 `BentoMetadata` 与 `pretty_yaml`/`pretty_gpu` 的读取路径）：

```yaml
# bentoml/bentos/myllm/1b-instruct-fp16/bento.yaml （示例）
name: myllm                 # OpenLLM 不用它命名，但 BentoML 需要
version: 1b-instruct-fp16   # 同上
labels:
  aliases: "latest"         # _complete_alias 会据此物化别名文件
  platforms: linux          # 默认 'linux'，见 common.py platforms 属性
envs: []                    # 环境变量声明，deploy 时会校验必需项
services:
  - config:
      resources:
        gpu: 1
        gpu_type: nvidia-tesla-l4   # 必须是 ACCELERATOR_SPECS 的 key
schema:
  routes: []                # pretty_yaml 会读它来渲染 API 表
```

> ⚠️ 易错点：`resources.gpu_type` 必须命中 `accelerator_spec.py` 里的 `ACCELERATOR_SPECS` 字典（如 `nvidia-tesla-l4`、`nvidia-a100-80g`），否则 `pretty_gpu` 会因 `KeyError` 静默返回空串（见 [src/openllm/common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241)），`model list` 的「required GPU RAM」列就会是空白。

#### 4.1.4 代码实践

**实践目标**：在不 clone 任何东西的前提下，用纯 Python 验证「目录约定」——亲手造一个迷你仓库目录，让 `list_bento` 把它扫出来。

**操作步骤**：

1. 准备一个临时目录当作「假仓库根」，并按 `bentoml/bentos/<name>/<version>/bento.yaml` 建好结构：

   ```python
   # 示例代码：构造一个迷你仓库目录
   import pathlib, textwrap
   root = pathlib.Path('/tmp/my-fake-repo')
   ver = root / 'bentoml' / 'bentos' / 'myllm' / '1b-instruct-fp16'
   ver.mkdir(parents=True, exist_ok=True)
   (ver / 'bento.yaml').write_text(textwrap.dedent('''
   name: myllm
   version: 1b-instruct-fp16
   labels:
     aliases: ""
     platforms: linux
   envs: []
   services:
     - config:
         resources:
           gpu: 1
           gpu_type: nvidia-tesla-l4
   schema:
     routes: []
   ''').strip())
   ```

2. 用 `OPENLLM_TEST_REPO` 这个隐藏开关把上面目录伪装成唯一的仓库。`repo.py` 的 `list_repo` 在该环境变量存在时会**短路**返回一个 `path` 指向它的 `RepoInfo`（见 [src/openllm/repo.py:18](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L18) 与 [src/openllm/repo.py:121-133](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L121-L133)）：

   ```python
   # 示例代码：用测试开关绕过克隆，直接扫本地目录
   import os
   os.environ['OPENLLM_TEST_REPO'] = str(root)
   from openllm.model import list_bento
   for b in list_bento():
       print(b.tag, '->', b.path)
   ```

**需要观察的现象**：

- `list_bento()` 能返回一个 `BentoInfo`，其 `tag` 为 `myllm:1b-instruct-fp16`，`path` 指向你建的版本目录。
- 如果你把目录改成 `bentos/myllm/...`（少一层 `bentoml/`），`list_bento()` 将返回空列表——直观证明 glob 前缀是 `bentoml/bentos/`。

**预期结果**：成功打印 `myllm:1b-instruct-fp16 -> /tmp/my-fake-repo/bentoml/bentos/myllm/1b-instruct-fp16`。

> 若你本机 `list_bento()` 报「repo cache is never updated」并退出，是因为 `OPENLLM_TEST_REPO` 已让 `ensure_repo_updated` 直接 `return`（见 [src/openllm/repo.py:177-179](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L177-L179)），不会触发该检查；如仍遇异常，记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：把版本目录名从 `1b-instruct-fp16` 改成 `v1`，`bento.yaml` 里的 `version` 字段保持 `1b-instruct-fp16`。`openllm model list` 里显示的 version 列会是什么？为什么？

**参考答案**：显示 `myllm:v1`。因为 `BentoInfo.version = path.name`（版本**目录名**），`bento.yaml` 的 `version` 字段并不参与 OpenLLM 的命名（见 [src/openllm/common.py:185-187](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L185-L187)）。这提醒我们：目录名才是「代码真相」。

**练习 2**：`bento.yaml` 缺少 `services` 键时，`list_bento` 还能扫到这个 Bento 吗？`model list` 又会怎样？

**参考答案**：能扫到。`list_bento` 只检查「目录存在 + 含 `bento.yaml` 文件」（[src/openllm/model.py:157](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L157)），并不解析内容。但 `model list` 渲染「required GPU RAM」列时会触发 `pretty_gpu` 读 `services[0]['config']['resources']`（[src/openllm/common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241)），缺键会抛异常或返回空——也就是说「能发现」不等于「能正常展示/运行」。

---

### 4.2 仓库注册与模型发现联动

#### 4.2.1 概念说明

光有目录约定还不够，自定义仓库必须先被 OpenLLM「认识」，里面的模型才能被发现。这条联动链路由四个动作串起：

1. **注册（`repo add`）**：把 `name→url` 写进 `config.json` 的登记表，**不克隆**。
2. **克隆与物化（`repo update`）**：删旧克隆新，然后调 `_complete_alias` 把别名落成普通文件。
3. **新鲜度闸门（`ensure_repo_updated`）**：`list_bento` 每次执行前隐式调用它，确保缓存不至于太旧。
4. **扫描发现（`list_bento`）**：glob 上面建好的 `bentoml/bentos/*/*`。

理解这条链路的关键是：注册是「轻」的（只动登记表），发现是「重」的（要靠磁盘上的克隆），二者通过 `config.json` + `REPO_DIR` 两层间接耦合。

#### 4.2.2 核心流程

用伪代码描述一次「注册 → 发现」的完整时序：

```
openllm repo add myrepo <url>
  └─ cmd_add: 校验 name/URL → config.repos['myrepo']=url → save_config
                （此时磁盘上还没有克隆！）

openllm model list --repo myrepo
  └─ list_model → list_bento(repo_name='myrepo')
       ├─ ensure_repo_updated()        # 新鲜度闸门：从未更新→非交互下硬退出
       ├─ list_repo('myrepo')          # 从 config 查 url，parse_repo_url 算出本地路径
       └─ for repo in repo_list:
            repo.path.glob('bentoml/bentos/*/*')   # 此时需要磁盘上已有克隆
            ...
```

别名物化是这条链路里最容易被忽略、却决定了「别名能否解析」的一环：

```
openllm repo update
  └─ cmd_update:
       ├─ 对每个仓库: shutil.rmtree(旧) → _clone_repo(新)
       ├─ 清理孤儿缓存 + 写 last_update 时间戳
       └─ for repo in list_repo(): _complete_alias(repo.name)   # 关键收尾！
              └─ 读 bento.yaml 的 labels['aliases']
                 └─ 对每个别名 a: 写文件 bentoml/bentos/<name>/<a>，内容=version
```

也就是说：**别名文件不是你手写的，而是 `_complete_alias` 在 `repo update` 时自动生成的**。如果你只是 `git clone` 了仓库却没跑 `repo update`（或没触发它的别名收尾），`模型名:别名` 这条路径就解析不出来。

#### 4.2.3 源码精读

**① 注册**：`cmd_add` 是「瘦」函数，只做三件事——校验名字、校验 URL、写登记表。

[src/openllm/repo.py:82-110](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L82-L110)：`name = name.lower()` 后必须 `isidentifier()`（只能字母数字下划线）；URL 必须能过 `parse_repo_url`（否则红色报错退出）；已存在则 `questionary.confirm` 问是否覆盖；最后 `config.repos[name] = repo` + `save_config`。

注意它**完全没有克隆动作**——注册是纯登记。

**② URL 解析**：`parse_repo_url` 把仓库 URL 拆成 `server/owner/repo/branch`，并确定性映射到 `REPO_DIR` 四级目录。

[src/openllm/repo.py:210-215](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L210-L215)：`GIT_HTTP_RE` 与 `GIT_SSH_RE` 两条正则分别吃 HTTP(S)/git/ssh 协议与 `git@host:owner/repo` 形式。

[src/openllm/repo.py:257-266](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L257-L266)：`path = REPO_DIR / server / owner / repo / branch`——这就是你的自定义仓库克隆后落在 `~/.openllm/repos/<server>/<owner>/<repo>/<branch>` 的原因，也是 `list_bento` glob 的根。

**③ 新鲜度闸门**：`list_bento` 第一行就调它。

[src/openllm/repo.py:177-194](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L177-L194)：`last_update` 文件不存在（从未更新）时，非交互模式直接 `raise typer.Exit(1)`——这就是你刚 `repo add` 完立刻 `model list` 可能失败的原因。

**④ 别名物化**：`_complete_alias` 是注册→发现联动的「最后一公里」。

[src/openllm/repo.py:144-152](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L144-L152)：对仓库里每个 Bento，读 `labels['aliases']`（逗号分隔），为每个别名 `a` 在版本目录的**同级**写一个文件 `<name>/<a>`，内容是该 Bento 的 `version`。

它只在 `cmd_update` 末尾被批量调用：

[src/openllm/repo.py:78-79](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L78-L79)：`for repo in list_repo(): _complete_alias(repo.name)`。

**⑤ 发现**：`list_bento` 把别名文件「翻译」回带别名的 `BentoInfo`。

[src/openllm/model.py:122-141](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L122-L141)：先 `ensure_repo_updated()`；支持 `repo/tag` 简写（`tag` 里含 `/` 时前半段当 repo 名）；找不到指定 repo 时列出候选并退出。

把别名文件读回来的逻辑在 4.1.3 已贴过（[src/openllm/model.py:159-163](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L159-L163)）：读到文件内容当版本号，拼出 `origin_path = path.parent / origin_name`，构造 `BentoInfo(alias=path.name, repo=repo, path=origin_path)`。于是 `tag` 变成 `<name>:<别名>`（见 [src/openllm/common.py:171-175](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L171-L175)），而 `bentoml_tag` 仍是真实版本 `<name>:<version>`（[src/openllm/common.py:177-179](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L177-L179)）。

#### 4.2.4 代码实践

**实践目标**：亲手走一遍「注册 → 更新 → 发现」联动，观察 `config.json` 与 `REPO_DIR` 的变化，并验证别名物化。

**操作步骤**：

1. 先确认默认仓库结构作参照（它就是你要模仿的模板）：

   ```bash
   openllm repo list
   openllm repo default          # 打印默认仓库克隆后的本地路径
   ```

2. 注册一个公开的自定义仓库（可先用默认仓库的镜像 fork 当替身，因为它结构正确）：

   ```bash
   openllm repo add myrepo https://github.com/bentoml/openllm-models@main
   ```

3. 触发克隆与别名物化：

   ```bash
   openllm repo update --verbose
   ```

4. 从自定义仓库发现模型，并查看某个带别名的模型：

   ```bash
   openllm model list --repo myrepo
   openllm model get <某模型>:<某别名> --repo myrepo --verbose
   ```

**需要观察的现象**：

- 第 2 步后，`~/.openllm/config.json` 的 `repos` 多了 `"myrepo"`，但 `~/.openllm/repos/...` 下还**没有**对应克隆目录（注册不克隆）。
- 第 3 步后，`~/.openllm/repos/github.com/bentoml/openllm-models/main/bentoml/bentos/<某模型>/` 下应出现以别名命名的普通文件（如 `latest`），用 `cat` 可见其内容是版本号——这正是 `_complete_alias` 的产物。
- 第 4 步 `model get <模型>:<别名>` 能解析成功，证明别名联动闭环。

**预期结果**：`model list --repo myrepo` 输出与默认仓库一致的表格；别名可被 `model get` 解析。

> 若第 3 步因网络无法克隆默认仓库，可改为 4.1.4 的 `OPENLLM_TEST_REPO` 本地目录法验证别名物化：手动在 `bento.yaml` 写 `labels.aliases: "latest"`，调一次 `_complete_alias`，再观察 `latest` 文件是否被生成。记为「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `openllm repo add` 之后立刻 `openllm model list --repo <新仓库>` 可能报「The repo cache is never updated」？

**参考答案**：`repo add` 只更新登记表 `config.json`，不克隆（[src/openllm/repo.py:108-109](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L108-L109)）。而 `model list` → `list_bento` → `ensure_repo_updated` 发现 `last_update` 文件不存在，非交互模式下 `raise typer.Exit(1)`（[src/openllm/repo.py:181-194](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L181-L194)）。需要先 `openllm repo update` 产生首次克隆与 `last_update`。

**练习 2**：`_complete_alias` 把别名文件写在「版本目录的同级」。如果两个不同版本都声明了同一个别名 `latest`，最终磁盘上 `latest` 文件指向哪个版本？

**参考答案**：取决于 `list_bento(repo_name)` 的返回顺序与遍历顺序——后写的覆盖先写的（`open(..., 'w')` 全量覆写，[src/openllm/repo.py:151-152](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L151-L152)）。由于 `list_bento` 内部对路径有 `sorted(...)`（[src/openllm/model.py:152-155](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L152-L155)），结果是确定性的，但语义上「同名别名」是仓库作者应避免的歧义设计。

---

### 4.3 Bento 构建与提交流程

#### 4.3.1 概念说明

到目前为止我们都在「消费」别人造好的 Bento 目录。要让自定义仓库真正可用，你得会**生产** Bento。这里必须先澄清一个最容易踩的坑：

> `DEVELOPMENT.md` 里提到的 `make.py`、`recipe.yaml`、`vllm-chat/` 模板，**都不在本仓库**（已核实：本仓库根目录只有 `src/`、`pyproject.toml`、`DEVELOPMENT.md` 等，没有这三个文件）。它们是**默认仓库 `bentoml/openllm-models`** 的构建工具链。

证据见 [DEVELOPMENT.md:59-101](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md#L59-L101)：`recipe.yaml` 描述模型元数据，`make.py` 读取 recipe 并调用 BentoML 生成 Bento，命令是 `BENTOML_HOME=$(openllm repo default)/bentoml/bentos python make.py <model_name>:<model_tag>`。也就是说这套工具把 BentoML 的本地仓库指到了 `openllm-models` 仓库的 `bentoml/bentos` 子目录，生成后直接 commit 进 git。

对你的自定义仓库，有两条路：

- **A. 借用默认仓库的工具链**：fork `bentoml/openllm-models`，改它的 `recipe.yaml`，跑它的 `make.py`，把生成的 `bentoml/bentos/...` 提交到你自己的仓库。省事，但绑定 vLLM-chat 项目模板。
- **B. 用原生 BentoML 直接构建**：按 [BentoML 构建文档](https://docs.bentoml.com/en/latest/guides/build-options.html) 自己写 `bentovllm.yaml`/`service.py`，`bentoml build` 出 Bento，再把产物目录搬进自定义仓库的 `bentoml/bentos/<name>/<version>/`。最灵活，README 也正是推荐这条路（见 [README.md:241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L241)：「prepare your custom models in a `bentos` directory following the guidelines provided by BentoML to build Bentos」）。

无论哪条路，落到 OpenLLM 眼里的产物形状都一样：`bentoml/bentos/<name>/<version>/bento.yaml`。

#### 4.3.2 核心流程

端到端流程（以路径 B 为例）：

```
1. 在自定义仓库根写一个 BentoML 项目（bentovllm.yaml + service.py / 或 bento.yaml）
2. BENTOML_HOME=<你的仓库>/bentoml/bentos  bentoml build
     → 产物落在 <你的仓库>/bentoml/bentos/<name>/<version>/bento.yaml
3. （可选）在 bento.yaml 的 labels.aliases 写别名，留给 _complete_alias 物化
4. git add bentoml/ && git commit && git push
5. openllm repo add <你的名字> <你的 git url>
6. openllm repo update          # 克隆 + _complete_alias
7. openllm model list --repo <你的名字>   # 验证发现
```

关键点：第 2 步用 `BENTOML_HOME` 把 BentoML 的本地仓库重定向到自定义仓库内部，与 `DEVELOPMENT.md` 里 `BENTOML_HOME=$(openllm repo default)/bentoml/bentos` 是同一个套路——OpenLLM 扫描的 `bentoml/bentos/` 与 BentoML 默认存储的 `<BENTOML_HOME>/bentos/` 在这里被故意对齐。

#### 4.3.3 源码精读

`bento.yaml` 必须满足的字段形状由 `BentoMetadata` 约束（4.1.3 已贴 [src/openllm/common.py:102-108](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L102-L108)）。这里补充三个会被下游读取、决定「能否运行 / 能否部署」的细节：

- **资源声明**（决定 `can_run` 与 `pretty_gpu`）：`services[0].config.resources.{gpu, gpu_type}`，`gpu_type` 必须命中 `ACCELERATOR_SPECS`。

  [src/openllm/common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241)：`pretty_gpu` 读 resources；`KeyError` 静默返回空串。

- **平台声明**（决定能否在当前 OS 被勾选为可运行）：`labels.platforms`，默认 `'linux'`。

  [src/openllm/common.py:202-204](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L202-L204)：`platforms = labels.get('platforms', 'linux').split(',')`。

- **环境变量声明**（决定 `deploy` 时的必需变量校验与 `serve` 注入）：`envs` 列表。

  [src/openllm/common.py:193-195](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L193-L195)：`envs = bento_yaml['envs']`。

DEVELOPMENT.md 对 recipe 字段的权威说明（这也是默认仓库生成 bento.yaml 的源头）：

[DEVELOPMENT.md:61-99](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/DEVELOPMENT.md#L61-L99)：`<model_name>:<model_tag>` 作为 key；`service_config.resources`（gpu/gpu_type）会变成 bento.yaml 里的资源声明；`engine_config` 喂给 vLLM；`chat_template` 选对话模板。这些最终都会被 `make.py` 物化进 `bento.yaml`。

> 诚实说明：`make.py` 与 `recipe.yaml` 的具体实现不在本仓库，本讲不臆造其内部逻辑；要复用这条工具链，请到 `bentoml/openllm-models` 仓库查阅。

#### 4.3.4 代码实践

**实践目标**：用最朴素的方式（路径 B）产出一个能被 `list_bento` 发现的 Bento 目录，验证「构建→提交→发现」闭环。无 GPU 环境验证到「发现」即可，不强求真正起服务。

**操作步骤**：

1. 建一个公开 git 仓库（GitHub/GitLab 均可，OpenLLM 仅支持公开仓库，见 [README.md:251](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/README.md#L251)）。
2. 在仓库根按 `bentoml/bentos/<name>/<version>/bento.yaml` 手工放入一个最小 bento.yaml（可参考 4.1.3 的示例，先不求能跑，只求能被发现）。
3. `git commit && git push`。
4. `openllm repo add myrepo <你的 url>`。
5. `openllm repo update`。
6. `openllm model list --repo myrepo`。

**需要观察的现象**：

- 第 6 步表格里出现你的 `<name>:<version>`，「repo」列是 `myrepo`，「required GPU RAM」列取决于你写的 `gpu_type` 是否在 `ACCELERATOR_SPECS` 里。
- 若你写了 `labels.aliases`，第 5 步后会在版本目录同级看到别名文件，`model get <name>:<别名> --repo myrepo` 能解析。

**预期结果**：自定义模型出现在 `model list` 中，别名可解析。

> 待本地验证：真实「构建可运行 Bento」（带 vLLM service、能真正 `openllm serve`）需要 GPU 与可下载的权重，超出本讲范围；本实践聚焦「被发现」这一可离线验证的环节。

#### 4.3.5 小练习与答案

**练习 1**：为什么 README 说自定义仓库要「follow the format … with a `bentos` directory」，而本讲强调必须是 `bentoml/bentos`？

**参考答案**：README 是面向用户的宽松表述；代码 glob 前缀是 `bentoml/bentos/`（[src/openllm/model.py:142](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L142)），少一层 `bentoml/` 就扫不到。`bentoml/` 这一层来自 BentoML 的本地仓库惯例，`BENTOML_HOME=<repo>/bentoml/bentos` 让 BentoML 产物天然落位。

**练习 2**：如果你的自定义 Bento 的 `gpu_type` 写成 `nvidia-tesla-foo`（不在 `ACCELERATOR_SPECS` 里），`model list`、`hello`、`serve` 分别会怎样？

**参考答案**：`model list` 的「required GPU RAM」列空白（`pretty_gpu` 的 `KeyError` 分支，[src/openllm/common.py:239-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L239-L241)）；`hello` 里 `can_run` 因找不到规格大概率判为不可本地运行；`serve` 仍会尝试启动（OpenLLM 不校验 gpu_type 是否合法，只是无法给出准确的资源提示）。

---

## 5. 综合实践

**任务**：搭建一个真正属于你的、结构正确的迷你模型仓库，并验证别名联动。

1. 在 GitHub 新建一个**公开**空仓库 `my-openllm-models`，本地 clone。
2. 创建 `bentoml/bentos/demo/1b/bento.yaml`，内容满足 `BentoMetadata` 字段，`labels.aliases` 写 `"tiny"`，`resources.gpu_type` 用 `nvidia-tesla-l4`（确保在 `ACCELERATOR_SPECS` 里）。提交并推送。
3. `openllm repo add myrepo <你的 url>@main`。
4. `openllm repo update --verbose`，到 `~/.openllm/repos/<server>/<owner>/my-openllm-models/main/bentoml/bentos/demo/` 下确认存在 `1b/`（含 bento.yaml）和别名文件 `tiny`（内容为 `1b`）。
5. 运行：
   ```bash
   openllm model list --repo myrepo
   openllm model get demo:1b --repo myrepo --verbose
   openllm model get demo:tiny --repo myrepo            # 验证别名
   ```
6. 用一句话回答：`demo:tiny` 的 `tag` 与 `bentoml_tag` 分别是什么？为什么不同？（提示：[src/openllm/common.py:171-179](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L171-L179)）

**预期结果**：第 5 步三条命令全部成功；别名 `tiny` 能解析到真实版本 `1b`。

> 若无网络或无 GitHub 账号，可用 4.1.4 的 `OPENLLM_TEST_REPO` 本地目录法等价完成第 4–5 步的验证（手动放好 `bento.yaml` 与别名文件，直接 `list_bento()`）。

---

## 6. 本讲小结

- **目录约定是代码真相**：自定义仓库必须在根下放 `bentoml/bentos/<模型名>/<版本>/bento.yaml`（注意是 `bentoml/bentos/`，不是 `bentos/`），因为 `list_bento` 的 glob 前缀就是它（[src/openllm/model.py:141-147](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L141-L147)）。
- **模型名/版本来自目录名，不来自 bento.yaml 字段**：`BentoInfo.name/version` 直接取 `path.parent.name`/`path.name`（[src/openllm/common.py:181-187](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L181-L187)）。
- **注册与发现是解耦的**：`repo add` 只写 `config.json` 登记表不克隆，发现要靠 `repo update` 落地的克隆，中间靠 `ensure_repo_updated` 做新鲜度闸门。
- **别名是「物化」出来的**：`_complete_alias` 在每次 `repo update` 末尾，按 `bento.yaml` 的 `labels.aliases` 把别名写成普通文件，`list_bento` 再把它读回带别名的 `BentoInfo`（[src/openllm/repo.py:144-152](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/repo.py#L144-L152)）。
- **`tag` vs `bentoml_tag`**：前者别名感知（给用户看），后者恒为真实版本（给 `bentoml serve` 用）。
- **工具链归属要分清**：`make.py`/`recipe.yaml`/`vllm-chat` 属于 `bentoml/openllm-models`，不在本仓库；自定义仓库可用原生 `bentoml build` + `BENTOML_HOME=<repo>/bentoml/bentos` 直接产 Bento。

---

## 7. 下一步学习建议

至此你已走完 OpenLLM 学习手册的全部 15 讲，从「认识项目」到「二次开发」打通了整条链路。接下来建议：

1. **把发现接到运行**：本讲止步于「被发现」。若你有 GPU，继续走 [src/openllm/local.py](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/local.py)（u3-l1）的 `serve`/`run` 链路，验证自定义 Bento 能真正起服务、用 OpenAI 客户端对话。
2. **打通云端**：参考 u3-l2（cloud.py）把自定义模型 `openllm deploy` 到 BentoCloud，注意 `--env HF_TOKEN` 等环境变量三层优先级。
3. **向上游贡献**：模仿 [openllm-models PR #1](https://github.com/bentoml/openllm-models/pull/1)（README 与 DEVELOPMENT.md 都指向它），为默认仓库贡献一个新模型，体会 `recipe.yaml → make.py → bento.yaml → git` 的官方工作流。
4. **深挖可运行性判定**：若你的自定义 Bento 资源声明特殊，回头精读 u2-l5 的 `can_run` 打分公式，确保 `hello` 里能正确打勾。
