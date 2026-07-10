# 模型管理与配置

## 1. 本讲目标

本讲承接 u3-l1（`kt` 子命令总览）与 u3-l2（`kt run` 启动推理），把焦点从「怎么跑命令」转到「命令背后的两类数据」。读完后你应当能够：

- 区分两套截然不同的「模型账本」：用户模型注册表 `~/.ktransformers/user_models.yaml`（你磁盘上**实际有**哪些模型文件）与内置模型库 `BUILTIN_MODELS`（KTransformers **支持/推荐**哪些模型）。
- 说清 `kt model scan / add / list / edit / verify / remove` 这一整套模型管理命令背后的 `UserModelRegistry` 数据结构与 CRUD 逻辑。
- 掌握 `kt config show / set / get / reset / path / init` 如何读写 `~/.ktransformers/config.yaml`，理解点分键（`server.port`）、深合并、单例与默认值回退。
- 描述 `~/.ktransformers/config.yaml` 的完整结构（general / paths / server / inference / download / advanced / dependencies 各段的作用）。

## 2. 前置知识

继续之前，请确认你了解以下概念（不熟悉也没关系，下面会顺带解释）：

- **`kt` 子应用**：在 u3-l1 里讲过，`kt model` 与 `kt config` 是用 `app.add_typer` 挂上去的「命令组」，各自是一个独立的 `typer.Typer()` 实例，其下再分若干子命令。
- **YAML**：一种用缩进表示层级的配置文件格式，`kt-kernel` 用它来存配置和模型清单，靠 `pyyaml` 读写。
- **dataclass（数据类）**：Python 的 `@dataclass` 装饰器，能自动生成 `__init__`/`__repr__` 等方法，`UserModel` 和 `ModelInfo` 都用它来定义结构。
- **UUID**：通用唯一标识符。`UserModel` 内部给每个模型生成一个 UUID 作为稳定 ID，这样即便用户改名，模型间的关联关系（如 CPU 模型挂到哪个 GPU 模型）也不会断。
- **点分键（dot-separated key）**：用 `.` 表示配置层级，例如 `server.port` 指向配置里 `server` 段下的 `port` 字段，避免写嵌套字典访问。
- **两类模型账本的分工**（本讲核心）：`UserModelRegistry` 回答「我**手头**有什么」（本地文件清单），`ModelRegistry` 回答「这个模型 kt **认不认得**、推荐怎么跑」（知识库/目录）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `kt-kernel/python/cli/utils/user_model_registry.py` | 用户模型注册表：`UserModel` 数据类与 `UserModelRegistry` 的增删改查，落盘到 `user_models.yaml`。 |
| `kt-kernel/python/cli/utils/model_registry.py` | 内置模型库：`ModelInfo` 数据类、`BUILTIN_MODELS` 常量与带模糊匹配的 `ModelRegistry`，外加各模型 GPU 专家数计算函数。 |
| `kt-kernel/python/cli/commands/model.py` | `kt model` 子应用：`scan/add/list/edit/info/verify/remove/download/path-*` 等命令。 |
| `kt-kernel/python/cli/commands/config.py` | `kt config` 子应用：`init/show/set/get/reset/path` 子命令。 |
| `kt-kernel/python/cli/config/settings.py` | 配置管理器 `Settings`：`~/.ktransformers/config.yaml` 的默认值、加载（深合并）、点分键读写与单例。 |
| `kt-kernel/python/cli/utils/model_discovery.py` | 模型发现工具：`scan`/`add`/`download` 共用的扫描+登记逻辑。 |

## 4. 核心概念与源码讲解

### 4.1 用户模型注册表（UserModelRegistry）

#### 4.1.1 概念说明

大模型动辄几十到几百 GB，用户通常把它们散落在多个磁盘目录里。`kt` 需要一个「清单」来记住「我手头有哪些模型、分别在哪个路径、是什么格式、是从哪个仓库下的、校验过没有」。这份清单就是**用户模型注册表**，落盘成一个 YAML 文件：

```
~/.ktransformers/user_models.yaml
```

它由 `UserModelRegistry` 类管理，核心是一条 `List[UserModel]`。每条记录是一个 `UserModel` 数据类，字段既包含「身份信息」（名字、路径、格式、UUID），也包含「来源与状态」（仓库类型/ID、SHA256 校验状态、MoE 分析结果、AMX 量化元数据）。

需要特别强调：**这个注册表只记「我有什么」，不记「怎么跑」**。后者属于下一节 4.3 的内置模型库。两者职责分离是本讲最重要的设计点。

#### 4.1.2 核心流程

注册表的生命周期围绕一个内存列表 `self.models` 展开，所有操作都先改内存、再 `save()` 落盘：

