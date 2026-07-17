# 模型发现与 Bento 解析 model.py

## 1. 本讲目标

在前面几讲里，我们已经知道 `openllm serve llama3.2` 这条命令的「指挥层」在 `__main__.py`，而真正干活前必须先回答一个问题：**`llama3.2` 这个字符串，到底对应磁盘上哪一个可以运行的模型？** 本讲就聚焦这个「名字 → 可运行对象」的解析过程。

读完本讲，你应当能够：

- 说清楚 `list_bento` 如何用 `glob` 扫描仓库目录、如何识别「真实版本目录」与「别名文件」、如何去重。
- 说清楚 `BentoInfo` 的 `tag / bentoml_tag / name / version / labels / envs / pretty_yaml / pretty_gpu` 这些属性分别从 `bento.yaml` 的哪一段派生而来。
- 说清楚 `ensure_bento` 在「0 个 / 1 个 / 多个」匹配时分别怎么处理，以及在资源不足时如何给出黄色提示。

## 2. 前置知识

本讲承接以下已建立的认知（不再重复细节）：

- **模型仓库是 git 仓库**：每个仓库被克隆到 `OPENLLM_HOME/repos/<server>/<owner>/<repo>/<branch>` 下，由 `RepoInfo.path` 指向（见 [u2-l3](u2-l3-repo-management.md)）。本讲所有扫描都发生在 `repo.path` 之内。
- **`ensure_repo_updated` 是新鲜度闸门**：`list_bento` 第一行就调用它，缓存从未更新会硬退出、超 3 天过期只会黄色提醒放行（见 [u2-l3](u2-l3-repo-management.md)）。
- **`_complete_alias` 物化别名文件**：每次 `repo update` 之后，`repo.py` 会读取每个 `bento.yaml` 的 `aliases` 标签，把别名写成「文件名 = 别名、内容 = 版本号」的普通文件。本讲要讲的扫描逻辑正是消费这些产物。
- **`can_run` 返回分数**：分数大于 0 表示本地可运行，等于 0 表示资源不足或平台不匹配（见 [u1-l4](u1-l4-hello-interactive-flow.md)）。本讲会在 `ensure_bento` 里再次遇到它。
- **`output` 按 `VERBOSE_LEVEL` 过滤**：非字符串内容会被 `pyaml` 美化成 YAML 输出（见 [u2-l1](u2-l1-common-config-output.md)）。

一个关键概念：在 OpenLLM 里，一个「可运行的模型版本」在磁盘上对应一个 **Bento 目录**（BentoML 的标准打包单位），目录里有一份 `bento.yaml` 描述它的元信息。本讲要解析的，就是把这样的目录读成一个 Python 对象 `BentoInfo`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/openllm/model.py` | 本讲主角。定义 `model` 子命令组（`list` / `get`），以及核心函数 `list_bento`（扫描）与 `ensure_bento`（匹配）。 |
| `src/openllm/common.py` | 定义 `BentoInfo`（以及 `RepoInfo`、`BentoMetadata`）。`BentoInfo` 的几乎所有派生属性都在这里。 |
| `src/openllm/repo.py` | 提供 `list_repo`（枚举仓库）、`ensure_repo_updated`（新鲜度闸门）、`_complete_alias`（物化别名文件）。本讲把它们当作已存在的工具使用。 |
| `src/openllm/accelerator_spec.py` | 提供 `can_run` 与 `ACCELERATOR_SPECS`，`ensure_bento` 和 `BentoInfo.pretty_gpu` 都依赖它做资源判定与展示。 |

## 4. 核心概念与源码讲解

本讲按「先扫出来、再看里面装了什么、最后选一个」的顺序，拆成三个最小模块：

1. `list_bento`：扫描与去重（产出候选 `BentoInfo` 列表）
2. `BentoInfo` 与 `bento.yaml` 解析（每个候选里到底有什么）
3. `ensure_bento`：把用户输入的模型名匹配成单个 `BentoInfo`

### 4.1 list_bento 扫描与去重

#### 4.1.1 概念说明

仓库克隆下来后，里面长什么样？OpenLLM 约定每个仓库的模型都放在一个固定的子路径下：

```text
<repo.path>/
└── bentoml/
    └── bentos/
        └── <模型名>/            # 例如 llama3.2
            ├── <版本>/          # 例如 1b，是一个「真实版本目录」
            │   └── bento.yaml   # 元信息文件
            └── <别名>           # 例如 latest，是一个普通文件，内容是该别名指向的版本号
```

也就是说，`bentoml/bentos/*/*` 这个 glob 会同时命中两类东西：

- **真实版本目录**：里面有 `bento.yaml`，是模型的一个真实可运行版本。
- **别名文件**：一个普通文件，文件名是别名（如 `latest`），文件内容是它指向的真实版本号（如 `1b`）。这些文件由 `repo.py` 的 `_complete_alias` 在每次更新后写入。

`list_bento` 的职责，就是把这两类东西统一扫成一个 `BentoInfo` 列表。

#### 4.1.2 核心流程

`list_bento` 的执行过程可以概括为：

1. 调 `ensure_repo_updated()` 做新鲜度检查（u2-l3 讲过的闸门）。
2. 解析「仓库名 / 模型名」：如果调用者传了 `myrepo/llama3.2` 这种带斜杠的 tag，就拆成 `repo_name=myrepo`、`tag=llama3.2`。
3. 根据 tag 构造 glob 模式（见下表）。
4. 对每个仓库，按 glob 扫描路径，并按 `(模型名, 版本号里的数字, 长度, 版本名)` 排序。
5. 逐个判断每个路径是「真实版本目录」「别名文件」还是「其他」，构造对应的 `BentoInfo`。
6. 默认按 `bento.yaml` 里的 `name:version` 去重，丢弃重复项（别名和它指向的真实版本算同一个）。

tag 到 glob 模式的映射是理解整个扫描的关键：

| 输入 tag | 构造的 glob 模式 | 含义 |
| --- | --- | --- |
| 不传（`None`） | `bentoml/bentos/*/*` | 扫描全部模型、全部版本 |
| `llama3.2`（无冒号） | `bentoml/bentos/llama3.2/*` | 该模型的所有版本（及别名） |
| `llama3.2:1b`（有冒号） | `bentoml/bentos/llama3.2/1b` | 精确命中某一个版本或别名 |

注意第三种：当你写 `llama3.2:1b` 时，glob 精确匹配到 `1b` 这一项——它既可能是真实版本目录，也可能是别名文件（比如 `llama3.2:latest` 会命中别名文件 `latest`）。这正是别名可以被当作 tag 直接使用的原因。

#### 4.1.3 源码精读

先看入口与 glob 构造：[src/openllm/model.py:122-148](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L122-L148)。这段代码先做新鲜度检查、拆分斜杠 tag、校验仓库名是否存在，然后按上表的规则拼出 `glob_pattern`。

接着是扫描 + 排序 + 分类构造的核心循环：[src/openllm/model.py:149-167](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L149-L167)。关键几点：

- 排序键 `(x.parent.name, _extract_first_number(x.name), len(x.name), x.name)`：`x.parent.name` 是模型名（保证同模型聚在一起）；`_extract_first_number(x.name)` 从版本名里抓第一个数字（`1b`→1、`70b`→70），从而让版本号按数值而不是字符串排序；后两项是兜底的字典序。`_extract_first_number` 抓不到数字时返回 `100`（见 [src/openllm/model.py:114-119](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L114-L119)），所以像 `latest` 这种无数字的别名会排到很靠后。
- 分类逻辑用「是目录且含 `bento.yaml`」「是普通文件」「其他」三态区分：目录 → 真实版本；普通文件 → 别名文件（读其内容作为指向的真实版本，构造带 `alias` 的 `BentoInfo`）；都不是 → 跳过。

最后是去重：[src/openllm/model.py:169-179](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L169-L179)。当 `include_alias=False`（默认值）时，按 `bento_yaml` 里的 `name:version` 去重。这里用了一个「在列表推导里借助 `seen.add(...)` 副作用」的惯用法：`seen.add` 返回 `None`（假值），所以「键已在集合里」或「刚把它加进集合」都会被过滤掉，只保留每个 `name:version` 的第一次出现。

> 关于别名的一个精确事实：别名文件 `latest`（内容 `1b`）构造出的 `BentoInfo`，其 `bento_yaml` 实际读的是它指向的真实版本目录的 `bento.yaml`（见下文 4.2.3，`path` 被设成了 `origin_path`）。所以别名与真实版本的 `bento_yaml['name']:bento_yaml['version']` 完全相同，去重时必然成对出现、后者被丢弃。又因为排序时真实版本（数字小）排在别名（数字 100）前面，**默认调用下保留的是真实版本、丢弃别名**。因此 `openllm model list` 通常只列真实版本，而你仍然可以用 `openllm model get llama3.2:latest` 这种带别名的精确 tag 命中它（走的是「有冒号」的精确 glob 分支，列表里只有一项，不会被去重）。顺带一提：当前代码库里没有任何调用方显式传 `include_alias=True`（四个调用点 `__main__.py:235`、`model.py:37`、`model.py:90`、`repo.py:147` 都用默认值 `False`），所以这个开关目前主要留给编程式调用使用。

#### 4.1.4 代码实践

**实践目标**：亲手验证「无冒号 tag」与「有冒号 tag」走的是不同的 glob 分支，并观察别名如何被去重。

**操作步骤**（源码阅读型实践，无需 GPU）：

1. 先确保本地有仓库缓存：运行 `openllm repo update`（或任意一条会触发更新的命令）。
2. 进入缓存目录查看真实布局（路径形如 `~/.openllm/repos/github.com/bentoml/openllm-models/main/bentoml/bentos/`），列出某个模型（如 `llama3.2`）下的条目，区分哪些是目录、哪些是别名文件，并 `cat` 一个别名文件看其内容。
3. 在 Python 里对照源码手工模拟两种 glob：

   ```python
   # 示例代码：仅用于对照源码理解，不是项目原有代码
   from openllm.model import list_bento
   # 无冒号：返回 llama3.2 的所有真实版本（别名被去重）
   for b in list_bento('llama3.2'):
       print(b.tag, '->', b.bentoml_tag)
   # 有冒号：精确命中别名 latest（如果存在）
   # for b in list_bento('llama3.2:latest'):
   #     print(b.tag, '->', b.bentoml_tag)
   ```

**需要观察的现象**：第 3 步第一段输出里，每个 `b.tag` 形如 `llama3.2:1b`，且与 `b.bentoml_tag` 相同（因为没有别名混入）；如果取消注释第二段且 `latest` 存在，会看到 `b.tag` 是 `llama3.2:latest` 而 `b.bentoml_tag` 是 `llama3.2:1b`——这正是「用户面向的别名 tag」与「底层真实版本 tag」的区别。

**预期结果**：能口述「为什么默认列表里看不到别名，但用 `模型:别名` 又能精确取到它」。若本地缓存里该模型恰好没有别名，则第二段为空，属正常现象（**待本地验证**：具体别名取决于默认仓库当前内容）。

#### 4.1.5 小练习与答案

**练习 1**：为什么排序键里要用 `_extract_first_number(x.name)` 而不是直接用 `x.name`？

> **答案**：直接按字符串排序时 `10b` 会排在 `1b` 和 `2b` 之间（字典序 `'10b' < '1b'` 在第二字符 `'0' < 'b'` 成立时会出现错乱），用版本名里的数字作为主排序键才能让 `1b < 2b < 10b < 70b` 按数值大小排列。

**练习 2**：`list_bento()`（不传任何参数）默认会扫描哪些仓库？

> **答案**：会扫描 `config.json` 里登记的全部仓库（`list_repo(None)` 返回所有仓库），因为 `repo_name` 为 `None` 时 `list_repo` 不做过滤。这也是为什么默认 `openllm model list` 先用 `load_config().default_repo` 限定到单个仓库（见 4.3.3），避免把所有仓库的模型混在一起。

### 4.2 BentoInfo 与 bento.yaml 解析

#### 4.2.1 概念说明

`list_bento` 产出的每个元素都是一个 `BentoInfo`。它是 `common.py` 里的一个 Pydantic 模型，作用是「把磁盘上的一个 Bento 目录，懒加载地包装成一组好用的属性」。所谓懒加载，是指只有两个字段（`repo`、`path`、`alias`）是构造时传入的「身份」，其余如 `labels`、`envs`、`pretty_gpu` 都是首次访问时才去读 `bento.yaml`，并用 `functools.cached_property` 缓存。

理解 `BentoInfo` 的关键是分清两套「名字」：

- **`tag`**：用户面向的可寻址名字。真实版本是 `模型名:版本号`，别名是 `模型名:别名`。你在 `openllm serve <tag>` 里写的就是它。
- **`bentoml_tag`**：底层真实版本的 tag，永远是 `模型名:版本号`（不含别名）。传给 `bentoml serve` / `bentoml deploy` 的是它。

#### 4.2.2 核心流程

`BentoInfo` 的派生属性可以这样归类：

| 属性 | 来源 | 用途 |
| --- | --- | --- |
| `name` | `path.parent.name`（模型名） | 表格「model」列 |
| `version` | `path.name`（版本号，别名时是真实版本） | 内部使用 |
| `tag` | `name + (:alias 或 :version)` | 用户面向、表格「version」列 |
| `bentoml_tag` | `name:version`（恒为真实版本） | 传给 `bentoml serve/deploy` |
| `bento_yaml` | 读取 `path/bento.yaml` 并 `yaml.safe_load` | 所有字段的源头（cached） |
| `labels` | `bento_yaml['labels']` | 读 `aliases`/`platforms` 等 |
| `envs` | `bento_yaml['envs']` | 注入运行时环境变量 |
| `platforms` | `labels['platforms']`（默认 `'linux'`）逗号切分 | 平台匹配 |
| `pretty_yaml` | 由 `services`/`schema` 精简出的易读字典 | `--verbose` 展示 |
| `pretty_gpu` | `services[0].config.resources` + `ACCELERATOR_SPECS` | 表格「required GPU RAM」列 |

`tolist()` 则按 `VERBOSE_LEVEL` 分档输出：0 档只给 `str(self)`，10 档给含 `pretty_yaml` 的摘要，20 档给完整 `bento_yaml`。

#### 4.2.3 源码精读

先看身份字段与两套 tag 的定义：[src/openllm/common.py:156-188](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L156-L188)。注意 `tag` 在 `alias` 非空时返回 `模型名:alias`，否则返回 `模型名:版本号`；而 `bentoml_tag` 永远是后者。`__str__` 在默认仓库时只显示 `tag`，非默认仓库则加 `仓库名/` 前缀，这正是你在命令行里看到的展示形式。

再看懒加载的元信息属性：[src/openllm/common.py:197-225](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L197-L225)。`bento_yaml` 用 `cached_property` 把 `yaml.safe_load((self.path / 'bento.yaml').read_text())` 的结果缓存起来，后续所有属性都复用它，避免重复读盘解析。`pretty_yaml` 在「只有单个 service」时把原始 `bento.yaml` 精简成 `{apis, resources, envs, platforms}` 四块易读结构，方便在 `openllm model get --verbose` 里查看；若多于一个 service 则原样返回。

接着是 `pretty_gpu`：[src/openllm/common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241)。它从 `services[0]['config']['resources']` 取 `gpu` 数量与 `gpu_type`，再用 `gpu_type` 去 `ACCELERATOR_SPECS`（见 [accelerator_spec.py:35-61](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L35-L61)）查出该卡型的显存，格式化为 `8G`（单卡）或 `80Gx2`（多卡）；若 `gpu_type` 不在表里（`KeyError`）或不需要 GPU，则返回空串。这就是 `openllm model list` 表格里「required GPU RAM」列的来源。

最后是分档输出：[src/openllm/common.py:243-255](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L243-L255)，配合 `output` 函数的非字符串对象会被 `pyaml` 美化（u2-l1 讲过），所以 `openllm model get llama3.2:1b` 默认输出一行 tag，加 `--verbose` 后会变成结构化的 YAML。

> 顺带提一句「别名 BentoInfo 的 path」：4.1.3 里构造别名时 `path=origin_path`（即真实版本目录），所以别名对象的 `bento_yaml`、`labels`、`pretty_gpu` 都来自它指向的真实版本——这正是别名「行为与真实版本一致、只是名字不同」的实现方式。

#### 4.2.4 代码实践

**实践目标**：观察 `BentoInfo` 各属性在默认与 `--verbose` 下的输出差异，并验证 `pretty_gpu` 的来源。

**操作步骤**：

1. 运行 `openllm model get llama3.2:1b`，记录默认输出（应只有一行 tag）。
2. 运行 `openllm model get llama3.2:1b --verbose`，记录 YAML 结构，找到 `resources`、`envs`、`platforms` 字段。
3. 对照 [common.py:227-241](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/common.py#L227-L241) 与 [accelerator_spec.py:35-61](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L35-L61)，手动用第 2 步看到的 `resources.gpu_type` 查表，算出「required GPU RAM」应显示什么。

**需要观察的现象**：默认输出是一行；`--verbose` 输出是 YAML，里面能看到 `pretty_yaml` 精简后的 `apis/resources/envs/platforms`（因为该模型只有单个 service）。

**预期结果**：你手工查表算出的显存字符串，应与 `openllm model list` 表格里该模型的「required GPU RAM」列一致。若该模型不需要 GPU，则该列为空（`pretty_gpu` 返回 `''`）。**待本地验证**：具体显存值取决于默认仓库里该模型当前的 `bento.yaml`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `bento_yaml` 用 `cached_property` 而不是普通 `@property`？

> **答案**：`bento.yaml` 要读盘并做 `yaml.safe_load`，开销不小，而同一个 `BentoInfo` 会被 `labels`、`envs`、`platforms`、`pretty_gpu`、`tolist()` 反复访问。用 `cached_property` 只在首次访问时解析一次，之后直接复用缓存，避免重复 IO 和解析。

**练习 2**：`tag` 和 `bentoml_tag` 何时会不同？给出一个具体例子。

> **答案**：当 `BentoInfo` 是「别名」时二者不同。例如别名 `latest` 指向真实版本 `1b`：`tag == 'llama3.2:latest'`（用户面向），`bentoml_tag == 'llama3.2:1b'`（传给 `bentoml serve` 的真实版本）。真实版本对象则两者相同。

### 4.3 ensure_bento 匹配逻辑

#### 4.3.1 概念说明

用户在命令行写 `openllm serve llama3.2` 时，给的往往只是一个模糊的名字（可能没带版本）。`ensure_bento` 就是把这串输入「确定化」为**唯一一个** `BentoInfo` 的函数——它服务于 `serve` / `run` / `deploy` / `model get` 等所有需要「拿到一个具体 Bento」的命令。它的设计哲学是「要么给你唯一的答案，要么明确告诉你为什么给不了」。

#### 4.3.2 核心流程

`ensure_bento(model, target=None, repo_name=None)` 的判定是一个三分支状态机：

```
               list_bento(model, repo_name)
                        │
          ┌─────────────┼──────────────┐
          ▼             ▼              ▼
      0 个匹配       1 个匹配       多个匹配
          │             │              │
   红色报错 +        若给了 target   红色提示
   Exit(1)          且 can_run≤0     "Multiple models"
                    → 黄色资源不足    + 调 list_model
                    提示（仍返回）    列出候选 + Exit(1)
                        │
                     返回该 BentoInfo
```

注意三个细节：

- **0 个匹配**：直接红色报错并退出（`typer.Exit(1)`），不会往下走。
- **1 个匹配**：是「成功路径」。若调用者同时传了 `target`（运行目标），还会顺带用 `can_run` 检查资源是否够；不够只给**黄色提醒、仍然返回**这个 Bento（不阻断，把「要不要硬跑」的决定权留给后续命令）。
- **多个匹配**：红色提示，并复用 `list_model` 把候选打印出来帮你选，然后退出。

#### 4.3.3 源码精读

`ensure_bento` 主体：[src/openllm/model.py:80-108](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L80-L108)。注意它和 `model list` 一样，在 `repo_name` 为空时先用 `load_config().default_repo` 兜底（[model.py:85-88](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L85-L88)），所以不指定 `--repo` 时只在默认仓库里找。

资源不足提示在第 97-102 行：调用 `can_run(bentos[0], target)`，当返回值 `<= 0` 时打印黄色警告。`can_run` 的打分逻辑（[accelerator_spec.py:116-149](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/accelerator_spec.py#L116-L149)）可概括为：平台不匹配返回 `0.0`；没有资源声明返回 `0.5`；需要 GPU 但张数/显存不够返回 `0.0`；否则返回一个正的加权分数。其 GPU 分支的得分公式为：

\[
\text{score} = \frac{\text{required\_memory} \times \text{required\_gpu\_count}}{\sum \text{local\_accelerator\_memory}}
\]

即「所需总显存」占「本机总显存」的比例——需求越接近本机上限，分数越接近 1；超了则在前面就被判 `0.0`。

多个匹配分支第 106-108 行直接调用 `list_model(model, repo=repo_name)` 复用列表展示，这是个很省事的复用：报错信息顺便就是把候选模型列一遍。

再看 `ensure_bento` 在命令链里的位置：[src/openllm/__main__.py:267](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L267)（`serve`）、[293](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L293)（`run`）、[322](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L322)（`deploy`）。可以看到 `serve` 和 `run` 都先 `get_local_machine_spec()` 拿到本机 `target`，再 `ensure_bento(model, target=target, ...)`——正因为传了 `target`，才会触发那条「资源不足黄色提醒」；而 `deploy` 不传 `target`（部署到云端，本机资源无关），所以不会有这条提醒。

`model get` 命令本身则是 `ensure_bento` 最薄的包装：[src/openllm/model.py:15-21](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L15-L21)，拿到后直接 `output_(bento_info)`。

#### 4.3.4 代码实践

**实践目标**：用故意模糊的输入触发 `ensure_bento` 的「多个匹配」分支，并观察一次成功的「1 个匹配」输出。

**操作步骤**：

1. 运行 `openllm model get llama3.2`（不带版本号）。若该模型在默认仓库里有多个版本，会触发「多个匹配」分支，红色提示并列出候选。
2. 运行 `openllm model get llama3.2:1b`（带具体版本），应命中「1 个匹配」分支，绿色提示 `Found model ...` 并输出该 Bento。
3. 对照 [model.py:80-108](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L80-L108) 解释第 1、2 步分别走了哪个 `if` 分支。

**需要观察的现象**：第 1 步若有多版本会看到 `Multiple models match ...` 加一张候选表；第 2 步会看到 `Found model llama3.2:1b`（绿色）。

**预期结果**：能口述「0/1/多」三种情况各自的输出与退出码（0 个和多 个都会 `Exit(1)`，1 个正常返回）。**待本地验证**：第 1 步是否真的「多个匹配」取决于默认仓库里 `llama3.2` 当前的版本数量；若只有一个版本，则第 1 步也会走「1 个匹配」分支。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `ensure_bento` 在资源不足时只是「黄色提醒」而不是直接报错退出？

> **答案**：因为 `can_run` 只是「按规格表的乐观估计」，未必准确（实际能否跑还受量化、上下文长度等影响）。`ensure_bento` 把「要不要硬跑」交给下游命令和用户决定，自己只负责「解析出唯一的 Bento」，职责单一。所以资源不足不阻断，仅提示。

**练习 2**：`openllm deploy llama3.2` 不会出现「资源不足黄色提醒」，而 `openllm serve llama3.2` 会。结合源码说明原因。

> **答案**：`serve` 调 `ensure_bento(model, target=target, ...)` 传了本机 `target`（[__main__.py:267](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L267)），触发 `can_run` 检查；`deploy` 调 `ensure_bento(model, repo_name=repo)` 没传 `target`（[__main__.py:322](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/__main__.py#L322)），`ensure_bento` 里 `target is not None` 为假，跳过资源检查。部署目标是云端实例，本机资源本就无关。

## 5. 综合实践

把三个模块串起来：写一个小脚本，**不依赖** `openllm model list` 的现成输出，而是直接调用本讲的函数，自己重建一张「模型清单」，并验证它与 CLI 输出一致。

```python
# 示例代码：综合实践，不是项目原有代码
from openllm.common import VERBOSE_LEVEL, load_config
from openllm.model import list_bento, ensure_bento

# 1. 只看默认仓库（与 openllm model list 的默认行为一致）
repo = load_config().default_repo
print('default repo =', repo)

# 2. 扫描该仓库全部模型（真实版本，别名已去重）
bentos = list_bento(repo_name=repo)
bentos.sort(key=lambda x: x.name)

# 3. 逐个打印：name | tag(=version 列) | repo | pretty_gpu(=required GPU RAM 列)
for b in bentos:
    print(f'{b.name:20s} | {b.tag:30s} | {b.repo.name:10s} | GPU={b.pretty_gpu or "-"}')

# 4. 选一个模型，走一次 ensure_bento 的「1 个匹配」路径
if bentos:
    pick = bentos[0]
    print('\nensure_bento ->', ensure_bento(pick.name, repo_name=repo))
```

要求：

1. 运行脚本后，把你脚本里第 3 步打印的表格，与 `openllm model list`（不带 `--repo`）的输出逐列对照。
2. 写明 CLI 表格的五列 `model / version / repo / required GPU RAM / platforms` 分别来自 `BentoInfo` 的哪个属性（提示：注意「version」列填的其实是 `tag` 而不是 `version`，见 [model.py:64-77](https://github.com/bentoml/OpenLLM/blob/ec2355ce1a75176164c451cbb7592b3046531540/src/openllm/model.py#L64-L77)）。
3. 解释为什么你的脚本里出现的都是真实版本、没有别名。

通过这个实践，你会同时用到 `list_bento`（扫描去重）、`BentoInfo`（属性派生）和 `ensure_bento`（匹配）三个模块，并真正理解 `openllm model list` 背后发生了什么。**待本地验证**：具体模型清单取决于默认仓库当前内容，需先 `openllm repo update`。

## 6. 本讲小结

- `list_bento` 按 tag 构造 glob（无冒号→该模型所有版本、有冒号→精确命中），扫描 `bentoml/bentos/*/*`，把「真实版本目录」和「别名文件」统一成 `BentoInfo`，再按 `bento.yaml` 的 `name:version` 去重。
- 别名文件由 `repo.py` 的 `_complete_alias` 物化（文件名=别名、内容=版本号）；默认调用 `include_alias=False` 会丢弃别名、保留真实版本，但仍可用 `模型:别名` 精确命中。
- `BentoInfo` 只在构造时持有 `repo/path/alias` 三项身份，其余（`labels/envs/platforms/pretty_yaml/pretty_gpu`）都从 `bento.yaml` 懒加载并缓存。
- `tag` 是用户面向的别名感知名字，`bentoml_tag` 是传给 `bentoml serve/deploy` 的真实版本 tag——这是理解别名行为的关键区别。
- `ensure_bento` 是「名字 → 唯一 Bento」的状态机：0 个或多 个匹配都红色报错退出，1 个匹配成功返回；若传了 `target` 还会用 `can_run` 给出资源不足的黄色提醒（不阻断）。
- `openllm model list` 表格的列分别对应 `bento.name`（去重留空）、`bento.tag`、`bento.repo.name`、`bento.pretty_gpu`、`bento.platforms`。

## 7. 下一步学习建议

本讲把「名字 → BentoInfo」讲透了，但 `BentoInfo` 里的 `resources` 还只是规格表里的一个声明。下一讲 [u2-l5 加速器规格与可运行性判定](u2-l5-accelerator-spec.md) 会深入 `accelerator_spec.py`，讲清楚 `ACCELERATOR_SPECS` 这张表、`get_local_machine_spec` 如何用 NVML 探测真实 GPU、以及 `can_run` 的完整打分公式——也就是本讲里被「借用」的那条资源判定逻辑的真正来源。

如果想提前看到「拿到 BentoInfo 之后怎么用」，可以跳到 [u3-l1 本地 serve 与 run 的完整链路](u3-l1-local-serve-run.md)，观察 `bento.bentoml_tag`、`bento.envs` 是如何被注入到 `bentoml serve` 子进程里的。