```
UserModelRegistry()              # 构造时
  ├─ self.models = []
  ├─ load()                      # 读 user_models.yaml
  │    ├─ 文件不存在 → 空表 + 创建文件
  │    └─ 文件存在 → 解析每条 → UserModel.from_dict(...)
  │                  并迁移补齐缺失的 UUID
  │
增: add_model(m)   → 查重名(check_name_conflict) → append → save
删: remove_model(name) → 列表过滤 → save
改: update_model(name, {字段:值}) → setattr 循环 → save
查: get_model(name) / get_model_by_id(uuid) / find_by_path(path) / list_models()
```

`kt model` 的命令到注册表方法的对应关系：

| 命令 | 主要调用的注册表方法 | 作用 |
| --- | --- | --- |
| `kt model scan` | 经 `discover_and_register_global` → `add_model` | 全盘扫描并登记新模型 |
| `kt model add <path>` | 经 `discover_and_register_path` → `add_model` | 扫描指定目录并登记 |
| `kt model list` | `list_models()` | 列出全部（并清理路径失效的） |
| `kt model info <name>` | `get_model(name)` | 显示单条详情 |
| `kt model edit <name>` | `get_model` + `update_model` | 交互式改名/改仓库/改关联 |
| `kt model remove <name>` | `remove_model(name)` | 从清单删除（**不删文件**） |
| `kt model verify <name>` | `get_model` + `update_model(sha256_status=...)` | SHA256 校验并回写状态 |

#### 4.1.3 源码精读

注册表文件路径与版本常量：

[kt-kernel/python/cli/utils/user_model_registry.py:L14-L16](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L14-L16) — 清单固定写在 `~/.ktransformers/user_models.yaml`，`REGISTRY_VERSION` 用于未来格式升级时做迁移判断。

`UserModel` 数据类定义了一条模型记录的全部字段：

[kt-kernel/python/cli/utils/user_model_registry.py:L19-L41](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L19-L41) — `name`/`path`/`format`（safetensors 或 gguf）是必填的身份字段；`id` 是自动生成的 UUID；`repo_type`/`repo_id` 记录来源（HuggingFace/ModelScope）；`sha256_status` 是校验状态机；`gpu_model_ids` 把一个 CPU 模型（GGUF/AMX）关联到若干 GPU 模型 UUID，用于联合启动；`is_moe`/`moe_num_experts*` 缓存 MoE 分析结果；`amx_*` 记录 AMX 量化元数据。

UUID 在构造后自动补齐，保证每条记录有稳定标识：

[kt-kernel/python/cli/utils/user_model_registry.py:L42-L47](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L42-L47) — 用 `__post_init__` 在对象创建后补 `id`。这正是「关联关系用 UUID 而非名字」的原因：用户改名不会让 CPU↔GPU 的链接失效。

加载逻辑包含一个向后兼容的 UUID 迁移：

[kt-kernel/python/cli/utils/user_model_registry.py:L83-L119](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L83-L119) — 读 YAML 后遍历所有模型，凡是 `id` 为空的旧记录都补一个 UUID 并立即 `save()`，使老清单平滑升级到新格式。

`save()` 用 `yaml.safe_dump` 落盘，保留字段顺序（`sort_keys=False`）：

[kt-kernel/python/cli/utils/user_model_registry.py:L121-L129](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L121-L129) — 注意保存结构是 `{version, models: [...]}`，与加载时的 `data.get("models", [])` 对应。

新增模型前先查重名，避免名字冲突：

[kt-kernel/python/cli/utils/user_model_registry.py:L131-L145](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L131-L145) — 重名直接 `raise ValueError`，调用方（如 `scan`）会先用 `suggest_name` 起一个带 `-2`/`-3` 后缀的唯一名。

`suggest_name` 用递增后缀避免冲突，是 scan/add 自动登记不报错的关键：

[kt-kernel/python/cli/utils/user_model_registry.py:L284-L302](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L284-L302) — 从 `base_name-2` 开始尝试，直到 `check_name_conflict` 返回 False。

`update_model` 用 `setattr` 循环套用变更字典，是 `edit`/`verify`/`download`/`link-cpu` 共同的写回入口：

[kt-kernel/python/cli/utils/user_model_registry.py:L165-L186](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/user_model_registry.py#L165-L186) — 它先 `hasattr` 判断字段是否存在再赋值，避免写入未定义字段污染数据类。

`scan` 命令把这些串起来——全盘发现、去重、登记：

[kt-kernel/python/cli/utils/model_discovery.py:L22-L61](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_discovery.py#L22-L61) — `discover_and_register_global` 先用底层 `discover_models` 扫盘，再用 `find_by_path` 过滤掉已在清单中的，最后逐个 `_create_and_register_model` 登记新模型。

单条模型的登记会顺带自动探测仓库来源与 MoE 信息：

[kt-kernel/python/cli/utils/model_discovery.py:L124-L179](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_discovery.py#L124-L179) — 这里能看到「先用 `suggest_name` 起唯一名 → 尝试从 README.md frontmatter 探测 `repo_id` → 对 safetensors 跑 `analyze_moe_model` 缓存 MoE 信息 → `add_model`」的完整流水线，每一步失败都静默继续，保证扫描健壮。

`kt model` 子应用本身的注册与「无子命令默认列模型」的回调：

[kt-kernel/python/cli/commands/model.py:L85-L101](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/model.py#L85-L101) — `invoke_without_command=True` 配合 `callback`，使得直接敲 `kt model`（不带子命令）时默认调用 `list_models(...)`，省去敲 `list`。

`list` 命令在列出前会自动清理路径已失效的记录：

[kt-kernel/python/cli/commands/model.py:L513-L536](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/model.py#L513-L536) — 遍历每条记录的 `path_exists()`，对路径不存在的自动 `remove_model`，并刷新列表。这是一种「惰性自愈」——文件被删后，下次 `kt model list` 自动清理脏记录。

判断一个 safetensors 目录是否为 AMX（NUMA 感知量化）权重，靠正则匹配 `.numa.N.`：

[kt-kernel/python/cli/commands/model.py:L46-L82](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/model.py#L46-L82) — 它打开前 3 个 safetensors 文件，收集所有 `.numa.{数字}.` 键里的 NUMA 下标，据此返回 `(是否AMX, NUMA节点数)`，`list`/`info`/`link-cpu` 都用它来区分 AMX 与普通 GPU 权重。

#### 4.1.4 代码实践

**实践目标**：亲手把一个本地模型目录登记进注册表，观察 `user_models.yaml` 的变化。

**操作步骤**：

1. 准备一个含 `config.json`（safetensors）或 `*.gguf` 的目录（哪怕是一个很小的测试模型目录）。假设路径为 `/tmp/my-model`。
2. 登记它：
   ```bash
   kt model add /tmp/my-model
   ```
3. 查看清单（无子命令也行）：
   ```bash
   kt model list
   # 或
   kt model
   ```
4. 看详情：
   ```bash
   kt model info <登记时显示的名字>
   ```
5. 直接查看落盘的清单文件：
   ```bash
   cat ~/.ktransformers/user_models.yaml
   ```
6. 删除登记（注意它**不会**删磁盘文件）：
   ```bash
   kt model remove <名字>
   ```

**需要观察的现象**：

- `add` 后 `user_models.yaml` 多出一条记录，含 `name`/`path`/`format`/`id`(UUID)/`created_at`，可能还有自动探测到的 `repo_id` 与 `is_moe`。
- `list` 输出会按 GGUF / AMX / GPU(safetensors) 分表显示。
- 若 `/tmp/my-model` 不存在或被删，再次 `kt model list` 会提示自动清理了这条失效记录。
- `remove` 后磁盘上的 `/tmp/my-model` 仍在，只是清单里没了。

**预期结果**：能复述「`kt model add` → 扫描 → `suggest_name` → `add_model` → `save` 落盘」这条链路，并解释 UUID 为何用于模型间关联。

**待本地验证**：若本机没有任何模型目录，可用一个只含 `config.json` 的空目录试验 `add` 的报错与登记行为；MoE 列与 SHA256 状态需真实模型文件才会出现。

#### 4.1.5 小练习与答案

**练习 1**：用户把模型 A 改名成 B 后，原先「CPU 模型 C 关联到模型 A」的链接会失效吗？为什么？

**参考答案**：不会失效。关联关系存的是模型 A 的 UUID（`gpu_model_ids` 里是一串 UUID），而不是名字。`update_model` 只改 `name` 字段，UUID 不变，所以 C 仍能通过 `get_model_by_id` 找到改名后的 B。这正是 `__post_init__` 给每条记录补 UUID 的设计意义（`user_model_registry.py:42-47`）。

**练习 2**：`scan` 全盘扫描时，如何避免把已经在清单里的模型重复登记？

**参考答案**：`discover_and_register_global` 在登记前对每个扫描结果调用 `registry.find_by_path(model.path)`（`model_discovery.py:50-51`），路径已在清单中的就跳过。`find_by_path` 还会用 `Path.resolve()` 归一化路径再比较，避免软链/`.`/`..` 造成误判（`user_model_registry.py:227-244`）。

---

### 4.2 配置文件管理（Settings + kt config）

#### 4.2.1 概念说明

除了模型清单，`kt` 还需要一份「偏好设置」：用中文还是英文、模型默认放哪、服务器监听哪个端口、下载要不要校验、SGLang 从哪装……这些都写在另一个文件：

```
~/.ktransformers/config.yaml
```

它由 `Settings` 类管理。和「扁平的模型清单」不同，配置是一棵**嵌套字典树**，用点分键访问（如 `server.port`、`paths.models`、`general.language`）。`Settings` 的三件法宝是：

- **默认值表** `DEFAULT_CONFIG`：代码里写死的「出厂设置」，保证配置文件缺失或字段缺失时仍有合理值。
- **深合并（deep merge）**：加载时把用户配置叠到默认值上，只覆盖用户写过的字段，未写的保留默认——这样升级版本新增字段时，老配置不会丢字段。
- **单例 `get_settings()`**：全进程共享一个 `Settings` 实例，避免反复读盘。

`kt config` 子应用把这些能力暴露成命令：`show`/`set`/`get`/`reset`/`path`/`init`。

#### 4.2.2 核心流程

配置加载与读写的核心模型：

```
Settings()
  └─ _load()
       ├─ self._config = 深拷贝(DEFAULT_CONFIG)      # 先填出厂值
       ├─ 若 config.yaml 存在:
       │     user_config = yaml.safe_load(...)
       │     _deep_merge(self._config, user_config)  # 用户值叠加上去
       └─ _ensure_dirs()                              # 确保目录存在

读: get("server.port")
     → 按 "." split → 逐层下钻字典 → 找不到返回 default

写: set("server.port", 30001)
     → 按 "." split → 下钻到父节点 → 赋值 → _save() 落盘
```

`kt config` 子命令分工：

| 命令 | 作用 | 对应 Settings 方法 |
| --- | --- | --- |
| `kt config show [key]` | 显示全部或某个键（带语法高亮） | `get_all()` / `get(key)` |
| `kt config set <key> <value>` | 设置一个键（自动做类型推断） | `set(key, parsed_value)` |
| `kt config get <key>` | 取单个键的值 | `get(key)` |
| `kt config reset [--yes]` | 恢复全部默认值 | `reset()` |
| `kt config path` | 打印配置文件路径 | 读 `config_path` |
| `kt config init` | 重跑首运行向导（见 u3-l1） | 调 `_show_first_run_setup` |

#### 4.2.3 源码精读

配置文件与几个标准目录的默认位置：

[kt-kernel/python/cli/config/settings.py:L14-L17](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L14-L17) — 一切都集中在 `~/.ktransformers/` 下：配置 `config.yaml`、模型清单同目录、`models/` 与 `cache/` 两个子目录。

完整的默认配置树，是理解配置结构的最佳地图：

[kt-kernel/python/cli/config/settings.py:L20-L65](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L20-L65) — 六大段：`general`（语言/配色/verbose）、`paths`（模型/缓存/权重目录，`weights` 默认空）、`server`（host/port，端口默认 30000）、`inference.env`（推理时注入的环境变量，如 `PYTORCH_ALLOC_CONF`）、`download`（镜像/断点续传/校验）、`advanced`（自定义 env 与透传给 sglang/llamafactory 的额外参数）、`dependencies.sglang`（SGLang 安装源：pypi 或 github 及其 repo/branch）。

加载时把用户配置深合并到默认值之上，且失败时只警告不崩溃：

[kt-kernel/python/cli/config/settings.py:L93-L106](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L93-L106) — 先 `_deep_copy(DEFAULT_CONFIG)`，再 `_deep_merge`；YAML 解析失败只 `print` 警告并继续用默认值，保证配置坏了也能启动。

深合并的递归实现——只有「两边都是 dict」时才下钻，否则整体替换：

[kt-kernel/python/cli/config/settings.py:L125-L131](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L125-L131) — 这是「新增字段不丢、用户覆盖优先」的关键：例如用户只写了 `server.port`，合并后 `server.host` 仍是默认的 `0.0.0.0`。

点分键读取，逐层下钻，任意一层缺失即返回默认：

[kt-kernel/python/cli/config/settings.py:L133-L152](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L133-L152) — `get("server.port")` 会 `split(".")` 成 `["server","port"]`，依次取 `self._config["server"]["port"]`；中途若不是 dict 或键不存在，直接 `return default`。

点分键写入，会自动补齐缺失的中间层字典：

[kt-kernel/python/cli/config/settings.py:L154-L172](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L154-L172) — `set("a.b.c", v)` 时若 `a`/`b` 不存在会沿途 `config[part] = {}` 新建，最后赋值并 `_save()` 落盘。

模型存储路径支持「单字符串」与「字符串列表」两种形态，`get_model_paths` 统一返回 `list[Path]`：

[kt-kernel/python/cli/config/settings.py:L225-L240](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L225-L240) — 兼容 `paths.models: "/x"` 与 `paths.models: ["/x","/y"]` 两种写法，下游命令一律拿 `list[Path]` 用。这是 `kt model path-list`/`kt run`/`kt quant` 的共同入口。

新增/删除模型路径，并刻意「不允许删到一条不剩」：

[kt-kernel/python/cli/config/settings.py:L242-L282](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L242-L282) — `remove_model_path` 在删完会变空时返回 False，避免配置里一个存储路径都不剩导致后续命令无处放模型。

进程级单例，避免每个命令重复读盘：

[kt-kernel/python/cli/config/settings.py:L300-L305](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/config/settings.py#L300-L305) — `get_settings()` 用模块级 `_settings` 缓存；`reset_settings()` 供测试强制重建。

`kt config` 子应用把上面的能力包装成命令。`set` 命令会先做类型推断再写入：

[kt-kernel/python/cli/commands/config.py:L56-L68](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/config.py#L56-L68) — 命令行参数都是字符串，所以先 `_parse_value` 把 `"30001"` 转成整数、`"true"` 转成布尔、`"[1,2]"` 转成列表，再交给 `settings.set`。

类型推断函数依次尝试 bool → int → float → YAML(列表/字典) → 字符串：

[kt-kernel/python/cli/commands/config.py:L138-L167](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/config.py#L138-L167) — 注意它把 `yes/on/1` 都认作 True，`no/off/0` 认作 False，所以 `kt config set general.verbose true` 写进去的是布尔而非字符串。

`show` 命令对整棵配置用 rich 做语法高亮，对单个键则按类型分别输出：

[kt-kernel/python/cli/commands/config.py:L30-L53](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/config.py#L30-L53) — 无参数时 `yaml.dump(settings.get_all())` 配 `Syntax(theme="monokai")` 高亮打印；还顺带打印配置文件位置，方便用户定位。

#### 4.2.4 代码实践

**实践目标**：用 `kt config` 改一项服务器配置，并验证它被正确写入 `config.yaml`。

**操作步骤**：

1. 先看当前配置（确认文件位置）：
   ```bash
   kt config show
   kt config path
   ```
2. 改一个值，例如把服务器端口从默认 30000 改成 30001：
   ```bash
   kt config set server.port 30001
   ```
3. 验证写入（两种方式）：
   ```bash
   kt config get server.port
   kt config show server
   cat ~/.ktransformers/config.yaml
   ```
4. 试试类型推断：设一个布尔和一个列表
   ```bash
   kt config set general.verbose true
   kt config show general
   ```
5. （可选）恢复默认：`kt config reset --yes`。

**需要观察的现象**：

- `set server.port 30001` 后，`config.yaml` 里 `server` 段的 `port` 变成 `30001`，而同段的 `host: 0.0.0.0` 仍在（深合并保住了未改字段）。
- `get server.port` 直接打印 `30001`（整数，不是带引号的字符串）。
- `set general.verbose true` 写入的是布尔 `true`，说明 `_parse_value` 生效。

**预期结果**：能解释「点分键 → 下钻字典 → 落盘」的写入链路，以及深合并为何能保住未提及的字段。

**待本地验证**：若 `~/.ktransformers/config.yaml` 尚不存在，第一次 `kt config set` 会自动创建它（`_save` 里 `_ensure_dirs` 会建目录）。

#### 4.2.5 小练习与答案

**练习 1**：用户手动在 `config.yaml` 里只写了一行 `server: {port: 12345}`，重启 `kt` 后 `server.host` 是什么？为什么？

**参考答案**：仍是默认的 `0.0.0.0`。`_load` 先把 `self._config` 设为 `DEFAULT_CONFIG` 的深拷贝，再 `_deep_merge` 叠加用户配置；用户只写了 `port`，合并时只在 `server` 这层下钻覆盖 `port`，`host` 维持默认（`settings.py:93-106` 与 `125-131`）。

**练习 2**：`kt config set advanced.sglang_args "['--tp','2']"` 能否把一个列表写进配置？依据是什么？

**参考答案**：能。`set` 命令先调用 `_parse_value`，它在前几种类型都不命中后，会尝试 `yaml.safe_load`，若解析出 `dict`/`list` 就返回该结构（`config.py:158-164`）。所以字符串 `"['--tp','2']"` 会被解析成真正的 Python 列表再写入，`kt config show advanced` 会看到列表而非带引号字符串。

---

### 4.3 内置模型库（BUILTIN_MODELS + ModelRegistry）

#### 4.3.1 概念说明

4.1 节的 `UserModelRegistry` 回答「我**有**什么」，但还有另一类问题：用户敲 `kt quant deepseek-v3` 或想跑某个模型时，`kt` 需要知道「`deepseek-v3` 这个名字 kt 认不认得？它的 HuggingFace 仓库是哪个？推荐用哪些参数？每张 GPU 大概能放几个专家？」。这些问题由**内置模型库**回答。

它和用户注册表的对比是本讲的关键：

| 维度 | 用户模型注册表 `UserModelRegistry` | 内置模型库 `ModelRegistry` |
| --- | --- | --- |
| 文件 | `~/.ktransformers/user_models.yaml`（运行时生成） | `model_registry.py` 里的 `BUILTIN_MODELS` 常量（代码内固化）+ 可选 `registry.yaml` 覆盖 |
| 回答的问题 | 我磁盘上**有哪些**模型文件 | kt **认识/推荐**哪些模型、怎么跑 |
| 记录类 | `UserModel`（path/format/UUID/repo…） | `ModelInfo`（hf_repo/aliases/default_params…） |
| 是否含本地路径 | **是**（核心字段） | **否**（只有仓库 ID 与推荐参数） |
| 主要消费方 | `kt model *`、`kt run` 解析本地路径 | `kt quant` 模糊匹配模型名、提供推荐参数与 GPU 专家数计算 |
| 标识稳定性 | 用 UUID 关联 | 用名字+别名做（模糊）匹配 |

一句话：**用户注册表是「库存清单」，内置模型库是「产品目录」**。两者互补——`kt run`/`kt quant` 往往先用内置库把名字认出来、拿到推荐参数，再在用户注册表里找到本地路径。

#### 4.3.2 核心流程

内置库的加载把「固化的内置模型」与「用户自定义覆盖」合并：

```
ModelRegistry()
  ├─ _load_builtin_models()      # 遍历 BUILTIN_MODELS 逐个 _register
  │     → self._models[name.lower()] = ModelInfo
  │     → self._aliases[alias.lower()] = name.lower()
  └─ _load_user_models()         # 读 ~/.ktransformers/registry.yaml 覆盖/扩展
        （文件不存在则跳过）

查找:
  get(name)    → 先精确名 → 再别名表 → 命中返回 ModelInfo，否则 None
  search(query)→ 对每个模型算 _match_score（精确/别名/包含/fuzzy）
                 按分数降序返回 top-N

计算:
  MODEL_COMPUTE_FUNCTIONS[model_name](tp_size, vram_per_gpu) → 推荐每 GPU 专家数
```

匹配分数 `_match_score` 体现了一种「宽进严排」的模糊匹配策略：

```
1.0  名字完全相等（大小写不敏感）
0.95 别名完全相等
0.8  查询是名字的子串
0.7  查询是某别名的子串
0.6  查询在 hf_repo 里
0.5* 查询拆词后命中的比例（fuzzy）
0.0  全不沾边
```

#### 4.3.3 源码精读

`ModelInfo` 数据类描述「一个被支持的模型」的元信息：

[kt-kernel/python/cli/utils/model_registry.py:L17-L30](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L17-L30) — `hf_repo` 是 HuggingFace 仓库 ID；`aliases` 是简称列表（如 `dsv3`/`v3`）；`default_params` 是推荐启动参数（如 `kt-method`、`attention-backend`）；`max_tensor_parallel_size` 限制该模型最大张量并行度。

内置支持的模型清单，每个都带了推荐参数——这其实就是 KTransformers 官方「调好的配方」：

[kt-kernel/python/cli/utils/model_registry.py:L33-L172](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L33-L172) — 例如 `DeepSeek-V3-0324` 推荐 `kt-method: AMXINT4`、`kt-num-gpu-experts: 1`；`DeepSeek-V3.2` 推荐 `kt-method: FP8`；`DeepSeek-V4-Flash` 用 `MXFP4`；`Kimi-K2-Thinking` 用 `RAWINT4`；`MiniMax-M2`/`M2.1` 还带 `max_tensor_parallel_size: 4` 的限制。新增一个受支持模型，就是往这张表加一项。

`ModelRegistry` 构造时合并内置模型与用户 `registry.yaml` 覆盖：

[kt-kernel/python/cli/utils/model_registry.py:L178-L215](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L178-L215) — `_load_user_models` 从 `settings.config_dir / "registry.yaml"` 读取用户自定义模型并 `_register` 进同一张表，所以用户能在不碰源码的前提下扩展受支持模型清单。

`get` 先精确名后别名，全部转小写做大小写不敏感匹配：

[kt-kernel/python/cli/utils/model_registry.py:L225-L237](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L225-L237) — `_register` 时就把名字和别名都转小写存进 `_models`/`_aliases`，查询时也转小写，因此 `DeepSeek-V3`、`deepseek-v3`、`dsv3` 都能命中同一条。

`search` 的模糊打分函数，体现了上面的 1.0/0.95/0.8/… 分级：

[kt-kernel/python/cli/utils/model_registry.py:L262-L297](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L262-L297) — 最后一段把查询按 `[-_.\s]` 拆词，统计有多少词出现在名字里，按命中比例给分，容错用户记不清完整名字。

`kt quant` 实际消费这个内置库——把用户给的名字模糊匹配到 `ModelInfo`，再到模型目录里找本地文件：

[kt-kernel/python/cli/commands/quant.py:L486-L501](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/commands/quant.py#L486-L501) — `registry.search(model)` 取最佳匹配，再用 `model_info.name`/`hf_repo` 在配置的模型路径下找真实目录。这里能清楚看到「内置库负责认名字，用户路径负责找文件」的分工。

各模型「每 GPU 能放几个专家」的估算函数表，用于交互式参数推荐：

[kt-kernel/python/cli/utils/model_registry.py:L382-L433](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L382-L433) — 如 `compute_deepseek_v3_gpu_experts` 按 `(tp*(vram-16))//3` 估算；V4 因 MXFP4 更省显存用 `*2//3`；M2 用 `//1`。这是「热专家放 GPU」理念在 CLI 层的参数化体现（热/冷专家分工见 u1-l1、u6-l1）。

内置库同样是进程级单例：

[kt-kernel/python/cli/utils/model_registry.py:L365-L374](https://github.com/kvcache-ai/ktransformers/blob/cb9f47d142a507cac5d74450b30463d2e8d1cf58/kt-kernel/python/cli/utils/model_registry.py#L365-L374) — `get_registry()` 懒加载，与 `get_settings()` 同样的单例模式。

#### 4.3.4 代码实践

**实践目标**：用一行 Python 直接体验内置库的模糊匹配，理解它和用户注册表的区别。

**操作步骤**：

1. 进入 Python，构造内置库并查询：
   ```bash
   python -c "from kt_kernel.cli.utils.model_registry import get_registry; r=get_registry(); m=r.get('dsv3'); print(m.name, m.hf_repo, m.default_params.get('kt-method'))"
   ```
2. 体验模糊搜索，看不同输入的打分排序：
   ```bash
   python -c "from kt_kernel.cli.utils.model_registry import get_registry; r=get_registry(); print([(m.name) for m in r.search('deepseek')])"
   ```
3. 对比「内置库」与「用户注册表」两份单例：
   ```bash
   python -c "
   from kt_kernel.cli.utils.model_registry import get_registry
   from kt_kernel.cli.utils.user_model_registry import UserModelRegistry
   print('内置支持模型数:', len(get_registry().list_all()))
   print('我已登记模型数:', UserModelRegistry().get_model_count())
   "
   ```
4. 若想扩展受支持模型，可手写 `~/.ktransformers/registry.yaml`（示例代码，字段需与 `ModelInfo` 对应）：
   ```yaml
   # 示例代码：自定义一个受支持模型（覆盖/扩展内置库）
   models:
     MyModel:
       hf_repo: org/MyModel
       aliases: [mymodel, mm]
       type: moe
       default_params:
         kt-method: AMXINT4
   ```
   再重复第 1 步用 `get('mm')` 看能否命中。

**需要观察的现象**：

- 第 1 步 `dsv3` 这个别名能命中 `DeepSeek-V3-0324`，打印出仓库与推荐 `kt-method`。
- 第 2 步 `search('deepseek')` 会把多个 DeepSeek 变体按相关度排序返回。
- 第 3 步两个数字含义不同：前者是「kt 认识多少模型」（固定+自定义），后者是「我磁盘上登记了多少」（随 `kt model add` 增长）。

**预期结果**：能说清「内置库靠名字/别名匹配、不含本地路径；用户注册表靠路径、含 UUID」的本质差异，并指出 `kt quant` 是同时使用两者的典型命令。

**待本地验证**：第 1–3 步需已安装 `kt-kernel`；自定义 `registry.yaml` 后需新开 Python 进程才能让 `get_registry()` 重新加载。

#### 4.3.5 小练习与答案

**练习 1**：用户敲 `kt quant v3`，`kt` 是怎么把它对应到一个真实模型目录的？分别用到内置库和用户侧的什么东西？

**参考答案**：先用内置库 `get_registry().search("v3")` 模糊匹配，取最高分的 `ModelInfo`（`v3` 命中别名，`quant.py:488-492`）；再用 `ModelInfo.name`/`hf_repo` 在 `settings.get_model_paths()` 返回的模型目录里找本地真实路径（`quant.py:494-501`）。内置库负责「认名字+给仓库 ID」，配置的模型路径/用户注册表负责「找到磁盘文件」。

**练习 2**：为什么 `MiniMax-M2` 在 `ModelInfo` 里带 `max_tensor_parallel_size: 4`，而 `DeepSeek-V3-0324` 没有？

**参考答案**：因为 M2/M2.1 模型本身只支持最高 4 路张量并行（`model_registry.py:146,170`），CLI 在交互式收集参数时需要据此限制 `--tp` 的可选范围，避免用户设了无法运行的并行度；DeepSeek-V3 没有这个限制故留空（`None`）。这体现了「内置库不仅是名字表，还承载每个模型的运行约束」。

## 5. 综合实践

把本讲三个模块串成一个「登记模型 → 认模型 → 改配置」的小任务：

1. **准备目录**：建一个含 `config.json`（或 `*.gguf`）的测试模型目录 `/tmp/demo-model`。
2. **登记进用户注册表**：`kt model add /tmp/demo-model`，记下登记的名字。
3. **验证清单**：`kt model list` 能看到它；`cat ~/.ktransformers/user_models.yaml` 看到一条含 UUID 的记录。
4. **认识内置库**：用第 4.3.4 步的 Python 片段确认 `get_registry()` 与 `UserModelRegistry()` 是两套账本，数量含义不同。
5. **改一项配置**：`kt config set server.port 30001`，再 `kt config show server` 与 `cat ~/.ktransformers/config.yaml` 确认端口已改、`host` 因深合并仍在。
6. **清理**：`kt model remove <名字>`（不删磁盘文件），可选 `kt config reset --yes` 恢复默认。
7. **画一张关系图**：用自己的话画出「`kt model *` → `UserModelRegistry` → `user_models.yaml`」与「`kt quant` → 内置 `ModelRegistry` 认名字 → 用户路径找文件 → `Settings` 提供路径」两条数据流，标注关键源码行号。

> 若本机没有真实大模型，第 1–3 步可用空壳目录验证登记/清理逻辑；MoE 列、SHA256 校验、内置库的 `default_params` 需要真实模型文件或真实模型名才能完整体验。

## 6. 本讲小结

- KTransformers 维护**两套模型账本**：`UserModelRegistry`（`~/.ktransformers/user_models.yaml`）记录「我磁盘上有什么」，是带本地路径与 UUID 的库存清单；内置 `ModelRegistry`/`BUILTIN_MODELS`（`model_registry.py`）记录「kt 认识/推荐什么」，是带 `hf_repo`/别名/推荐参数的产品目录。
- `kt model scan/add/list/edit/info/verify/remove/download` 全部围绕 `UserModelRegistry` 的内存列表展开：先改内存、再 `save()` 落盘；新增靠 `suggest_name` 避免重名，关联靠 UUID 保证改名不失效，`list` 会惰性清理路径失效的记录。
- 配置由 `Settings` 管理 `~/.ktransformers/config.yaml`：用 `DEFAULT_CONFIG` 提供出厂值、用深合并叠加用户配置（保住未写字段）、用点分键（`server.port`）读写嵌套字典、用 `get_settings()` 做进程单例；配置分 general/paths/server/inference/download/advanced/dependencies 七段。
- `kt config show/set/get/reset/path/init` 把 `Settings` 包装成命令；`set` 会用 `_parse_value` 做 bool/int/float/YAML/字符串的类型推断，保证写入的值类型正确。
- 内置库的 `get`/`search` 提供大小写不敏感与多级模糊匹配，`kt quant` 是典型消费者：先认名字（内置库）再找文件（用户路径/`Settings`）；`MODEL_COMPUTE_FUNCTIONS` 还把「热专家放 GPU」理念参数化为每 GPU 专家数估算。

## 7. 下一步学习建议

- 想知道用户注册表里的模型如何被 `kt run` 解析成 sglang 启动命令？复习 **u3-l2 用 kt run 启动推理服务**，它正是用 `UserModelRegistry` 把模型名映射到本地路径（`run.py:31,222`）。
- 想深入内置库里那些 `kt-method: AMXINT4/FP8/MXFP4/RAWINT4` 推荐参数的含义？进入 **u4-l1 KTMoEWrapper 工厂与后端分发** 与 **u5 后端实现**，那里解释每种 method 背后的 CPU 后端。
- 对 `paths.models` 多路径、`MODEL_COMPUTE_FUNCTIONS` 的 GPU 专家估算感兴趣？这些会在 **u6 SGLang 集成与专家调度**（特别是专家放置策略与 NUMA 线程池）展开。
- 若想把受支持模型加入内置库而不改源码，可继续研究 `model_registry.py` 的 `_load_user_models` 与 `~/.ktransformers/registry.yaml` 的自定义扩展机制，并对比它与「用 `kt model add` 登记本地文件」的差别。
